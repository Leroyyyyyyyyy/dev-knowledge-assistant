"""
Issue drafts: built from a template, never by a model, and only ever submitted
after an operator confirms a specific revision on the review page (spec §7).

A draft is tied to one answered retrieval run. Its text is stored as numbered
revisions; editing adds a revision, so a confirmation made against revision 2
cannot submit revision 3.
"""

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import UTC, datetime

from app.answers import RunNotFound, accepted_answer
from app.feedback import NoAnswerForRun

NOT_PROVIDED = "未提供"
TITLE_MAX = 200

# Statuses in which the text may still change. Once a submission has started,
# what was sent must stay what is stored.
EDITABLE = ("draft", "failed")


class IdempotencyConflict(Exception):
    """The same idempotency key was used before with different content."""


class DraftNotFound(Exception):
    pass


class StaleRevision(Exception):
    """The operator edited an older revision than the current one."""


class DraftLocked(Exception):
    """The draft is being submitted or was submitted; its text can no longer change."""


def text_hash(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


def clean(value: str | None) -> str:
    return (value or "").strip()


def cited_chunks(connection: sqlite3.Connection, run_id: str, answer: sqlite3.Row) -> list[sqlite3.Row]:
    """The chunks the accepted answer cites, in the order it cites them."""
    rows = {}
    for row in connection.execute("SELECT * FROM run_chunks WHERE run_id = ?", (run_id,)):
        rows[row["chunk_id"]] = row
    chunks = []
    for chunk_id in json.loads(answer["cited_chunk_ids"]):
        if chunk_id in rows:
            chunks.append(rows[chunk_id])
    return chunks


def render(run: sqlite3.Row, answer: sqlite3.Row, chunks: list[sqlite3.Row], fields: dict) -> tuple[str, str, list[str]]:
    """
    Title and Markdown body from what is known. Anything the user did not give
    is written as 未提供 and listed under missing information; nothing is filled in.
    """
    question = run["query"].strip()
    title = fields["title"] or ("问题：" + question)
    title = title[:TITLE_MAX]

    missing = []
    for key, label in (("environment", "环境"), ("error_output", "报错原文"), ("steps_tried", "已尝试的步骤")):
        if not fields[key]:
            missing.append(label)

    lines = ["## 问题", "", question, ""]

    status_label = "已回答" if answer["status"] == "answered" else "证据不足"
    lines += [f"## 助手的回答（{status_label}）", "", answer["answer_text"].strip(), ""]

    lines += ["## 相关代码", ""]
    if chunks:
        for chunk in chunks:
            label = f"{chunk['repo_id']}:{chunk['path']}#L{chunk['start_line']}-L{chunk['end_line']}"
            lines.append(f"- [{label}]({chunk['url']})")
    else:
        lines.append(NOT_PROVIDED)
    lines.append("")

    lines += ["## 环境", "", fields["environment"] or NOT_PROVIDED, ""]

    lines += ["## 报错原文", ""]
    if fields["error_output"]:
        # Only what the user pasted counts as a real error message (spec §7.1).
        lines += ["```", fields["error_output"], "```"]
    else:
        lines.append(NOT_PROVIDED)
    lines.append("")

    lines += ["## 已尝试的步骤", "", fields["steps_tried"] or NOT_PROVIDED, ""]

    lines += ["## 仍缺少的信息", ""]
    if missing:
        for label in missing:
            lines.append(f"- {label}")
    else:
        lines.append("无")
    lines.append("")

    lines.append("---")
    lines.append(f"由研发知识助手根据检索记录 `{run['run_id']}`（索引版本 `{run['index_version']}`）生成，提交前经人工确认。")
    return title, "\n".join(lines), missing


# Things that should not leave the machine in a public Issue. These are warnings
# for the operator, not a filter: the operator edits the text and decides.
WARNING_PATTERNS = [
    (re.compile(r"/Users/[^/\s]+|/home/[^/\s]+|[A-Za-z]:\\Users\\[^\\\s]+"), "包含本机用户目录路径"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"), "包含疑似 GitHub token"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "包含疑似 API key"),
    (re.compile(r"(?i)(password|passwd|secret|token)\s*[=:]\s*\S+"), "包含疑似密码或密钥赋值"),
]


def warnings_for(text: str) -> list[str]:
    found = []
    for pattern, message in WARNING_PATTERNS:
        if pattern.search(text):
            found.append(message)
    return found


def create_draft(
    connection: sqlite3.Connection,
    *,
    idempotency_key: str,
    run_id: str,
    title: str | None,
    environment: str | None,
    error_output: str | None,
    steps_tried: str | None,
    target_repo: str | None,
    request_id: str,
) -> tuple[dict, bool]:
    """Returns (draft, created). A retry with the same key and content returns the original draft."""
    fields = {
        "title": clean(title),
        "environment": clean(environment),
        "error_output": clean(error_output),
        "steps_tried": clean(steps_tried),
    }
    request_hash = text_hash(run_id, fields["title"], fields["environment"], fields["error_output"], fields["steps_tried"])

    existing = connection.execute("SELECT * FROM issue_drafts WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
    if existing is not None:
        if existing["request_hash"] != request_hash:
            raise IdempotencyConflict(f"idempotency key already used for draft {existing['draft_id']} with different content")
        return get_draft(connection, existing["draft_id"]), False

    run = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if run is None:
        raise RunNotFound(f"no retrieval run {run_id}")
    answer = accepted_answer(connection, run_id)
    if answer is None:
        # An Issue is about an answer the user saw; without one there is nothing to cite.
        raise NoAnswerForRun(f"run {run_id} has no accepted answer to build an Issue from")

    title_text, body, missing = render(run, answer, cited_chunks(connection, run_id, answer), fields)
    draft_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    try:
        with connection:
            connection.execute(
                """
                INSERT INTO issue_drafts (draft_id, idempotency_key, request_hash, run_id, answer_id, target_repo,
                                          status, current_revision, missing_fields, request_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'draft', 1, ?, ?, ?, ?)
                """,
                (draft_id, idempotency_key, request_hash, run_id, answer["answer_id"], target_repo,
                 json.dumps(missing, ensure_ascii=False), request_id, now, now),
            )
            connection.execute(
                """
                INSERT INTO draft_revisions (draft_id, revision, title, body, body_hash, author, created_at)
                VALUES (?, 1, ?, ?, ?, 'template', ?)
                """,
                (draft_id, title_text, body, text_hash(title_text, body), now),
            )
    except sqlite3.IntegrityError:
        # Two identical requests raced; the unique key let only one in.
        winner = connection.execute("SELECT * FROM issue_drafts WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
        if winner is None or winner["request_hash"] != request_hash:
            raise IdempotencyConflict("idempotency key already used with different content") from None
        return get_draft(connection, winner["draft_id"]), False
    return get_draft(connection, draft_id), True


def get_draft(connection: sqlite3.Connection, draft_id: str) -> dict:
    draft = connection.execute("SELECT * FROM issue_drafts WHERE draft_id = ?", (draft_id,)).fetchone()
    if draft is None:
        raise DraftNotFound(f"no draft {draft_id}")
    revision = connection.execute(
        "SELECT * FROM draft_revisions WHERE draft_id = ? AND revision = ?", (draft_id, draft["current_revision"])
    ).fetchone()
    return {
        "draft_id": draft["draft_id"],
        "run_id": draft["run_id"],
        "status": draft["status"],
        "target_repo": draft["target_repo"],
        "revision": draft["current_revision"],
        "title": revision["title"],
        "body": revision["body"],
        "body_hash": revision["body_hash"],
        "author": revision["author"],
        "missing": json.loads(draft["missing_fields"]),
        "warnings": warnings_for(revision["title"] + "\n" + revision["body"]),
        "issue_url": draft["issue_url"],
        "issue_number": draft["issue_number"],
        "failure_reason": draft["failure_reason"],
        "submitted_revision": draft["submitted_revision"],
        "reconcile_result": draft["reconcile_result"],
        "reconciled_at": draft["reconciled_at"],
        "created_at": draft["created_at"],
        "updated_at": draft["updated_at"],
    }


def edit_draft(connection: sqlite3.Connection, draft_id: str, *, expected_revision: int, title: str, body: str) -> dict:
    """Save the operator's text as a new revision. Unchanged text adds nothing."""
    title = clean(title)[:TITLE_MAX]
    body = body.replace("\r\n", "\n").strip()
    current = get_draft(connection, draft_id)
    if current["status"] not in EDITABLE:
        raise DraftLocked(f"draft {draft_id} is {current['status']}; its text can no longer change")
    if current["revision"] != expected_revision:
        raise StaleRevision(f"draft {draft_id} is at revision {current['revision']}, the edit was made on {expected_revision}")
    if not title or not body:
        raise ValueError("title and body must not be empty")
    if title == current["title"] and body == current["body"]:
        return current

    new_revision = expected_revision + 1
    now = datetime.now(UTC).isoformat()
    with connection:
        # Conditional update: of two edits made on the same revision, only the
        # first moves the pointer; the second matches no row.
        moved = connection.execute(
            """
            UPDATE issue_drafts SET current_revision = ?, updated_at = ?
            WHERE draft_id = ? AND current_revision = ? AND status IN ('draft', 'failed')
            """,
            (new_revision, now, draft_id, expected_revision),
        ).rowcount
        if moved != 1:
            raise StaleRevision(f"draft {draft_id} changed while this edit was being saved")
        connection.execute(
            """
            INSERT INTO draft_revisions (draft_id, revision, title, body, body_hash, author, created_at)
            VALUES (?, ?, ?, ?, ?, 'operator', ?)
            """,
            (draft_id, new_revision, title, body, text_hash(title, body), now),
        )
    return get_draft(connection, draft_id)
