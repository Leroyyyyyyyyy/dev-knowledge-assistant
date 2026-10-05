"""
Build one complete index version from the pinned commits in config/repos.json.

    uv run python -m app.indexing.build              # build, validate, activate
    uv run python -m app.indexing.build --no-activate

Order matters: the new version is fully written and checked before the active
pointer moves, so queries keep using the previous version during a build and a
failed build changes nothing they can see.
"""

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import chromadb

from app.indexing.snapshot import export_commit, load_repos
from app.retrieval.chunker import chunk_repository
from app.retrieval.encoder import Encoder
from app.settings import Settings
from app.storage.db import connect, init_db

DOC_SUFFIXES = {".md", ".txt"}
PY_SYMBOL = re.compile(r"^(?:async\s+def|def|class)\s+([A-Za-z_][A-Za-z0-9_]*)")


class BuildInProgress(Exception):
    """Another build holds the 'building' slot."""


def collection_name(version: str) -> str:
    return f"idx-{version}"


def new_version_id() -> str:
    return "v" + datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def symbol_of(path: str, content: str) -> str:
    """Function or class name for a Python chunk that starts with one, else ''."""
    if not path.endswith(".py"):
        return ""
    first_line = content.split("\n", 1)[0]
    match = PY_SYMBOL.match(first_line)
    if match:
        return match.group(1)
    return ""


def make_records(repo_id: str, repo: dict, snapshot: Path, version: str) -> list[dict]:
    """Chunk one repo snapshot and attach the metadata every citation needs (spec §4.2)."""
    commit = repo["commit"]
    records = []
    for chunk in chunk_repository(snapshot, repo_id):
        path = chunk["filepath"]
        content = chunk["content"]
        suffix = Path(path).suffix
        records.append(
            {
                # Stable for the same commit and line range, different across commits.
                "chunk_id": f"{repo_id}@{commit[:12]}:{path}#L{chunk['start_line']}-L{chunk['end_line']}",
                "content": content,
                "metadata": {
                    "repo_id": repo_id,
                    "commit_sha": commit,
                    "index_version": version,
                    "path": path,
                    "start_line": chunk["start_line"],
                    "end_line": chunk["end_line"],
                    "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    "source_type": "docs" if suffix in DOC_SUFFIXES else "code",
                    # Chroma metadata cannot hold None; '' means "no symbol".
                    "symbol": symbol_of(path, content),
                },
            }
        )
    return records


def write_collection(client, version: str, records: list[dict], encoder: Encoder) -> None:
    collection = client.create_collection(
        name=collection_name(version),
        metadata={"hnsw:space": "cosine", "embed_model": encoder.model_name},
    )
    texts = []
    for record in records:
        texts.append(record["content"])
    vectors = encoder.encode_passages(texts)

    batch = 500
    for start in range(0, len(records), batch):
        part = records[start : start + batch]
        ids = []
        documents = []
        metadatas = []
        for record in part:
            ids.append(record["chunk_id"])
            documents.append(record["content"])
            metadatas.append(record["metadata"])
        collection.add(
            ids=ids,
            documents=documents,
            embeddings=vectors[start : start + batch],
            metadatas=metadatas,
        )


def validate_collection(client, version: str, records: list[dict], repos: dict) -> None:
    """Refuse to activate an index that is empty, short, or missing a repo."""
    collection = client.get_collection(collection_name(version))
    if collection.count() != len(records):
        raise RuntimeError(f"collection has {collection.count()} chunks, expected {len(records)}")

    per_repo = {}
    for record in records:
        repo_id = record["metadata"]["repo_id"]
        per_repo[repo_id] = per_repo.get(repo_id, 0) + 1
    for repo_id in repos:
        if per_repo.get(repo_id, 0) == 0:
            raise RuntimeError(f"{repo_id} produced no chunks; check its commit and exclude list")


def activate_version(connection: sqlite3.Connection, version: str) -> None:
    """Point queries at `version`. Only a 'ready' version can be activated."""
    with connection:
        row = connection.execute(
            "SELECT status FROM index_versions WHERE version = ?", (version,)
        ).fetchone()
        if row is None or row["status"] != "ready":
            raise ValueError(f"version {version} is not ready")
        connection.execute(
            """
            INSERT INTO active_index (id, version, switched_at) VALUES (1, ?, ?)
            ON CONFLICT (id) DO UPDATE SET version = excluded.version, switched_at = excluded.switched_at
            """,
            (version, now_iso()),
        )


def build_index_version(settings: Settings, encoder: Encoder, activate: bool = True) -> str:
    init_db(settings.db_path)
    repos = load_repos(settings.repos_config)
    manifest = {}
    for repo_id, repo in repos.items():
        manifest[repo_id] = {"commit": repo["commit"], "remote": repo["remote"], "exclude": repo["exclude"]}

    version = new_version_id()
    connection = connect(settings.db_path)
    try:
        try:
            with connection:
                connection.execute(
                    """
                    INSERT INTO index_versions (version, status, manifest, embed_model, created_at)
                    VALUES (?, 'building', ?, ?, ?)
                    """,
                    (version, json.dumps(manifest), encoder.model_name, now_iso()),
                )
        except sqlite3.IntegrityError as exc:
            raise BuildInProgress("another index build is in progress") from exc

        client = chromadb.PersistentClient(path=str(settings.chroma_dir))
        try:
            records = []
            for repo_id, repo in repos.items():
                snapshot = export_commit(repo_id, repo, settings.snapshot_dir, settings.checkout_base)
                records.extend(make_records(repo_id, repo, snapshot, version))
            write_collection(client, version, records, encoder)
            validate_collection(client, version, records, repos)
        except Exception as exc:
            with connection:
                connection.execute(
                    "UPDATE index_versions SET status = 'failed', finished_at = ?, error = ? WHERE version = ?",
                    (now_iso(), f"{type(exc).__name__}: {exc}", version),
                )
            try:
                client.delete_collection(collection_name(version))
            except Exception:
                pass
            raise

        with connection:
            connection.execute(
                "UPDATE index_versions SET status = 'ready', chunk_count = ?, finished_at = ? WHERE version = ?",
                (len(records), now_iso(), version),
            )
        if activate:
            activate_version(connection, version)
    finally:
        connection.close()
    return version


def main() -> None:
    from app.retrieval.encoder import SentenceTransformerEncoder

    parser = argparse.ArgumentParser(description="Build a complete index version from pinned commits.")
    parser.add_argument("--no-activate", action="store_true", help="build and validate, but do not switch to it")
    args = parser.parse_args()

    settings = Settings()
    encoder = SentenceTransformerEncoder(settings.embed_model, settings.embed_batch_size)
    try:
        version = build_index_version(settings, encoder, activate=not args.no_activate)
    except BuildInProgress as exc:
        sys.exit(str(exc))
    state = "built" if args.no_activate else "built and activated"
    print(f"index version {version} {state}")


if __name__ == "__main__":
    main()
