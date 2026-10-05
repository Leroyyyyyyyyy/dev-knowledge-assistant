import pytest

from app.indexing.build import build_index_version
from app.storage.db import connect
from tests.conftest import AUTH


@pytest.fixture
def answered_run(client, workspace, encoder) -> dict:
    """A retrieval run with an accepted answer, as the user would have seen it."""
    build_index_version(workspace["settings"], encoder)
    run = client.post("/api/retrieve", json={"query": "ZebraTransitionError", "repo_id": "alpha"}, headers=AUTH).json()
    answer = client.post(
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
    assert answer.status_code == 200
    return {"run_id": run["run_id"], "answer_id": answer.json()["answer_id"]}


def feedback(client, **body):
    return client.post("/api/feedback", json=body, headers=AUTH)


def feedback_rows(workspace, run_id: str) -> list:
    connection = connect(workspace["settings"].db_path)
    rows = connection.execute("SELECT * FROM feedback WHERE run_id = ?", (run_id,)).fetchall()
    connection.close()
    return rows


def test_records_feedback_on_the_accepted_answer(client, answered_run):
    response = feedback(client, run_id=answered_run["run_id"], resolved=True)
    assert response.status_code == 200
    body = response.json()
    assert body["resolved"] is True
    assert body["answer_id"] == answered_run["answer_id"]
    assert body["reason"] is None


def test_unresolved_with_reason_and_comment(client, answered_run):
    response = feedback(
        client, run_id=answered_run["run_id"], resolved=False, reason="wrong_citation", comment="  链接打开不是这段  "
    )
    assert response.status_code == 200
    assert response.json()["reason"] == "wrong_citation"
    assert response.json()["comment"] == "链接打开不是这段"


def test_changing_your_mind_replaces_the_earlier_feedback(client, workspace, answered_run):
    first = feedback(client, run_id=answered_run["run_id"], resolved=True).json()
    second = feedback(client, run_id=answered_run["run_id"], resolved=False, reason="incomplete").json()
    rows = feedback_rows(workspace, answered_run["run_id"])
    assert len(rows) == 1
    assert rows[0]["resolved"] == 0 and rows[0]["reason"] == "incomplete"
    assert second["created_at"] == first["created_at"]
    assert second["updated_at"] >= first["updated_at"]


def test_user_verdict_is_stored_apart_from_the_models(client, workspace, answered_run):
    feedback(client, run_id=answered_run["run_id"], resolved=False, reason="wrong_answer")
    connection = connect(workspace["settings"].db_path)
    model_status = connection.execute(
        "SELECT status FROM answers WHERE answer_id = ?", (answered_run["answer_id"],)
    ).fetchone()["status"]
    connection.close()
    # The model said it answered; the user said it did not help. Both are kept.
    assert model_status == "answered"
    assert feedback_rows(workspace, answered_run["run_id"])[0]["resolved"] == 0


def test_unknown_run_is_404(client, answered_run):
    response = feedback(client, run_id="00000000-0000-4000-8000-000000000000", resolved=True)
    assert response.status_code == 404
    assert response.json()["code"] == "RUN_NOT_FOUND"


def test_run_without_an_accepted_answer_is_409(client, workspace, encoder):
    build_index_version(workspace["settings"], encoder)
    run = client.post("/api/retrieve", json={"query": "anything"}, headers=AUTH).json()
    # A rejected attempt does not count: the user never saw it as an answer.
    client.post(
        "/api/answers",
        json={"run_id": run["run_id"], "status": "answered", "answer_text": "x", "cited_chunk_ids": ["made-up"],
              "model": "m", "prompt_version": "p"},
        headers=AUTH,
    )
    response = feedback(client, run_id=run["run_id"], resolved=True)
    assert response.status_code == 409
    assert response.json()["code"] == "NO_ANSWER_FOR_RUN"


@pytest.mark.parametrize(
    "extra",
    [
        {"resolved": True, "reason": "wrong_answer"},  # reason only when unresolved
        {"resolved": False, "reason": "because"},  # not in the enum
        {"resolved": False, "comment": "x" * 1001},  # too long
        {"resolved": "maybe"},
        {"resolved": True, "score": 5},  # unknown field
    ],
)
def test_invalid_feedback_is_422(client, answered_run, extra):
    response = feedback(client, run_id=answered_run["run_id"], **extra)
    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_REQUEST"


def test_feedback_requires_the_service_token(client, answered_run):
    response = client.post("/api/feedback", json={"run_id": answered_run["run_id"], "resolved": True}, headers={})
    assert response.status_code == 401
