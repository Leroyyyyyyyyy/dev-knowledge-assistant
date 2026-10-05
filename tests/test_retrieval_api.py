import json
import sqlite3

import chromadb
import pytest

from app.citations import source_url
from app.indexing.build import BuildInProgress, build_index_version, collection_name
from app.storage.db import active_version, connect, init_db
from tests.conftest import AUTH, FakeEncoder


def build(workspace, encoder) -> str:
    return build_index_version(workspace["settings"], encoder)


# --- health and readiness -------------------------------------------------


def test_health_reveals_nothing(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_only_after_an_index_is_active(client, workspace, encoder):
    before = client.get("/ready")
    assert before.status_code == 503
    assert before.json()["checks"]["active_index"] is False

    build(workspace, encoder)
    after = client.get("/ready")
    assert after.status_code == 200
    assert after.json() == {"status": "ready", "checks": {"database": True, "active_index": True, "encoder": True}}


# --- auth and input validation --------------------------------------------


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong-token-0123456789"}, {"Authorization": "test-token-0123456789"}])
def test_retrieve_rejects_missing_or_wrong_token(client, headers):
    response = client.post("/api/retrieve", json={"query": "anything"}, headers=headers)
    assert response.status_code == 401
    body = response.json()
    assert body["code"] == "UNAUTHORIZED"
    assert body["request_id"] == response.headers["X-Request-ID"]


@pytest.mark.parametrize(
    "payload",
    [
        {"query": ""},
        {"query": "   "},
        {"query": "x", "top_k": 0},
        {"query": "x", "top_k": 11},
        {"query": "x", "topk": 3},
        {"query": "x", "repo_id": "../etc"},
    ],
)
def test_retrieve_rejects_bad_input(client, workspace, encoder, payload):
    build(workspace, encoder)
    response = client.post("/api/retrieve", json=payload, headers=AUTH)
    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_REQUEST"


def test_unknown_repo_is_a_client_error(client, workspace, encoder):
    build(workspace, encoder)
    response = client.post("/api/retrieve", json={"query": "x", "repo_id": "gamma"}, headers=AUTH)
    assert response.status_code == 422
    assert response.json()["code"] == "UNKNOWN_REPO"


def test_no_active_index_is_a_dependency_error(client):
    response = client.post("/api/retrieve", json={"query": "x"}, headers=AUTH)
    assert response.status_code == 503
    assert response.json()["code"] == "NO_ACTIVE_INDEX"


# --- retrieval results and citations ---------------------------------------


def test_retrieve_returns_commit_pinned_citations(client, workspace, encoder):
    version = build(workspace, encoder)
    response = client.post("/api/retrieve", json={"query": "ZebraTransitionError cancel"}, headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["index_version"] == version

    top = body["chunks"][0]
    commit = workspace["repos"]["alpha"]["commit"]
    assert top["repo_id"] == "alpha"
    assert top["path"] == "service/orders.py"
    assert top["commit_sha"] == commit
    assert top["symbol"] == "cancel_order"
    assert top["score"]["type"] == "cosine_distance"
    assert top["url"] == f"https://github.com/example/alpha/blob/{commit}/service/orders.py#L1-L2"


def test_index_reads_the_commit_not_the_working_tree(client, workspace, encoder):
    version = build(workspace, encoder)
    chroma = chromadb.PersistentClient(path=str(workspace["settings"].chroma_dir))
    documents = chroma.get_collection(collection_name(version)).get(include=["documents", "metadatas"])

    all_text = "\n".join(documents["documents"])
    assert "ZebraTransitionError" in all_text  # committed content is there
    assert "uncommittedmarker" not in all_text  # the working-tree edit is not

    paths = set()
    for meta in documents["metadatas"]:
        paths.add(meta["path"])
    assert "notes/private.md" not in paths  # excluded by config


def test_path_with_spaces_and_chinese_is_encoded(client, workspace, encoder):
    build(workspace, encoder)
    response = client.post("/api/retrieve", json={"query": "quokka installer", "repo_id": "alpha"}, headers=AUTH)
    top = response.json()["chunks"][0]
    assert top["path"] == "docs/使用 说明.md"
    assert top["source_type"] == "docs"
    assert "/blob/" in top["url"] and "%E4%BD%BF%E7%94%A8%20%E8%AF%B4%E6%98%8E.md#L1-L3" in top["url"]


def test_repo_filter_limits_results(client, workspace, encoder):
    build(workspace, encoder)
    response = client.post("/api/retrieve", json={"query": "walrus dedupe", "repo_id": "beta", "top_k": 10}, headers=AUTH)
    body = response.json()
    assert list(body["repos"]) == ["beta"]
    for chunk in body["chunks"]:
        assert chunk["repo_id"] == "beta"


def test_run_is_recorded_with_its_chunks(client, workspace, encoder):
    version = build(workspace, encoder)
    body = client.post("/api/retrieve", json={"query": "walrus dedupe event"}, headers=AUTH).json()

    connection = connect(workspace["settings"].db_path)
    run = connection.execute("SELECT * FROM runs WHERE run_id = ?", (body["run_id"],)).fetchone()
    rows = connection.execute(
        "SELECT rank, chunk_id FROM run_chunks WHERE run_id = ? ORDER BY rank", (body["run_id"],)
    ).fetchall()
    connection.close()

    assert run["index_version"] == version
    assert run["embed_model"] == "fake-hash-encoder"
    returned = []
    for chunk in body["chunks"]:
        returned.append((chunk["rank"], chunk["chunk_id"]))
    recorded = []
    for row in rows:
        recorded.append((row["rank"], row["chunk_id"]))
    assert recorded == returned


def test_encoder_mismatch_refuses_to_answer(workspace):
    from fastapi.testclient import TestClient

    from app.main import create_app

    build_index_version(workspace["settings"], FakeEncoder("model-a"))
    chroma = chromadb.PersistentClient(path=str(workspace["settings"].chroma_dir))
    app = create_app(workspace["settings"], encoder=FakeEncoder("model-b"), chroma_client=chroma)
    with TestClient(app) as client:
        response = client.post("/api/retrieve", json={"query": "x"}, headers=AUTH)
        assert response.status_code == 503
        assert response.json()["code"] == "INDEX_ENCODER_MISMATCH"
        assert client.get("/ready").json()["checks"]["encoder"] is False


# --- index versions --------------------------------------------------------


def test_failed_build_keeps_the_previous_version_active(workspace, encoder):
    first = build(workspace, encoder)

    repos = json.loads(json.dumps(workspace["repos"]))
    repos["beta"]["commit"] = "0" * 40  # not in the clone
    workspace["config_path"].write_text(json.dumps({"repos": repos}), encoding="utf-8")

    with pytest.raises(ValueError):
        build(workspace, encoder)

    connection = connect(workspace["settings"].db_path)
    assert active_version(connection)["version"] == first
    statuses = connection.execute("SELECT status FROM index_versions ORDER BY created_at").fetchall()
    connection.close()
    recorded = []
    for row in statuses:
        recorded.append(row["status"])
    assert recorded == ["ready", "failed"]


def test_second_concurrent_build_is_rejected(workspace, encoder):
    settings = workspace["settings"]
    init_db(settings.db_path)
    connection = connect(settings.db_path)
    with connection:
        connection.execute(
            "INSERT INTO index_versions (version, status, manifest, embed_model, created_at) "
            "VALUES ('v-other', 'building', '{}', 'x', 'now')"
        )
    connection.close()

    with pytest.raises(BuildInProgress):
        build(workspace, encoder)


def test_only_one_build_slot_in_the_schema(workspace):
    settings = workspace["settings"]
    init_db(settings.db_path)
    connection = connect(settings.db_path)
    insert = (
        "INSERT INTO index_versions (version, status, manifest, embed_model, created_at) "
        "VALUES (?, 'building', '{}', 'x', 'now')"
    )
    connection.execute(insert, ("v1",))
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(insert, ("v2",))
    connection.close()


# --- citation builder -------------------------------------------------------


def test_source_url_rules():
    sha = "a" * 40
    assert source_url("https://github.com/o/r", sha, "a b/c#d.py", 3, 3) == f"https://github.com/o/r/blob/{sha}/a%20b/c%23d.py#L3"
    with pytest.raises(ValueError):
        source_url("https://github.com/o/r", "abc123", "x.py", 1, 2)
    with pytest.raises(ValueError):
        source_url("https://gitlab.com/o/r", sha, "x.py", 1, 2)
