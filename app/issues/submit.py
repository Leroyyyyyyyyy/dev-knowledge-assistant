"""
Confirm-and-submit, and reconciliation when the outcome is unknown (spec §7.2).

    draft ──confirm──> submitting ──> created
      ^                    ├────────> failed   (GitHub certainly did not create it)
      └──── resubmit ──────┤
                           └────────> unknown  (it may exist; reconcile first)

Only the request whose conditional UPDATE moved the draft to 'submitting' calls
GitHub, so two clicks on the same confirmation create at most one Issue. This is
duplicate control plus reconciliation, not exactly-once: the local database and
GitHub cannot commit together.
"""

import sqlite3
import uuid
from datetime import UTC, datetime

from app.issues.drafts import get_draft, text_hash
from app.issues.github import (
    GitHubClient,
    IssueLookupFailed,
    IssueNotCreated,
    IssueOutcomeUnknown,
)


class SubmitRefused(Exception):
    """The submission was not started; nothing was sent. The message is for the operator."""


class NotReconcilable(Exception):
    pass


def marker_for(draft_id: str) -> str:
    """Stable hidden marker written into the Issue body, so reconciliation can find it."""
    return f"<!-- dka-draft:{draft_id} -->"


def body_to_send(body: str, draft_id: str) -> str:
    return body + "\n\n" + marker_for(draft_id) + "\n"


def now() -> str:
    return datetime.now(UTC).isoformat()


def submit_draft(
    connection: sqlite3.Connection,
    client: GitHubClient | None,
    *,
    draft_id: str,
    confirmed_revision: int,
    confirmed_hash: str,
    allowed_repo: str | None,
    enabled: bool,
) -> dict:
    draft = get_draft(connection, draft_id)

    if draft["status"] == "created":
        # Confirming again after success returns the existing Issue.
        return draft
    if draft["status"] == "submitting":
        raise SubmitRefused("这份草稿正在提交，请稍后刷新查看结果。")
    if draft["status"] == "unknown":
        raise SubmitRefused("上一次提交的结果未知，Issue 可能已经创建。请先对账，确认没有创建之后才能重新提交。")
    if not enabled:
        raise SubmitRefused("服务没有开启提交（DKA_ISSUE_SUBMIT_ENABLED），没有发送任何内容。")
    if client is None:
        raise SubmitRefused("服务没有配置 GitHub token（DKA_GITHUB_TOKEN），没有发送任何内容。")
    if not draft["target_repo"]:
        raise SubmitRefused("这份草稿没有目标仓库，不能提交。")
    if draft["target_repo"] != allowed_repo:
        raise SubmitRefused(f"草稿的目标仓库 {draft['target_repo']} 不是服务当前允许的仓库，没有发送任何内容。")
    if draft["revision"] != confirmed_revision or draft["body_hash"] != confirmed_hash:
        raise SubmitRefused(
            f"你确认的是第 {confirmed_revision} 版，草稿现在是第 {draft['revision']} 版。请查看最新内容后重新确认。"
        )

    body = body_to_send(draft["body"], draft_id)
    attempt_id = str(uuid.uuid4())
    started = now()
    try:
        with connection:
            # The claim. Of two concurrent confirmations only one matches this row.
            moved = connection.execute(
                """
                UPDATE issue_drafts SET status = 'submitting', submitted_revision = ?, failure_reason = NULL,
                                        reconcile_result = NULL, updated_at = ?
                WHERE draft_id = ? AND status IN ('draft', 'failed') AND current_revision = ?
                """,
                (confirmed_revision, started, draft_id, confirmed_revision),
            ).rowcount
            if moved != 1:
                raise SubmitRefused("草稿的状态或版本刚刚变了（可能是重复点击），这次没有提交。请刷新查看。")
            connection.execute(
                """
                INSERT INTO issue_submissions (attempt_id, draft_id, revision, body_hash, target_repo, started_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (attempt_id, draft_id, confirmed_revision, text_hash(draft["title"], body), draft["target_repo"], started),
            )
    except sqlite3.IntegrityError:
        raise SubmitRefused("这份草稿已经有一次提交在进行中，这次没有提交。") from None

    # From here on, this request owns the submission.
    try:
        issue = client.create_issue(draft["target_repo"], draft["title"], body)
    except IssueNotCreated as exc:
        finish(connection, draft_id, attempt_id, "failed", str(exc))
        return get_draft(connection, draft_id)
    except IssueOutcomeUnknown as exc:
        finish(connection, draft_id, attempt_id, "unknown", str(exc))
        return get_draft(connection, draft_id)
    except Exception as exc:
        # Anything unexpected after the claim: the request may have been sent.
        finish(connection, draft_id, attempt_id, "unknown", f"提交时出现意外错误（{type(exc).__name__}）。")
        return get_draft(connection, draft_id)

    try:
        finish(connection, draft_id, attempt_id, "created", None, issue_url=issue.url, issue_number=issue.number)
    except Exception:
        # Created on GitHub but not recorded here. Never report that as failed:
        # record unknown with the URL we did get, or leave 'submitting' for
        # recover_interrupted to turn into unknown on the next start.
        try:
            finish(connection, draft_id, attempt_id, "unknown", f"GitHub 已创建 {issue.url}，但本地保存结果失败。")
        except Exception:
            raise SubmitRefused(f"GitHub 已创建 {issue.url}，但本地没能记下结果。重启服务后对这份草稿对账。") from None
    return get_draft(connection, draft_id)


def finish(
    connection: sqlite3.Connection,
    draft_id: str,
    attempt_id: str,
    outcome: str,
    detail: str | None,
    issue_url: str | None = None,
    issue_number: int | None = None,
) -> None:
    finished = now()
    with connection:
        connection.execute(
            """
            UPDATE issue_drafts SET status = ?, failure_reason = ?, issue_url = COALESCE(?, issue_url),
                                    issue_number = COALESCE(?, issue_number), updated_at = ?
            WHERE draft_id = ? AND status = 'submitting'
            """,
            (outcome, detail, issue_url, issue_number, finished, draft_id),
        )
        connection.execute(
            "UPDATE issue_submissions SET outcome = ?, detail = ?, finished_at = ? WHERE attempt_id = ? AND outcome IS NULL",
            (outcome, detail if detail else issue_url, finished, attempt_id),
        )


def recover_interrupted(connection: sqlite3.Connection) -> int:
    """
    At startup no request is in flight (single instance), so a draft still
    'submitting' was interrupted mid-call. Its Issue may exist: mark it unknown.
    """
    finished = now()
    reason = "服务在提交过程中重启，不能确定 Issue 是否已创建。请对账。"
    with connection:
        changed = connection.execute(
            "UPDATE issue_drafts SET status = 'unknown', failure_reason = ?, updated_at = ? WHERE status = 'submitting'",
            (reason, finished),
        ).rowcount
        connection.execute(
            "UPDATE issue_submissions SET outcome = 'unknown', detail = ?, finished_at = ? WHERE outcome IS NULL",
            (reason, finished),
        )
    return changed


def reconcile_draft(connection: sqlite3.Connection, client: GitHubClient | None, draft_id: str) -> dict:
    """
    Look for the draft's marker in the target repository. Found: record the
    Issue. Not found: stay unknown. Absence is not proof (the Issue may have
    been deleted or the marker edited away), so only an operator may decide
    it was never created.
    """
    draft = get_draft(connection, draft_id)
    if draft["status"] != "unknown":
        raise NotReconcilable(f"只有结果未知的草稿需要对账，这份草稿是 {draft['status']}。")
    if client is None:
        raise NotReconcilable("服务没有配置 GitHub token，不能对账。")

    checked = now()
    try:
        found = client.find_issues_with_marker(draft["target_repo"], marker_for(draft_id))
    except IssueLookupFailed as exc:
        with connection:
            connection.execute(
                "UPDATE issue_drafts SET reconcile_result = 'error', reconciled_at = ?, failure_reason = ? WHERE draft_id = ?",
                (checked, f"对账失败：{exc}", draft_id),
            )
        return get_draft(connection, draft_id)

    with connection:
        if found:
            first = min(found, key=lambda issue: issue.number)
            note = None
            if len(found) > 1:
                numbers = []
                for issue in sorted(found, key=lambda issue: issue.number):
                    numbers.append(f"#{issue.number}")
                note = f"仓库里有 {len(found)} 个带这份草稿标记的 Issue（{'、'.join(numbers)}），记录了最早的一个，其余请人工处理。"
            connection.execute(
                """
                UPDATE issue_drafts SET status = 'created', issue_url = ?, issue_number = ?, failure_reason = ?,
                                        reconcile_result = 'found', reconciled_at = ?, updated_at = ?
                WHERE draft_id = ? AND status = 'unknown'
                """,
                (first.url, first.number, note, checked, checked, draft_id),
            )
        else:
            connection.execute(
                """
                UPDATE issue_drafts SET reconcile_result = 'not_found', reconciled_at = ?,
                                        failure_reason = '对账没有找到带这份草稿标记的 Issue。这不能证明没有创建，请到仓库里确认。'
                WHERE draft_id = ? AND status = 'unknown'
                """,
                (checked, draft_id),
            )
    return get_draft(connection, draft_id)


def mark_not_created(connection: sqlite3.Connection, draft_id: str) -> dict:
    """The operator checked the repository and confirms no Issue exists: allow a new submission."""
    with connection:
        moved = connection.execute(
            """
            UPDATE issue_drafts SET status = 'failed', updated_at = ?,
                                    failure_reason = '操作者确认 GitHub 上没有创建这个 Issue，可以重新提交。'
            WHERE draft_id = ? AND status = 'unknown' AND reconcile_result = 'not_found'
            """,
            (now(), draft_id),
        ).rowcount
    if moved != 1:
        raise NotReconcilable("只有对账没有找到 Issue 的草稿，才能标记为未创建。")
    return get_draft(connection, draft_id)
