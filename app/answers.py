"""
Validate and record a generated answer against the retrieval run it came from.

What this proves: every cited chunk exists and was returned by *this* run, and
every link in the text is the backend's own link for a cited chunk. What it
does not prove: that the cited chunks support what the answer says. That is
checked by evaluation (spec §3.1, §8.2), not here.

Rejected submissions are stored too. They are the evidence for "the validator
stopped a fabricated citation" and feed the answer-layer evaluation.
"""

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta

# Anything that looks like a link. Trailing punctuation (ASCII and full-width)
# is trimmed so "见 https://...#L3-L9。" still matches the canonical link.
URL_PATTERN = re.compile(r"https?://[^\s<>\"'`\[\]()（）【】]+")
TRAILING_PUNCTUATION = ".,;:!?。，；：！？、」』"


class RunNotFound(Exception):
    pass


class RunExpired(Exception):
    pass


class AnswerRejected(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class AnswerConflict(Exception):
    """This run already has a different accepted answer."""


def links_in(text: str) -> list[str]:
    links = []
    for match in URL_PATTERN.findall(text):
        links.append(match.rstrip(TRAILING_PUNCTUATION))
    return links


def content_hash(status: str, answer_text: str, cited_chunk_ids: list[str], model: str, prompt_version: str) -> str:
    payload = json.dumps(
        {"status": status, "answer_text": answer_text, "cited": cited_chunk_ids, "model": model, "prompt": prompt_version},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def check_citations(status: str, answer_text: str, cited_chunk_ids: list[str], run_chunks: dict) -> list[dict]:
    """Return the cited chunks in citation order, or raise AnswerRejected."""
    seen = set()
    duplicates = []
    for chunk_id in cited_chunk_ids:
        if chunk_id in seen:
            duplicates.append(chunk_id)
        seen.add(chunk_id)
    if duplicates:
        raise AnswerRejected("DUPLICATE_CITATION", f"cited more than once: {duplicates}")

    if status == "answered" and not cited_chunk_ids:
        raise AnswerRejected("CITATION_REQUIRED", "an 'answered' answer must cite at least one retrieved chunk")

    unknown = []
    for chunk_id in cited_chunk_ids:
        if chunk_id not in run_chunks:
            unknown.append(chunk_id)
    if unknown:
        raise AnswerRejected("UNKNOWN_CITATION", f"not returned by this retrieval run: {unknown}")

    citations = []
    allowed_links = set()
    for chunk_id in cited_chunk_ids:
        row = run_chunks[chunk_id]
        citations.append(
            {
                "chunk_id": chunk_id,
                "rank": row["rank"],
                "repo_id": row["repo_id"],
                "commit_sha": row["commit_sha"],
                "path": row["path"],
                "start_line": row["start_line"],
                "end_line": row["end_line"],
                "url": row["url"],
            }
        )
        allowed_links.add(row["url"])

    fabricated = []
    for link in links_in(answer_text):
        if link not in allowed_links:
            fabricated.append(link)
    if fabricated:
        raise AnswerRejected(
            "FABRICATED_LINK",
            f"links in the answer must be the canonical links of cited chunks; not allowed: {fabricated}",
        )
    return citations


def load_run(connection: sqlite3.Connection, run_id: str, window_minutes: int) -> tuple[sqlite3.Row, dict]:
    run = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if run is None:
        raise RunNotFound(f"no retrieval run {run_id}")

    created = datetime.fromisoformat(run["created_at"])
    if datetime.now(UTC) - created > timedelta(minutes=window_minutes):
        raise RunExpired(f"retrieval run {run_id} is older than {window_minutes} minutes; retrieve again")

    rows = connection.execute("SELECT * FROM run_chunks WHERE run_id = ?", (run_id,)).fetchall()
    run_chunks = {}
    for row in rows:
        if row["url"] is None:
            # Recorded before citation metadata was stored (schema v1).
            raise RunExpired(f"retrieval run {run_id} predates citation records; retrieve again")
        run_chunks[row["chunk_id"]] = row
    return run, run_chunks


def accepted_answer(connection: sqlite3.Connection, run_id: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM answers WHERE run_id = ? AND validation = 'accepted'", (run_id,)
    ).fetchone()


def submit_answer(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    status: str,
    answer_text: str,
    cited_chunk_ids: list[str],
    model: str,
    prompt_version: str,
    request_id: str,
    window_minutes: int,
) -> dict:
    run, run_chunks = load_run(connection, run_id, window_minutes)
    digest = content_hash(status, answer_text, cited_chunk_ids, model, prompt_version)

    # A retried submission of an answer that was already accepted gets the same
    # result back, so a Dify retry never turns into a conflict.
    existing = accepted_answer(connection, run_id)
    if existing is not None:
        if existing["content_hash"] == digest:
            return build_response(existing, run, run_chunks)
        raise AnswerConflict(f"run {run_id} already has a different accepted answer {existing['answer_id']}")

    answer_id = str(uuid.uuid4())
    row = {
        "answer_id": answer_id,
        "run_id": run_id,
        "request_id": request_id,
        "created_at": datetime.now(UTC).isoformat(),
        "status": status,
        "answer_text": answer_text,
        "cited_chunk_ids": json.dumps(cited_chunk_ids),
        "model": model,
        "prompt_version": prompt_version,
        "content_hash": digest,
        "validation": "accepted",
        "rejection_code": None,
        "rejection_detail": None,
    }

    try:
        check_citations(status, answer_text, cited_chunk_ids, run_chunks)
    except AnswerRejected as exc:
        row["validation"] = "rejected"
        row["rejection_code"] = exc.code
        row["rejection_detail"] = exc.message
        insert_answer(connection, row)
        raise

    try:
        insert_answer(connection, row)
    except sqlite3.IntegrityError:
        # Another request accepted an answer for this run between our check and insert.
        existing = accepted_answer(connection, run_id)
        if existing is not None and existing["content_hash"] == digest:
            return build_response(existing, run, run_chunks)
        raise AnswerConflict(f"run {run_id} already has a different accepted answer")

    return build_response(accepted_answer(connection, run_id), run, run_chunks)


def insert_answer(connection: sqlite3.Connection, row: dict) -> None:
    columns = list(row)
    placeholders = ", ".join("?" for _ in columns)
    values = []
    for column in columns:
        values.append(row[column])
    with connection:
        connection.execute(f"INSERT INTO answers ({', '.join(columns)}) VALUES ({placeholders})", values)


def build_response(answer: sqlite3.Row, run: sqlite3.Row, run_chunks: dict) -> dict:
    cited_chunk_ids = json.loads(answer["cited_chunk_ids"])
    citations = check_citations(answer["status"], answer["answer_text"], cited_chunk_ids, run_chunks)
    return {
        "answer_id": answer["answer_id"],
        "run_id": answer["run_id"],
        "status": answer["status"],
        "answer_text": answer["answer_text"],
        "citations": citations,
        "index_version": run["index_version"],
        "model": answer["model"],
        "prompt_version": answer["prompt_version"],
    }
