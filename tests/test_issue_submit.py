"""
Confirm-and-submit against a fake GitHub (spec §9: external writes are mocked
in automated tests; real submissions are tested by hand and recorded apart).
"""

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import chromadb
import httpx
import pytest
from fastapi.testclient import TestClient

from app.indexing.build import build_index_version
from app.issues.github import (
    CreatedIssue,
    HttpGitHubClient,
    IssueLookupFailed,
    IssueNotCreated,
    IssueOutcomeUnknown,
)
from app.main import create_app
from app.storage.db import connect
from tests.conftest import AUTH

PASSWORD = "operator-password-123"
REPO = "example/issue-sandbox"


class FakeGitHub:
    """Records every create call. 'mode' decides what the next create does."""

    def __init__(self) -> None:
        self.issues: list[tuple[CreatedIssue, str, str]] = []  # (issue, title, body)
        self.create_calls = 0
        self.mode = "ok"
        self.gate: threading.Event | None = None
        self.lookup_fails = False

    def create_issue(self, repo: str, title: str, body: str) -> CreatedIssue:
        self.create_calls += 1
        if self.gate is not None:
            self.gate.wait(timeout=5)
        if self.mode == "refused":
            raise IssueNotCreated("GitHub 拒绝了 token（401）：token 无效或已过期，换一个 token 后再提交。")
        if self.mode == "unknown_not_created":
            raise IssueOutcomeUnknown("GitHub 返回 502，不能确定 Issue 是否已创建。")
        number = len(self.issues) + 1
        issue = CreatedIssue(number=number, url=f"https://github.com/{repo}/issues/{number}")
        self.issues.append((issue, title, body))
        if self.mode == "response_lost":
            # Created on GitHub, but the answer never came back.
            raise IssueOutcomeUnknown("请求已发出但没有收到完整响应（ReadTimeout）。")
        return issue

    def find_issues_with_marker(self, repo: str, marker: str) -> list[CreatedIssue]:
        if self.lookup_fails:
            raise IssueLookupFailed("读取 Issue 列表失败（HTTP 503）。")
        found = []
        for issue, _title, body in self.issues:
            if marker in body:
                found.append(issue)
        return found


@pytest.fixture
def github() -> FakeGitHub:
    return FakeGitHub()


@pytest.fixture
def settings(workspace):
    return workspace["settings"].model_copy(
        update={"operator_password": PASSWORD, "issue_repo": REPO, "issue_submit_enabled": True}
    )


def make_client(settings, encoder, github):
    chroma = chromadb.PersistentClient(path=str(settings.chroma_dir))
    return TestClient(create_app(settings, encoder=encoder, chroma_client=chroma, github_client=github))


@pytest.fixture
def client(settings, encoder, github):
    with make_client(settings, encoder, github) as test_client:
        yield test_client


@pytest.fixture
def draft_id(client, workspace, encoder) -> str:
    build_index_version(workspace["settings"], encoder)
    run = client.post(
        "/api/retrieve", json={"query": "ZebraTransitionError cancel", "repo_id": "alpha", "top_k": 2}, headers=AUTH
    ).json()
    client.post(
        "/api/answers",
        json={
            "run_id": run["run_id"],
            "status": "answered",
            "answer_text": "cancel_order raises ZebraTransitionError.",
            "cited_chunk_ids": [run["chunks"][0]["chunk_id"]],
            "model": "test-model",
            "prompt_version": "p1",
        },
        headers=AUTH,
    )
    draft = client.post(
        "/api/issue-drafts", json={"idempotency_key": "key-00000001", "run_id": run["run_id"]}, headers=AUTH
    ).json()
    client.post("/login", data={"password": PASSWORD, "next": "/review"})
    return draft["draft_id"]


def form_of(client, draft_id: str) -> dict:
    page = client.get(f"/review/{draft_id}").text
    return {
        "csrf_token": re.search(r'name="csrf_token" value="([^"]+)"', page).group(1),
        "revision": re.search(r'name="revision" value="(\d+)"', page).group(1),
        "body_hash": re.search(r'name="body_hash" value="([0-9a-f]+)"', page).group(1),
    }


def csrf_of(client, draft_id: str) -> str:
    page = client.get(f"/review/{draft_id}").text
    return re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)


def submit(client, draft_id: str, form: dict):
    return client.post(f"/review/{draft_id}/submit", data=form, follow_redirects=False)


def status_of(client, draft_id: str) -> dict:
    return client.get(f"/review/{draft_id}/status").json()


def attempts(workspace, draft_id: str) -> list:
    connection = connect(workspace["settings"].db_path)
    rows = connection.execute(
        "SELECT revision, outcome FROM issue_submissions WHERE draft_id = ? ORDER BY started_at", (draft_id,)
    ).fetchall()
    connection.close()
    return [(row["revision"], row["outcome"]) for row in rows]


# --- the normal path --------------------------------------------------------


def test_confirm_creates_one_issue_with_the_confirmed_text(client, draft_id, github, workspace):
    response = submit(client, draft_id, form_of(client, draft_id))
    assert response.status_code == 303

    assert github.create_calls == 1
    issue, title, body = github.issues[0]
    assert title == "问题：ZebraTransitionError cancel"
    assert "cancel_order raises ZebraTransitionError." in body
    assert body.rstrip().endswith(f"<!-- dka-draft:{draft_id} -->")

    status = status_of(client, draft_id)
    assert status["status"] == "created"
    assert status["issue_url"] == issue.url
    assert status["submitted_revision"] == 1
    assert issue.url in client.get(f"/review/{draft_id}").text
    assert attempts(workspace, draft_id) == [(1, "created")]


def test_confirming_again_after_success_creates_nothing(client, draft_id, github):
    form = form_of(client, draft_id)
    submit(client, draft_id, form)
    again = submit(client, draft_id, form)
    assert again.status_code == 303
    assert github.create_calls == 1
    assert status_of(client, draft_id)["status"] == "created"


# --- confirmation is bound to one revision ----------------------------------


def test_confirmation_of_an_older_revision_is_refused(client, draft_id, github):
    old_form = form_of(client, draft_id)
    client.post(
        f"/review/{draft_id}",
        data={"csrf_token": old_form["csrf_token"], "revision": 1, "title": "改过的标题", "body": "改过的正文"},
    )
    response = submit(client, draft_id, old_form)
    assert response.status_code == 409
    assert "第 1 版" in response.text and "第 2 版" in response.text
    assert github.create_calls == 0


def test_confirmation_with_a_different_fingerprint_is_refused(client, draft_id, github):
    form = form_of(client, draft_id)
    form["body_hash"] = "0" * 64
    assert submit(client, draft_id, form).status_code == 409
    assert github.create_calls == 0


# --- double click -----------------------------------------------------------


def test_concurrent_double_click_creates_one_issue(client, draft_id, github, workspace):
    form = form_of(client, draft_id)
    github.gate = threading.Event()  # hold the first call inside "GitHub"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(submit, client, draft_id, form), pool.submit(submit, client, draft_id, form)]
        # The winner is held inside "GitHub"; the loser must come back on its own.
        for _ in range(200):
            if any(future.done() for future in futures):
                break
            time.sleep(0.01)
        loser_returned_while_winner_was_held = any(future.done() for future in futures)
        github.gate.set()
        statuses = sorted(future.result().status_code for future in futures)

    assert loser_returned_while_winner_was_held
    assert github.create_calls == 1
    assert statuses == [303, 409]
    assert status_of(client, draft_id)["status"] == "created"
    assert attempts(workspace, draft_id) == [(1, "created")]


# --- GitHub refuses -----------------------------------------------------------


def test_refusal_is_failed_and_can_be_fixed_and_resubmitted(client, draft_id, github, workspace):
    github.mode = "refused"
    submit(client, draft_id, form_of(client, draft_id))
    status = status_of(client, draft_id)
    assert status["status"] == "failed"
    assert "401" in status["failure_reason"] and "token" in status["failure_reason"]
    assert "401" in client.get(f"/review/{draft_id}").text

    github.mode = "ok"
    submit(client, draft_id, form_of(client, draft_id))
    assert status_of(client, draft_id)["status"] == "created"
    assert github.create_calls == 2
    assert attempts(workspace, draft_id) == [(1, "failed"), (1, "created")]


# --- the outcome is unknown -------------------------------------------------


def test_lost_response_is_unknown_and_never_resubmitted(client, draft_id, github):
    github.mode = "response_lost"
    form = form_of(client, draft_id)
    submit(client, draft_id, form)
    assert status_of(client, draft_id)["status"] == "unknown"

    github.mode = "ok"
    again = submit(client, draft_id, form)
    assert again.status_code == 409
    assert "对账" in again.text
    assert github.create_calls == 1  # no second Issue


def test_reconciliation_finds_the_issue_by_its_marker(client, draft_id, github):
    github.mode = "response_lost"
    submit(client, draft_id, form_of(client, draft_id))
    created_on_github = github.issues[0][0]

    client.post(f"/review/{draft_id}/reconcile", data={"csrf_token": csrf_of(client, draft_id)})
    status = status_of(client, draft_id)
    assert status["status"] == "created"
    assert status["issue_url"] == created_on_github.url
    assert status["reconcile_result"] == "found"
    assert github.create_calls == 1


def test_not_found_stays_unknown_until_the_operator_confirms(client, draft_id, github, workspace):
    github.mode = "unknown_not_created"
    submit(client, draft_id, form_of(client, draft_id))
    csrf = csrf_of(client, draft_id)

    # Before reconciling, the operator cannot declare it not created.
    early = client.post(f"/review/{draft_id}/mark-not-created", data={"csrf_token": csrf})
    assert early.status_code == 409

    client.post(f"/review/{draft_id}/reconcile", data={"csrf_token": csrf})
    status = status_of(client, draft_id)
    assert status["status"] == "unknown"  # absence is not proof
    assert status["reconcile_result"] == "not_found"

    client.post(f"/review/{draft_id}/mark-not-created", data={"csrf_token": csrf})
    assert status_of(client, draft_id)["status"] == "failed"

    github.mode = "ok"
    submit(client, draft_id, form_of(client, draft_id))
    assert status_of(client, draft_id)["status"] == "created"
    assert attempts(workspace, draft_id) == [(1, "unknown"), (1, "created")]


def test_failed_lookup_keeps_the_draft_unknown(client, draft_id, github):
    github.mode = "response_lost"
    submit(client, draft_id, form_of(client, draft_id))
    github.lookup_fails = True
    client.post(f"/review/{draft_id}/reconcile", data={"csrf_token": csrf_of(client, draft_id)})
    status = status_of(client, draft_id)
    assert status["status"] == "unknown"
    assert status["reconcile_result"] == "error"


def test_several_marked_issues_record_the_earliest_and_say_so(client, draft_id, github):
    github.mode = "response_lost"
    submit(client, draft_id, form_of(client, draft_id))
    marker_body = github.issues[0][2]
    duplicate = CreatedIssue(number=2, url=f"https://github.com/{REPO}/issues/2")
    github.issues.append((duplicate, "dup", marker_body))  # e.g. someone resubmitted by hand

    client.post(f"/review/{draft_id}/reconcile", data={"csrf_token": csrf_of(client, draft_id)})
    status = status_of(client, draft_id)
    assert status["issue_url"].endswith("/issues/1")
    assert "#1、#2" in status["failure_reason"]


def test_restart_turns_an_interrupted_submission_into_unknown(settings, encoder, github, client, draft_id, workspace):
    connection = connect(workspace["settings"].db_path)
    with connection:
        connection.execute("UPDATE issue_drafts SET status = 'submitting' WHERE draft_id = ?", (draft_id,))
        connection.execute(
            """
            INSERT INTO issue_submissions (attempt_id, draft_id, revision, body_hash, target_repo, started_at)
            VALUES ('interrupted', ?, 1, 'h', ?, '2026-10-12T00:00:00+00:00')
            """,
            (draft_id, REPO),
        )
    connection.close()

    with make_client(settings, encoder, github) as restarted:  # lifespan runs on start
        restarted.post("/login", data={"password": PASSWORD, "next": "/review"})
        assert status_of(restarted, draft_id)["status"] == "unknown"
    assert attempts(workspace, draft_id) == [(1, "unknown")]
    assert github.create_calls == 0


# --- who may submit ----------------------------------------------------------


def test_service_token_and_missing_csrf_cannot_submit(client, draft_id, github, settings, encoder):
    form = form_of(client, draft_id)

    # The Dify credential, without a browser session.
    with make_client(settings, encoder, github) as dify:
        response = dify.post(f"/review/{draft_id}/submit", data=form, headers=AUTH, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"].startswith("/login")

    for token in ("", "forged"):
        forged = dict(form, csrf_token=token)
        assert submit(client, draft_id, forged).status_code == 403
    assert github.create_calls == 0


def test_nothing_is_sent_while_submission_is_switched_off(workspace, encoder, github):
    off = workspace["settings"].model_copy(update={"operator_password": PASSWORD, "issue_repo": REPO})
    with make_client(off, encoder, github) as client:
        build_index_version(workspace["settings"], encoder)
        run = client.post("/api/retrieve", json={"query": "ZebraTransitionError", "repo_id": "alpha"}, headers=AUTH).json()
        client.post(
            "/api/answers",
            json={"run_id": run["run_id"], "status": "insufficient_evidence", "answer_text": "不确定。",
                  "cited_chunk_ids": [], "model": "m", "prompt_version": "p"},
            headers=AUTH,
        )
        draft = client.post("/api/issue-drafts", json={"idempotency_key": "key-off-0001", "run_id": run["run_id"]}, headers=AUTH).json()
        client.post("/login", data={"password": PASSWORD, "next": "/review"})
        response = submit(client, draft["draft_id"], form_of(client, draft["draft_id"]))
        assert response.status_code == 409
        assert "DKA_ISSUE_SUBMIT_ENABLED" in response.text
    assert github.create_calls == 0


def test_a_draft_for_another_repository_is_not_sent(client, draft_id, github, workspace):
    connection = connect(workspace["settings"].db_path)
    with connection:
        connection.execute("UPDATE issue_drafts SET target_repo = 'someone/else' WHERE draft_id = ?", (draft_id,))
    connection.close()
    response = submit(client, draft_id, form_of(client, draft_id))
    assert response.status_code == 409
    assert github.create_calls == 0


def test_status_needs_a_session(client, draft_id, settings, encoder, github):
    with make_client(settings, encoder, github) as anonymous:
        assert anonymous.get(f"/review/{draft_id}/status", headers=AUTH).status_code == 401


# --- how the real client sorts GitHub's answers -----------------------------


def http_client(handler) -> HttpGitHubClient:
    return HttpGitHubClient("test-token-not-real", transport=httpx.MockTransport(handler))


def test_http_client_sends_the_token_only_as_a_header():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["authorization"]
        seen["url"] = str(request.url)
        return httpx.Response(201, json={"number": 7, "html_url": f"https://github.com/{REPO}/issues/7"})

    issue = http_client(handler).create_issue(REPO, "t", "b")
    assert issue == CreatedIssue(number=7, url=f"https://github.com/{REPO}/issues/7")
    assert seen["auth"] == "Bearer test-token-not-real"
    assert "test-token-not-real" not in seen["url"]


@pytest.mark.parametrize(
    ("respond", "expected"),
    [
        (lambda request: httpx.Response(401, json={}), IssueNotCreated),
        (lambda request: httpx.Response(403, json={}), IssueNotCreated),
        (lambda request: httpx.Response(404, json={}), IssueNotCreated),
        (lambda request: httpx.Response(422, json={}), IssueNotCreated),
        (lambda request: httpx.Response(502, json={}), IssueOutcomeUnknown),
        (lambda request: httpx.Response(503, json={}), IssueOutcomeUnknown),
    ],
)
def test_http_client_sorts_status_codes(respond, expected):
    with pytest.raises(expected):
        http_client(respond).create_issue(REPO, "t", "b")


def test_http_client_sorts_network_failures():
    def connect_failure(request):
        raise httpx.ConnectError("refused", request=request)

    def read_timeout(request):
        raise httpx.ReadTimeout("no answer", request=request)

    with pytest.raises(IssueNotCreated):  # never connected, nothing sent
        http_client(connect_failure).create_issue(REPO, "t", "b")
    with pytest.raises(IssueOutcomeUnknown):  # sent, answer lost
        http_client(read_timeout).create_issue(REPO, "t", "b")


def test_http_client_lookup_skips_pull_requests_and_matches_the_marker():
    marker = "<!-- dka-draft:abc -->"

    def handler(request):
        return httpx.Response(
            200,
            json=[
                {"number": 3, "html_url": "u3", "body": "x " + marker},
                {"number": 2, "html_url": "u2", "body": marker, "pull_request": {}},
                {"number": 1, "html_url": "u1", "body": None},
            ],
        )

    assert http_client(handler).find_issues_with_marker(REPO, marker) == [CreatedIssue(number=3, url="u3")]
