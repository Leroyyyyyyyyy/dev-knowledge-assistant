"""Request and response bodies. These are the contract Dify's HTTP nodes are built against."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


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
