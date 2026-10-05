"""Request and response bodies. These are the contract Dify's HTTP nodes are built against."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RequestModel(BaseModel):
    # Reject unknown fields: a typo in a Dify node ("topk") should fail loudly,
    # not be dropped while the default silently applies.
    model_config = ConfigDict(extra="forbid")


class RetrieveRequest(RequestModel):
    query: str = Field(min_length=1, max_length=1000)
    # Omit to search every repository in the active index version.
    repo_id: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    top_k: int | None = Field(default=None, ge=1)


class Score(BaseModel):
    # A distance, not a confidence: lower means closer, and it says nothing about
    # whether the chunk answers the question.
    type: Literal["cosine_distance"]
    value: float


class RetrievedChunk(BaseModel):
    chunk_id: str
    rank: int
    repo_id: str
    commit_sha: str
    path: str
    start_line: int
    end_line: int
    source_type: Literal["code", "docs"]
    symbol: str | None
    content: str
    url: str
    score: Score


class RepoScope(BaseModel):
    commit_sha: str
    remote: str


class RetrieveResponse(BaseModel):
    run_id: str
    status: Literal["ok", "no_evidence"]
    index_version: str
    repos: dict[str, RepoScope]
    chunks: list[RetrievedChunk]
    latency_ms: float


class ErrorResponse(BaseModel):
    code: str
    message: str
    request_id: str


class AnswerRequest(RequestModel):
    run_id: str = Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    status: Literal["answered", "insufficient_evidence"]
    answer_text: str = Field(min_length=1, max_length=20000)
    cited_chunk_ids: list[str] = Field(default_factory=list, max_length=20)
    # Which generator produced this answer (spec §5: record model and prompt version).
    model: str = Field(min_length=1, max_length=200)
    prompt_version: str = Field(min_length=1, max_length=64)


class Citation(BaseModel):
    chunk_id: str
    rank: int
    repo_id: str
    commit_sha: str
    path: str
    start_line: int
    end_line: int
    url: str


class AnswerResponse(BaseModel):
    answer_id: str
    run_id: str
    status: Literal["answered", "insufficient_evidence"]
    answer_text: str
    citations: list[Citation]
    index_version: str
    model: str
    prompt_version: str


FeedbackReason = Literal["wrong_answer", "incomplete", "wrong_citation", "not_relevant", "other"]


class FeedbackRequest(RequestModel):
    run_id: str = Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    resolved: bool
    # Why it did not help. Only meaningful, and only accepted, when resolved is false.
    reason: FeedbackReason | None = None
    comment: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def reason_only_when_unresolved(self) -> "FeedbackRequest":
        if self.resolved and self.reason is not None:
            raise ValueError("reason is only accepted when resolved is false")
        return self


class FeedbackResponse(BaseModel):
    run_id: str
    answer_id: str
    resolved: bool
    reason: FeedbackReason | None
    comment: str | None
    created_at: str
    updated_at: str
