import re
import sqlite3

import chromadb
import pytest
from fastapi.testclient import TestClient

from app.indexing.build import build_index_version
from app.main import create_app
from app.storage.db import connect
from tests.conftest import AUTH

PASSWORD = "operator-password-123"
REPO = "example/issue-sandbox"


@pytest.fixture
def review_settings(workspace):
    return workspace["settings"].model_copy(update={"operator_password": PASSWORD, "issue_repo": REPO})


@pytest.fixture
def app_client(workspace, encoder, review_settings):
    chroma = chromadb.PersistentClient(path=str(review_settings.chroma_dir))
    app = create_app(review_settings, encoder=encoder, chroma_client=chroma)
    with TestClient(app) as client:
        yield client


@pytest.fixture
def answered_run(app_client, workspace, encoder) -> dict:
    build_index_version(workspace["settings"], encoder)
    run = app_client.post(
        "/api/retrieve", json={"query": "ZebraTransitionError cancel", "repo_id": "alpha", "top_k": 2}, headers=AUTH
    ).json()
    top = run["chunks"][0]
    response = app_client.post(
        "/api/answers",
        json={
            "run_id": run["run_id"],
            "status": "answered",
            "answer_text": "cancel_order raises ZebraTransitionError.",
            "cited_chunk_ids": [top["chunk_id"]],
            "model": "test-model",
            "prompt_version": "p1",
        },
        headers=AUTH,
    )
    assert response.status_code == 200
    return {"run": run, "cited": top}


def make_draft(client, run_id: str, key: str = "key-00000001", **fields):
    body = {"idempotency_key": key, "run_id": run_id}
    body.update(fields)
    return client.post("/api/issue-drafts", json=body, headers=AUTH)


def login(client, password: str = PASSWORD, next_path: str = "/review"):
    return client.post("/login", data={"password": password, "next": next_path}, follow_redirects=False)


def csrf_of(html: str) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def revisions(workspace, draft_id: str) -> list[sqlite3.Row]:
    connection = connect(workspace["settings"].db_path)
    rows = connection.execute(
        "SELECT revision, title, author FROM draft_revisions WHERE draft_id = ? ORDER BY revision", (draft_id,)
    ).fetchall()
    connection.close()
    return rows


# --- POST /api/issue-drafts -------------------------------------------------


def test_draft_is_built_from_the_answered_run(app_client, answered_run):
    run, cited = answered_run["run"], answered_run["cited"]
    response = make_draft(app_client, run["run_id"])
    assert response.status_code == 201
    draft = response.json()
    assert draft["created"] is True
    assert draft["status"] == "draft"
    assert draft["revision"] == 1
    assert draft["target_repo"] == REPO
    assert draft["title"] == "问题：ZebraTransitionError cancel"
    assert draft["missing"] == ["环境", "报错原文", "已尝试的步骤"]
    assert draft["review_url"] == "http://localhost:8077/review/" + draft["draft_id"]

    login(app_client)
    page = app_client.get(f"/review/{draft['draft_id']}").text
    assert "cancel_order raises ZebraTransitionError." in page
    assert cited["url"] in page  # the cited chunk's commit-pinned link
    # Nothing the user did not give is invented: no fenced error block, just 未提供.
    assert "```" not in page
    assert page.count("未提供") >= 3


def test_user_supplied_details_are_used_verbatim(app_client, answered_run):
    draft = make_draft(
        app_client,
        answered_run["run"]["run_id"],
        environment="macOS 15, Python 3.13",
        error_output="make: /opt/py/bin/python3: No such file or directory",
        steps_tried="python3 -m venv .venv",
    ).json()
    assert draft["missing"] == []

    login(app_client)
    page = app_client.get(f"/review/{draft['draft_id']}").text
    assert "```\nmake: /opt/py/bin/python3: No such file or directory\n```" in page
    assert "macOS 15, Python 3.13" in page


def test_retry_with_the_same_key_returns_the_same_draft(app_client, answered_run, workspace):
    run_id = answered_run["run"]["run_id"]
    first = make_draft(app_client, run_id, environment="linux")
    second = make_draft(app_client, run_id, environment="linux")
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["draft_id"] == first.json()["draft_id"]
    assert second.json()["created"] is False

    connection = connect(workspace["settings"].db_path)
    count = connection.execute("SELECT COUNT(*) FROM issue_drafts").fetchone()[0]
    connection.close()
    assert count == 1


def test_same_key_with_different_content_is_a_conflict(app_client, answered_run):
    run_id = answered_run["run"]["run_id"]
    make_draft(app_client, run_id, environment="linux")
    response = make_draft(app_client, run_id, environment="windows")
    assert response.status_code == 409
    assert response.json()["code"] == "IDEMPOTENCY_CONFLICT"


def test_a_new_key_makes_a_separate_draft(app_client, answered_run):
    run_id = answered_run["run"]["run_id"]
    first = make_draft(app_client, run_id, key="key-00000001").json()
    second = make_draft(app_client, run_id, key="key-00000002").json()
    assert first["draft_id"] != second["draft_id"]


def test_unknown_or_unanswered_runs_are_refused(app_client, workspace, encoder):
    missing = make_draft(app_client, "00000000-0000-4000-8000-000000000000")
    assert missing.status_code == 404
    assert missing.json()["code"] == "RUN_NOT_FOUND"

    build_index_version(workspace["settings"], encoder)
    run = app_client.post("/api/retrieve", json={"query": "walrus"}, headers=AUTH).json()
    unanswered = make_draft(app_client, run["run_id"])
    assert unanswered.status_code == 409
    assert unanswered.json()["code"] == "NO_ANSWER_FOR_RUN"


def test_draft_api_requires_the_service_token(app_client, answered_run):
    response = app_client.post(
        "/api/issue-drafts", json={"idempotency_key": "key-00000001", "run_id": answered_run["run"]["run_id"]}
    )
    assert response.status_code == 401


def test_private_paths_and_secrets_are_flagged(app_client, answered_run):
    draft = make_draft(
        app_client,
        answered_run["run"]["run_id"],
        environment="venv at /Users/alice/.pyenv/versions/3.13",
        steps_tried="exported GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz0123",
    ).json()
    assert "包含本机用户目录路径" in draft["warnings"]
    assert "包含疑似 GitHub token" in draft["warnings"]


# --- review pages ----------------------------------------------------------


def test_review_needs_a_login_and_the_service_token_is_not_one(app_client, answered_run):
    draft_id = make_draft(app_client, answered_run["run"]["run_id"]).json()["draft_id"]
    for headers in ({}, AUTH):
        response = app_client.get(f"/review/{draft_id}", headers=headers, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == f"/login?next=/review/{draft_id}"


def test_wrong_password_is_refused(app_client):
    response = login(app_client, password="not-the-password")
    assert response.status_code == 401
    assert "dka_session" not in response.cookies


def test_login_without_a_configured_password_is_unavailable(client):
    page = client.get("/login")
    assert "DKA_OPERATOR_PASSWORD" in page.text
    response = client.post("/login", data={"password": "anything-at-all", "next": "/review"})
    assert response.status_code == 503


def test_login_only_redirects_to_draft_pages(app_client, answered_run):
    draft_id = make_draft(app_client, answered_run["run"]["run_id"]).json()["draft_id"]
    assert login(app_client, next_path=f"/review/{draft_id}").headers["location"] == f"/review/{draft_id}"
    assert login(app_client, next_path="https://evil.example/").headers["location"] == "/review"
    assert login(app_client, next_path="//evil.example/review").headers["location"] == "/review"


def test_review_page_shows_the_draft_with_protective_headers(app_client, answered_run):
    draft = make_draft(app_client, answered_run["run"]["run_id"]).json()
    login(app_client)
    response = app_client.get(f"/review/{draft['draft_id']}")
    assert response.status_code == 200
    assert REPO in response.text
    assert "第 1 版" in response.text
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["cache-control"] == "no-store"
    # The confirm button exists but submission is not wired up yet.
    assert re.search(r"<button[^>]*disabled[^>]*>确认并提交到 GitHub</button>", response.text)


def test_edit_saves_a_new_revision_and_keeps_the_old_one(app_client, answered_run, workspace):
    draft_id = make_draft(app_client, answered_run["run"]["run_id"]).json()["draft_id"]
    login(app_client)
    token = csrf_of(app_client.get(f"/review/{draft_id}").text)

    response = app_client.post(
        f"/review/{draft_id}",
        data={"csrf_token": token, "revision": 1, "title": "make setup 找不到解释器", "body": "新的正文"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = app_client.get(response.headers["location"]).text
    assert "已保存为第 2 版" in page

    rows = revisions(workspace, draft_id)
    assert [(row["revision"], row["author"]) for row in rows] == [(1, "template"), (2, "operator")]
    assert rows[1]["title"] == "make setup 找不到解释器"


def test_unchanged_save_adds_no_revision(app_client, answered_run, workspace):
    draft_id = make_draft(app_client, answered_run["run"]["run_id"]).json()["draft_id"]
    login(app_client)
    page = app_client.get(f"/review/{draft_id}").text
    title = re.search(r'name="title" value="([^"]*)"', page).group(1)
    body = re.search(r'<textarea id="body" name="body" required>(.*?)</textarea>', page, re.S).group(1)
    import html

    app_client.post(
        f"/review/{draft_id}",
        data={"csrf_token": csrf_of(page), "revision": 1, "title": html.unescape(title), "body": html.unescape(body)},
    )
    assert len(revisions(workspace, draft_id)) == 1


def test_edit_without_the_csrf_token_is_refused(app_client, answered_run, workspace):
    draft_id = make_draft(app_client, answered_run["run"]["run_id"]).json()["draft_id"]
    login(app_client)
    for token in ("", "forged-token"):
        response = app_client.post(
            f"/review/{draft_id}", data={"csrf_token": token, "revision": 1, "title": "x", "body": "y"}
        )
        assert response.status_code == 403
    assert len(revisions(workspace, draft_id)) == 1


def test_edit_on_an_old_revision_is_refused(app_client, answered_run, workspace):
    draft_id = make_draft(app_client, answered_run["run"]["run_id"]).json()["draft_id"]
    login(app_client)
    token = csrf_of(app_client.get(f"/review/{draft_id}").text)
    app_client.post(f"/review/{draft_id}", data={"csrf_token": token, "revision": 1, "title": "第二版", "body": "b2"})

    # A second tab still showing revision 1 saves over it: refused, nothing lost.
    stale = app_client.post(f"/review/{draft_id}", data={"csrf_token": token, "revision": 1, "title": "旧页面", "body": "b"})
    assert stale.status_code == 409
    rows = revisions(workspace, draft_id)
    assert [row["title"] for row in rows][-1] == "第二版"
    assert len(rows) == 2


def test_text_is_locked_once_submission_starts(app_client, answered_run, workspace):
    draft_id = make_draft(app_client, answered_run["run"]["run_id"]).json()["draft_id"]
    login(app_client)
    token = csrf_of(app_client.get(f"/review/{draft_id}").text)

    connection = connect(workspace["settings"].db_path)
    with connection:
        connection.execute("UPDATE issue_drafts SET status = 'submitting' WHERE draft_id = ?", (draft_id,))
    connection.close()

    response = app_client.post(f"/review/{draft_id}", data={"csrf_token": token, "revision": 1, "title": "x", "body": "y"})
    assert response.status_code == 409
    assert len(revisions(workspace, draft_id)) == 1
    page = app_client.get(f"/review/{draft_id}").text
    assert '<textarea' not in page  # read-only view


def test_page_escapes_what_users_and_models_wrote(app_client, answered_run):
    draft_id = make_draft(
        app_client, answered_run["run"]["run_id"], environment='<script>alert(1)</script><img src=x onerror="x">'
    ).json()["draft_id"]
    login(app_client)
    page = app_client.get(f"/review/{draft_id}").text
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page


def test_expired_session_must_log_in_again(app_client, answered_run, workspace):
    draft_id = make_draft(app_client, answered_run["run"]["run_id"]).json()["draft_id"]
    login(app_client)
    connection = connect(workspace["settings"].db_path)
    with connection:
        connection.execute("UPDATE operator_sessions SET expires_at = '2000-01-01T00:00:00+00:00'")
    connection.close()
    response = app_client.get(f"/review/{draft_id}", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_logout_ends_the_session(app_client, answered_run):
    draft_id = make_draft(app_client, answered_run["run"]["run_id"]).json()["draft_id"]
    login(app_client)
    token = csrf_of(app_client.get(f"/review/{draft_id}").text)
    cookie = app_client.cookies.get("dka_session")
    app_client.post("/logout", data={"csrf_token": token})

    # Replaying the old cookie no longer works: the session row is gone.
    app_client.cookies.set("dka_session", cookie)
    response = app_client.get(f"/review/{draft_id}", follow_redirects=False)
    assert response.status_code == 303
