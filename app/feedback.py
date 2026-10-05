"""
Record whether the user's problem was solved, for one answered retrieval run.

The user's `resolved` is stored apart from the model's own `answered` /
`insufficient_evidence` (spec §5): a confident model answer that the user marks
unresolved is exactly the case evaluation needs to find.
"""

import sqlite3
from datetime import UTC, datetime

from app.answers import RunNotFound, accepted_answer


class NoAnswerForRun(Exception):
    """Feedback is about an answer the user saw; this run has none accepted."""


def submit_feedback(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    resolved: bool,
    reason: str | None,
    comment: str | None,
    request_id: str,
) -> dict:
    run = connection.execute("SELECT run_id FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if run is None:
        raise RunNotFound(f"no retrieval run {run_id}")

    answer = accepted_answer(connection, run_id)
    if answer is None:
        raise NoAnswerForRun(f"run {run_id} has no accepted answer to give feedback on")

    now = datetime.now(UTC).isoformat()
    with connection:
        # Upsert: a user who changes their mind replaces their earlier feedback,
        # and a retried request lands on the same row instead of adding one.
        connection.execute(
            """
            INSERT INTO feedback (run_id, answer_id, resolved, reason, comment, request_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (run_id) DO UPDATE SET
                resolved = excluded.resolved,
                reason = excluded.reason,
                comment = excluded.comment,
                request_id = excluded.request_id,
                updated_at = excluded.updated_at
            """,
            (run_id, answer["answer_id"], int(resolved), reason, comment, request_id, now, now),
        )

    row = connection.execute("SELECT * FROM feedback WHERE run_id = ?", (run_id,)).fetchone()
    return {
        "run_id": row["run_id"],
        "answer_id": row["answer_id"],
        "resolved": bool(row["resolved"]),
        "reason": row["reason"],
        "comment": row["comment"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
