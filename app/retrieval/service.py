"""
One retrieval run: bind to the active index version, search, record what was returned.

Everything a later step needs to check a citation (M2 /api/answers) is written
here: which version was searched and which chunk ids came back at which rank.
"""

import json
import sqlite3
import time
import uuid
from datetime import UTC, datetime

from app.citations import source_url
from app.indexing.build import collection_name
from app.retrieval.encoder import Encoder
from app.storage.db import active_version


class NoActiveIndex(Exception):
    """No index version has been activated yet."""


class EncoderMismatch(Exception):
    """The query encoder is not the model the active index was built with."""


class UnknownRepo(Exception):
    def __init__(self, repo_id: str, known: list[str]) -> None:
        super().__init__(f"unknown repo_id {repo_id!r}; this index has {known}")
        self.repo_id = repo_id
        self.known = known


def retrieve(
    connection: sqlite3.Connection,
    chroma_client,
    encoder: Encoder,
    query: str,
    repo_id: str | None,
    top_k: int,
    request_id: str,
) -> dict:
    started = time.perf_counter()

    # Read the active version once. Everything below uses this version only, even
    # if a build switches the pointer while this request is running.
    version_row = active_version(connection)
    if version_row is None:
        raise NoActiveIndex("no index version is active; run python -m app.indexing.build")
    version = version_row["version"]
    manifest = json.loads(version_row["manifest"])

    # Vectors from different models live in different spaces. Comparing them does
    # not fail; it silently returns the wrong chunks. So refuse instead.
    if version_row["embed_model"] != encoder.model_name:
        raise EncoderMismatch(
            f"index {version} was built with {version_row['embed_model']}, "
            f"the service is running {encoder.model_name}"
        )

    if repo_id is not None and repo_id not in manifest:
        raise UnknownRepo(repo_id, sorted(manifest))

    collection = chroma_client.get_collection(collection_name(version))
    where = {"repo_id": repo_id} if repo_id is not None else None
    result = collection.query(
        query_embeddings=[encoder.encode_query(query)],
        n_results=top_k,
        where=where,
        include=["documents", "metadatas", "distances"],
    )

    chunks = []
    for index in range(len(result["ids"][0])):
        meta = result["metadatas"][0][index]
        remote = manifest[meta["repo_id"]]["remote"]
        chunks.append(
            {
                "chunk_id": result["ids"][0][index],
                "rank": index + 1,
                "repo_id": meta["repo_id"],
                "commit_sha": meta["commit_sha"],
                "path": meta["path"],
                "start_line": meta["start_line"],
                "end_line": meta["end_line"],
                "source_type": meta["source_type"],
                "symbol": meta["symbol"] or None,
                "content": result["documents"][0][index],
                "url": source_url(remote, meta["commit_sha"], meta["path"], meta["start_line"], meta["end_line"]),
                "score": {"type": "cosine_distance", "value": result["distances"][0][index]},
            }
        )

    # 'no_evidence' only means nothing came back. Whether what came back is enough
    # to answer is decided at the answer layer, not by a similarity threshold here.
    repos_in_scope = {}
    for scope_repo_id, entry in manifest.items():
        if repo_id is None or scope_repo_id == repo_id:
            repos_in_scope[scope_repo_id] = {"commit_sha": entry["commit"], "remote": entry["remote"]}

    status = "ok" if chunks else "no_evidence"
    latency_ms = (time.perf_counter() - started) * 1000
    run_id = str(uuid.uuid4())

    with connection:
        connection.execute(
            """
            INSERT INTO runs (run_id, request_id, created_at, query, repo_filter, index_version,
                              embed_model, top_k, status, latency_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                request_id,
                datetime.now(UTC).isoformat(),
                query,
                repo_id,
                version,
                version_row["embed_model"],
                top_k,
                status,
                latency_ms,
            ),
        )
        for chunk in chunks:
            connection.execute(
                "INSERT INTO run_chunks (run_id, rank, chunk_id, distance) VALUES (?, ?, ?, ?)",
                (run_id, chunk["rank"], chunk["chunk_id"], chunk["score"]["value"]),
            )

    return {
        "run_id": run_id,
        "status": status,
        "index_version": version,
        "repos": repos_in_scope,
        "chunks": chunks,
        "latency_ms": round(latency_ms, 1),
    }
