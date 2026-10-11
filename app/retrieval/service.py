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


# Reciprocal rank fusion of the question's and the translation's results: each
# list is searched this deep, and a chunk scores 1 / (RRF_K + rank) in every
# list it appears in. 60 is the usual constant from the RRF paper.
FUSION_DEPTH = 20
RRF_K = 60


class NoActiveIndex(Exception):
    """No index version has been activated yet."""


class EncoderMismatch(Exception):
    """The query encoder is not the model the active index was built with."""


class UnknownRepo(Exception):
    def __init__(self, repo_id: str, known: list[str]) -> None:
        super().__init__(f"unknown repo_id {repo_id!r}; this index has {known}")
        self.repo_id = repo_id
        self.known = known


def search(collection, query_embedding: list[float], repo_id: str | None, top_k: int) -> list[dict]:
    """
    The top_k nearest chunks, closest first: in one repository, or across all
    of them when repo_id is None.

    Across repositories this is one shared ranking. Searching each repository
    on its own and taking turns was measured on demo20 and was worse (@3 45% ->
    26%, @5 47% -> 45%): it gives half the slots to a repository the question is
    not about. See evals/experiments/demo20_scope.py.
    """
    where = {"repo_id": repo_id} if repo_id is not None else None
    result = collection.query(
        query_embeddings=[query_embedding],
        n_results=top_k,
        where=where,
        include=["documents", "metadatas", "distances"],
    )
    hits = []
    for index in range(len(result["ids"][0])):
        hits.append(
            {
                "chunk_id": result["ids"][0][index],
                "metadata": result["metadatas"][0][index],
                "content": result["documents"][0][index],
                "distance": result["distances"][0][index],
            }
        )
    return hits


def fuse(result_lists: list[list[dict]], top_k: int) -> list[dict]:
    """
    Reciprocal rank fusion. A chunk found by both searches keeps the smaller of
    its two distances, so the reported score is still a real cosine distance.
    """
    scores = {}
    best_hit = {}
    for hits in result_lists:
        for rank, hit in enumerate(hits, start=1):
            chunk_id = hit["chunk_id"]
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (RRF_K + rank)
            if chunk_id not in best_hit or hit["distance"] < best_hit[chunk_id]["distance"]:
                best_hit[chunk_id] = hit
    ordered = sorted(scores, key=lambda chunk_id: scores[chunk_id], reverse=True)
    fused = []
    for chunk_id in ordered[:top_k]:
        fused.append(best_hit[chunk_id])
    return fused


def retrieve(
    connection: sqlite3.Connection,
    chroma_client,
    encoder: Encoder,
    query: str,
    repo_id: str | None,
    top_k: int,
    request_id: str,
    query_en: str | None = None,
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
    if query_en is None:
        hits = search(collection, encoder.encode_query(query), repo_id, top_k)
    else:
        query_hits = search(collection, encoder.encode_query(query), repo_id, FUSION_DEPTH)
        translation_hits = search(collection, encoder.encode_query(query_en), repo_id, FUSION_DEPTH)
        hits = fuse([query_hits, translation_hits], top_k)

    chunks = []
    for rank, hit in enumerate(hits, start=1):
        meta = hit["metadata"]
        remote = manifest[meta["repo_id"]]["remote"]
        chunks.append(
            {
                "chunk_id": hit["chunk_id"],
                "rank": rank,
                "repo_id": meta["repo_id"],
                "commit_sha": meta["commit_sha"],
                "path": meta["path"],
                "start_line": meta["start_line"],
                "end_line": meta["end_line"],
                "source_type": meta["source_type"],
                "symbol": meta["symbol"] or None,
                "content": hit["content"],
                "url": source_url(remote, meta["commit_sha"], meta["path"], meta["start_line"], meta["end_line"]),
                "score": {"type": "cosine_distance", "value": hit["distance"]},
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
            INSERT INTO runs (run_id, request_id, created_at, query, query_en, repo_filter, index_version,
                              embed_model, top_k, status, latency_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                request_id,
                datetime.now(UTC).isoformat(),
                query,
                query_en,
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
                """
                INSERT INTO run_chunks (run_id, rank, chunk_id, distance, repo_id, commit_sha,
                                        path, start_line, end_line, url, content)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    chunk["rank"],
                    chunk["chunk_id"],
                    chunk["score"]["value"],
                    chunk["repo_id"],
                    chunk["commit_sha"],
                    chunk["path"],
                    chunk["start_line"],
                    chunk["end_line"],
                    chunk["url"],
                    chunk["content"],
                ),
            )

    return {
        "run_id": run_id,
        "status": status,
        "index_version": version,
        "repos": repos_in_scope,
        "chunks": chunks,
        "latency_ms": round(latency_ms, 1),
    }
