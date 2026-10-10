"""
FastAPI service: health, readiness and retrieval.

    DKA_SERVICE_TOKEN=... uv run uvicorn app.main:app

Error contract: every handled failure returns {code, message, request_id} and
the same request_id is in the X-Request-ID header and the log line. Auth
failures are 401, bad input 422, an unavailable dependency 503.
"""

import hmac
import logging
import sqlite3
import uuid
from contextlib import asynccontextmanager

import chromadb
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.answers import AnswerConflict, AnswerRejected, RunExpired, RunNotFound, submit_answer
from app.api.schemas import (
    AnswerRequest,
    AnswerResponse,
    ErrorResponse,
    FeedbackRequest,
    FeedbackResponse,
    RetrieveRequest,
    RetrieveResponse,
)
from app.feedback import NoAnswerForRun, submit_feedback
from app.indexing.build import collection_name
from app.retrieval.encoder import Encoder
from app.retrieval.service import EncoderMismatch, NoActiveIndex, UnknownRepo, retrieve
from app.settings import Settings
from app.storage.db import active_version, connect, init_db

logger = logging.getLogger("dka")

MIN_TOKEN_LENGTH = 16


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def error_response(request: Request, status_code: int, code: str, message: str) -> JSONResponse:
    request_id = request.state.request_id
    body = ErrorResponse(code=code, message=message, request_id=request_id)
    return JSONResponse(status_code=status_code, content=body.model_dump(), headers={"X-Request-ID": request_id})


def create_app(
    settings: Settings | None = None,
    encoder: Encoder | None = None,
    chroma_client=None,
) -> FastAPI:
    """Build the app. Tests pass a fake encoder and a temp-dir Chroma client."""
    settings = settings or Settings()
    if not settings.service_token or len(settings.service_token) < MIN_TOKEN_LENGTH:
        raise RuntimeError(f"DKA_SERVICE_TOKEN must be set to at least {MIN_TOKEN_LENGTH} characters")
    expected_auth = f"Bearer {settings.service_token}".encode("utf-8")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_db(settings.db_path)
        app.state.chroma = chroma_client or chromadb.PersistentClient(path=str(settings.chroma_dir))
        if encoder is not None:
            app.state.encoder = encoder
        else:
            from app.retrieval.encoder import SentenceTransformerEncoder

            app.state.encoder = SentenceTransformerEncoder(settings.embed_model, settings.embed_batch_size)
        yield

    app = FastAPI(title="Dev Knowledge Assistant", version="0.1.0", lifespan=lifespan)

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        # Always generated here, never taken from the caller, so it is unique per request.
        request.state.request_id = str(uuid.uuid4())
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError):
        return error_response(request, exc.status_code, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        problems = []
        for error in exc.errors():
            location = ".".join(str(part) for part in error["loc"])
            problems.append(f"{location}: {error['msg']}")
        return error_response(request, 422, "INVALID_REQUEST", "; ".join(problems))

    def get_db():
        # FastAPI may open this connection on one worker thread and run the
        # endpoint on another. One request uses it at a time, never two
        # threads at once, so sqlite3's same-thread check can be turned off.
        connection = connect(settings.db_path, check_same_thread=False)
        try:
            yield connection
        finally:
            connection.close()

    def require_service_token(request: Request) -> None:
        provided = request.headers.get("Authorization", "").encode("utf-8")
        # Constant-time comparison, so response timing does not leak the token.
        if not hmac.compare_digest(provided, expected_auth):
            raise ApiError(401, "UNAUTHORIZED", "missing or invalid service token")

    @app.get("/health")
    def health() -> dict:
        """Process is up. Reveals nothing about configuration."""
        return {"status": "ok"}

    @app.get("/ready")
    def ready(request: Request, connection: sqlite3.Connection = Depends(get_db)):
        """
        Can this instance serve retrieval right now? Never calls a paid model.

        Reports only pass/fail per check, no versions or paths, because it is unauthenticated.
        """
        checks = {"database": False, "active_index": False, "encoder": False}
        try:
            connection.execute("SELECT 1").fetchone()
            checks["database"] = True
            row = active_version(connection)
            if row is not None:
                collection = request.app.state.chroma.get_collection(collection_name(row["version"]))
                checks["active_index"] = collection.count() == row["chunk_count"]
                checks["encoder"] = request.app.state.encoder.model_name == row["embed_model"]
        except Exception:
            logger.exception("readiness check failed request_id=%s", request.state.request_id)

        all_ok = True
        for passed in checks.values():
            if not passed:
                all_ok = False
        status_code = 200 if all_ok else 503
        return JSONResponse(status_code=status_code, content={"status": "ready" if all_ok else "not_ready", "checks": checks})

    @app.post(
        "/api/retrieve",
        response_model=RetrieveResponse,
        dependencies=[Depends(require_service_token)],
        responses={401: {"model": ErrorResponse}, 422: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
    )
    def retrieve_endpoint(
        body: RetrieveRequest,
        request: Request,
        connection: sqlite3.Connection = Depends(get_db),
    ) -> dict:
        query = body.query.strip()
        if not query:
            raise ApiError(422, "INVALID_REQUEST", "query: must not be blank")
        top_k = body.top_k or settings.top_k_default
        if top_k > settings.top_k_max:
            raise ApiError(422, "INVALID_REQUEST", f"top_k: must be at most {settings.top_k_max}")

        try:
            return retrieve(
                connection,
                request.app.state.chroma,
                request.app.state.encoder,
                query,
                body.repo_id,
                top_k,
                request.state.request_id,
            )
        except UnknownRepo as exc:
            raise ApiError(422, "UNKNOWN_REPO", str(exc)) from exc
        except NoActiveIndex as exc:
            raise ApiError(503, "NO_ACTIVE_INDEX", str(exc)) from exc
        except EncoderMismatch as exc:
            logger.error("%s request_id=%s", exc, request.state.request_id)
            raise ApiError(503, "INDEX_ENCODER_MISMATCH", "the retrieval index and encoder do not match") from exc
        except Exception as exc:
            logger.exception("retrieval failed request_id=%s", request.state.request_id)
            raise ApiError(503, "RETRIEVAL_UNAVAILABLE", "retrieval is temporarily unavailable") from exc

    @app.post(
        "/api/answers",
        response_model=AnswerResponse,
        dependencies=[Depends(require_service_token)],
        responses={
            401: {"model": ErrorResponse},
            404: {"model": ErrorResponse},
            409: {"model": ErrorResponse},
            410: {"model": ErrorResponse},
            422: {"model": ErrorResponse},
        },
    )
    def answers_endpoint(
        body: AnswerRequest,
        request: Request,
        connection: sqlite3.Connection = Depends(get_db),
    ) -> dict:
        """
        Validate a generated answer against its retrieval run, then record it.

        A rejected answer (unknown citation, fabricated link, ...) is a 422 that
        Dify must route to an error branch, never shown as a normal answer.
        """
        try:
            return submit_answer(
                connection,
                run_id=body.run_id,
                status=body.status,
                answer_text=body.answer_text,
                cited_chunk_ids=body.cited_chunk_ids,
                model=body.model,
                prompt_version=body.prompt_version,
                request_id=request.state.request_id,
                window_minutes=settings.answer_window_minutes,
            )
        except RunNotFound as exc:
            raise ApiError(404, "RUN_NOT_FOUND", str(exc)) from exc
        except RunExpired as exc:
            raise ApiError(410, "RUN_EXPIRED", str(exc)) from exc
        except AnswerRejected as exc:
            raise ApiError(422, exc.code, exc.message) from exc
        except AnswerConflict as exc:
            raise ApiError(409, "ANSWER_ALREADY_ACCEPTED", str(exc)) from exc

    @app.post(
        "/api/feedback",
        response_model=FeedbackResponse,
        dependencies=[Depends(require_service_token)],
        responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
    )
    def feedback_endpoint(
        body: FeedbackRequest,
        request: Request,
        connection: sqlite3.Connection = Depends(get_db),
    ) -> dict:
        """Record whether the user's problem was solved. Replaces earlier feedback for the same run."""
        comment = body.comment.strip() if body.comment else None
        try:
            return submit_feedback(
                connection,
                run_id=body.run_id,
                resolved=body.resolved,
                reason=body.reason,
                comment=comment or None,
                request_id=request.state.request_id,
            )
        except RunNotFound as exc:
            raise ApiError(404, "RUN_NOT_FOUND", str(exc)) from exc
        except NoAnswerForRun as exc:
            raise ApiError(409, "NO_ANSWER_FOR_RUN", str(exc)) from exc

    return app


def __getattr__(name: str):
    # `uvicorn app.main:app` builds the app on first access, from the environment.
    # Importing this module (tests) does not require DKA_SERVICE_TOKEN.
    if name == "app":
        return create_app()
    raise AttributeError(name)
