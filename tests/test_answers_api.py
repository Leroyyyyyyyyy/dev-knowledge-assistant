import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from app.indexing.build import build_index_version
from app.storage.db import SCHEMA, SCHEMA_VERSION, connect, init_db
from tests.conftest import AUTH


@pytest.fixture
def run(client, workspace, encoder) -> dict:
    """One real retrieval run to answer against: both alpha chunks, none from beta."""
    build_index_version(workspace["settings"], encoder)
    response = client.post(
        "/api/retrieve", json={"query": "ZebraTransitionError cancel", "repo_id": "alpha", "top_k": 2}, headers=AUTH
    )
    assert response.status_code == 200
    return response.json()


def answer(client, run_id: str, **overrides) -> "object":
    body = {
        "run_id": run_id,
        "status": "answered",
        "answer_text": "cancel_order raises ZebraTransitionError.",
        "cited_chunk_ids": [],
        "model": "test-model",
        "prompt_version": "p1",
    }
    body.update(overrides)
    return client.post("/api/answers", json=body, headers=AUTH)


def stored_answers(workspace, run_id: str) -> list[sqlite3.Row]:
    connection = connect(workspace["settings"].db_path)
    rows = connection.execute(
        "SELECT validation, rejection_code FROM answers WHERE run_id = ? ORDER BY created_at", (run_id,)
    ).fetchall()
    connection.close()
    return rows


def test_accepts_answer_citing_this_runs_chunks(client, run):
    top = run["chunks"][0]
    text = f"cancel_order raises ZebraTransitionError, see {top['url']}。"
    response = answer(client, run["run_id"], answer_text=text, cited_chunk_ids=[top["chunk_id"]])
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "answered"
    assert body["index_version"] == run["index_version"]
    assert body["citations"] == [
        {
            "chunk_id": top["chunk_id"],
            "rank": top["rank"],
            "repo_id": top["repo_id"],
            "commit_sha": top["commit_sha"],
            "path": top["path"],
            "start_line": top["start_line"],
            "end_line": top["end_line"],
            "url": top["url"],
        }
    ]


def test_rejects_a_made_up_chunk_id(client, workspace, run):
    response = answer(client, run["run_id"], cited_chunk_ids=["alpha@000000000000:service/orders.py#L1-L2"])
    assert response.status_code == 422
    assert response.json()["code"] == "UNKNOWN_CITATION"
    rows = stored_answers(workspace, run["run_id"])
    assert [(row["validation"], row["rejection_code"]) for row in rows] == [("rejected", "UNKNOWN_CITATION")]


def test_rejects_a_real_chunk_from_another_run(client, run):
    other = client.post(
        "/api/retrieve", json={"query": "walrus dedupe event", "repo_id": "beta", "top_k": 1}, headers=AUTH
    ).json()
    foreign_chunk = other["chunks"][0]["chunk_id"]
    assert foreign_chunk not in [chunk["chunk_id"] for chunk in run["chunks"]]

    response = answer(client, run["run_id"], cited_chunk_ids=[foreign_chunk])
    assert response.status_code == 422
    assert response.json()["code"] == "UNKNOWN_CITATION"


def test_rejects_answered_without_citations(client, run):
    response = answer(client, run["run_id"], cited_chunk_ids=[])
    assert response.status_code == 422
    assert response.json()["code"] == "CITATION_REQUIRED"


def test_insufficient_evidence_needs_no_citation(client, run):
    response = answer(
        client, run["run_id"], status="insufficient_evidence", answer_text="资料里没有找到依据。", cited_chunk_ids=[]
    )
    assert response.status_code == 200
    assert response.json()["citations"] == []


@pytest.mark.parametrize(
    "link",
    [
        "https://github.com/example/alpha/blob/main/service/orders.py#L1-L2",  # branch, not commit
        "https://example.com/somewhere",
    ],
)
def test_rejects_links_the_backend_did_not_issue(client, run, link):
    top = run["chunks"][0]
    response = answer(client, run["run_id"], answer_text=f"See {link}", cited_chunk_ids=[top["chunk_id"]])
    assert response.status_code == 422
    assert response.json()["code"] == "FABRICATED_LINK"


def test_rejects_a_link_to_an_uncited_chunk(client, run):
    cited, uncited = run["chunks"][0], run["chunks"][1]
    response = answer(client, run["run_id"], answer_text=f"See {uncited['url']}", cited_chunk_ids=[cited["chunk_id"]])
    assert response.status_code == 422
    assert response.json()["code"] == "FABRICATED_LINK"


def test_rejects_duplicate_citations(client, run):
    top = run["chunks"][0]["chunk_id"]
    response = answer(client, run["run_id"], cited_chunk_ids=[top, top])
    assert response.status_code == 422
    assert response.json()["code"] == "DUPLICATE_CITATION"


def test_unknown_run_is_404(client, run):
    response = answer(client, "00000000-0000-4000-8000-000000000000", cited_chunk_ids=[])
    assert response.status_code == 404
    assert response.json()["code"] == "RUN_NOT_FOUND"


def test_expired_run_is_410(client, workspace, run):
    connection = connect(workspace["settings"].db_path)
    with connection:
        old = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        connection.execute("UPDATE runs SET created_at = ? WHERE run_id = ?", (old, run["run_id"]))
    connection.close()
    response = answer(client, run["run_id"], cited_chunk_ids=[run["chunks"][0]["chunk_id"]])
    assert response.status_code == 410
    assert response.json()["code"] == "RUN_EXPIRED"


def test_retry_of_the_same_answer_returns_the_original(client, workspace, run):
    cited = [run["chunks"][0]["chunk_id"]]
    first = answer(client, run["run_id"], cited_chunk_ids=cited)
    second = answer(client, run["run_id"], cited_chunk_ids=cited)
    assert first.status_code == second.status_code == 200
    assert first.json()["answer_id"] == second.json()["answer_id"]
    assert len(stored_answers(workspace, run["run_id"])) == 1


def test_a_different_second_answer_is_a_conflict(client, run):
    cited = [run["chunks"][0]["chunk_id"]]
    assert answer(client, run["run_id"], cited_chunk_ids=cited).status_code == 200
    response = answer(client, run["run_id"], answer_text="something else", cited_chunk_ids=cited)
    assert response.status_code == 409
    assert response.json()["code"] == "ANSWER_ALREADY_ACCEPTED"


def test_a_rejected_attempt_can_be_followed_by_a_fixed_one(client, workspace, run):
    """Dify gets one format fix: the rejected try is kept, the fixed one is accepted."""
    bad = answer(client, run["run_id"], cited_chunk_ids=["made-up"])
    good = answer(client, run["run_id"], cited_chunk_ids=[run["chunks"][0]["chunk_id"]])
    assert (bad.status_code, good.status_code) == (422, 200)
    rows = stored_answers(workspace, run["run_id"])
    assert [row["validation"] for row in rows] == ["rejected", "accepted"]


def test_answers_require_the_service_token(client, run):
    response = client.post("/api/answers", json={"run_id": run["run_id"]}, headers={})
    assert response.status_code == 401


def test_schema_v1_database_is_migrated(tmp_path):
    db_path = tmp_path / "old.sqlite3"
    legacy = sqlite3.connect(db_path)
    legacy.executescript(SCHEMA)  # what an M1 database looks like
    legacy.close()

    init_db(db_path)
    init_db(db_path)  # running it again must be a no-op

    connection = connect(db_path)
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    columns = [row["name"] for row in connection.execute("PRAGMA table_info(run_chunks)")]
    tables = [row["name"] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    connection.close()
    assert version == SCHEMA_VERSION
    assert "url" in columns and "answers" in tables


def test_links_stop_at_chinese_text_and_punctuation():
    from app.answers import links_in

    text = "文档在 http://127.0.0.1:8000/docs，以及造数命令；见 https://github.com/o/r/blob/abc/a.py#L1-L2。"
    assert links_in(text) == ["http://127.0.0.1:8000/docs", "https://github.com/o/r/blob/abc/a.py#L1-L2"]


def test_accepts_a_url_quoted_from_a_cited_chunk(client, workspace, encoder):
    """Found with real DeepSeek output: quoting a docs URL from a README is not a fabricated link."""
    build_index_version(workspace["settings"], encoder)
    run = client.post(
        "/api/retrieve", json={"query": "API docs served", "repo_id": "alpha", "top_k": 3}, headers=AUTH
    ).json()
    docs_chunk = [chunk for chunk in run["chunks"] if chunk["path"] == "docs/api.md"][0]
    other_chunk = [chunk for chunk in run["chunks"] if chunk["path"] != "docs/api.md"][0]
    text = "启动后，交互式文档在 http://127.0.0.1:8000/docs，以及可选的造数命令。"

    rejected = answer(client, run["run_id"], answer_text=text, cited_chunk_ids=[other_chunk["chunk_id"]])
    assert rejected.status_code == 422
    assert rejected.json()["code"] == "FABRICATED_LINK"  # the URL is in a chunk that was not cited

    accepted = answer(client, run["run_id"], answer_text=text, cited_chunk_ids=[docs_chunk["chunk_id"]])
    assert accepted.status_code == 200
