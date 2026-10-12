"""
Review pages: the operator logs in, reads a draft, edits it, and (next step)
confirms it for submission.

Access is by browser session only. The service token Dify holds opens /api/*
and nothing here, so the model side can create drafts but never confirm one.
Every form that changes something carries the session's CSRF token.
"""

import hashlib
import hmac
import re
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.issues.drafts import DraftLocked, DraftNotFound, StaleRevision, edit_draft, get_draft
from app.issues.submit import NotReconcilable, SubmitRefused, mark_not_created, reconcile_draft, submit_draft
from app.settings import Settings

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))
SESSION_COOKIE = "dka_session"
MIN_PASSWORD_LENGTH = 12
DRAFT_PATH = re.compile(r"^/review/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

# No scripts at all; inline styles only; forms post back to this origin only;
# the page cannot be framed, so a confirm button cannot be clickjacked.
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
}

STATUS_LABELS = {
    "draft": "草稿",
    "submitting": "提交中",
    "created": "已创建",
    "failed": "提交失败",
    "unknown": "结果未知，待对账",
}


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def page(request: Request, template: str, context: dict, status_code: int = 200) -> HTMLResponse:
    response = TEMPLATES.TemplateResponse(request, template, context, status_code=status_code)
    for name, value in SECURITY_HEADERS.items():
        response.headers[name] = value
    return response


def redirect(url: str) -> RedirectResponse:
    response = RedirectResponse(url, status_code=303)
    for name, value in SECURITY_HEADERS.items():
        response.headers[name] = value
    return response


def safe_next(target: str | None) -> str:
    """Only ever send the operator back to a draft page, never to an arbitrary URL."""
    if target and DRAFT_PATH.match(target):
        return target
    return "/review"


def review_router(settings: Settings, get_db, github_client=None) -> APIRouter:
    router = APIRouter(include_in_schema=False)

    def password_configured() -> bool:
        return bool(settings.operator_password) and len(settings.operator_password) >= MIN_PASSWORD_LENGTH

    def current_session(request: Request, connection: sqlite3.Connection) -> sqlite3.Row | None:
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            return None
        row = connection.execute("SELECT * FROM operator_sessions WHERE token_hash = ?", (token_hash(token),)).fetchone()
        if row is None:
            return None
        if row["expires_at"] <= datetime.now(UTC).isoformat():
            with connection:
                connection.execute("DELETE FROM operator_sessions WHERE token_hash = ?", (row["token_hash"],))
            return None
        return row

    def csrf_ok(session: sqlite3.Row, submitted: str) -> bool:
        return hmac.compare_digest(session["csrf_token"].encode("utf-8"), submitted.encode("utf-8"))

    def login_redirect(request: Request) -> RedirectResponse:
        return redirect("/login?next=" + request.url.path)

    @router.get("/login")
    def login_form(request: Request, next: str | None = None):
        return page(request, "login.html", {"next": safe_next(next), "configured": password_configured(), "error": None})

    @router.post("/login")
    def login(
        request: Request,
        password: str = Form(...),
        next: str = Form("/review"),
        connection: sqlite3.Connection = Depends(get_db),
    ):
        if not password_configured():
            return page(request, "login.html", {"next": safe_next(next), "configured": False, "error": None}, 503)
        if not hmac.compare_digest(password.encode("utf-8"), settings.operator_password.encode("utf-8")):
            return page(request, "login.html", {"next": safe_next(next), "configured": True, "error": "密码不对。"}, 401)

        token = secrets.token_urlsafe(32)
        now = datetime.now(UTC)
        expires = now + timedelta(hours=settings.session_hours)
        with connection:
            connection.execute(
                "INSERT INTO operator_sessions (token_hash, csrf_token, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (token_hash(token), secrets.token_urlsafe(32), now.isoformat(), expires.isoformat()),
            )
        response = redirect(safe_next(next))
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=settings.session_hours * 3600,
            httponly=True,
            samesite="strict",
            secure=settings.cookie_secure,
            path="/",
        )
        return response

    @router.post("/logout")
    def logout(request: Request, csrf_token: str = Form(""), connection: sqlite3.Connection = Depends(get_db)):
        session = current_session(request, connection)
        if session is not None and csrf_ok(session, csrf_token):
            with connection:
                connection.execute("DELETE FROM operator_sessions WHERE token_hash = ?", (session["token_hash"],))
        response = redirect("/login")
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @router.get("/review")
    def review_index(request: Request, connection: sqlite3.Connection = Depends(get_db)):
        session = current_session(request, connection)
        if session is None:
            return login_redirect(request)
        drafts = []
        rows = connection.execute(
            """
            SELECT d.draft_id, d.status, d.current_revision, d.updated_at, r.title
            FROM issue_drafts d JOIN draft_revisions r
              ON r.draft_id = d.draft_id AND r.revision = d.current_revision
            ORDER BY d.updated_at DESC LIMIT 50
            """
        )
        for row in rows:
            drafts.append(
                {
                    "draft_id": row["draft_id"],
                    "title": row["title"],
                    "status": STATUS_LABELS[row["status"]],
                    "revision": row["current_revision"],
                    "updated_at": row["updated_at"],
                }
            )
        return page(request, "review_index.html", {"drafts": drafts, "csrf_token": session["csrf_token"]})

    @router.get("/review/{draft_id}")
    def review_draft(request: Request, draft_id: str, saved: int = 0, connection: sqlite3.Connection = Depends(get_db)):
        session = current_session(request, connection)
        if session is None:
            return login_redirect(request)
        try:
            draft = get_draft(connection, draft_id)
        except DraftNotFound:
            return page(request, "message.html", {"title": "找不到草稿", "message": "这个草稿不存在。", "back": "/review"}, 404)
        blockers = []
        if not settings.issue_submit_enabled:
            blockers.append("服务没有开启提交（DKA_ISSUE_SUBMIT_ENABLED）")
        if github_client is None:
            blockers.append("没有配置 GitHub token（DKA_GITHUB_TOKEN）")
        if not draft["target_repo"]:
            blockers.append("草稿没有目标仓库（DKA_ISSUE_REPO）")
        elif draft["target_repo"] != settings.issue_repo:
            blockers.append(f"草稿的目标仓库 {draft['target_repo']} 不是服务当前允许的仓库")
        return page(
            request,
            "review.html",
            {
                "draft": draft,
                "status_label": STATUS_LABELS[draft["status"]],
                "editable": draft["status"] in ("draft", "failed"),
                "configured_repo": settings.issue_repo,
                "csrf_token": session["csrf_token"],
                "saved": bool(saved),
                "blockers": blockers,
            },
        )

    @router.post("/review/{draft_id}")
    def save_draft(
        request: Request,
        draft_id: str,
        csrf_token: str = Form(""),
        revision: int = Form(...),
        title: str = Form(""),
        body: str = Form(""),
        connection: sqlite3.Connection = Depends(get_db),
    ):
        session = current_session(request, connection)
        if session is None:
            return login_redirect(request)
        if not csrf_ok(session, csrf_token):
            return page(request, "message.html", {"title": "请求无效", "message": "表单已过期，请刷新页面后重试。", "back": f"/review/{draft_id}"}, 403)
        try:
            edit_draft(connection, draft_id, expected_revision=revision, title=title, body=body)
        except DraftNotFound:
            return page(request, "message.html", {"title": "找不到草稿", "message": "这个草稿不存在。", "back": "/review"}, 404)
        except StaleRevision:
            message = "草稿在你编辑期间被改过了，这次修改没有保存。请刷新页面，在最新版本上重新修改。"
            return page(request, "message.html", {"title": "草稿已更新", "message": message, "back": f"/review/{draft_id}"}, 409)
        except DraftLocked:
            message = "草稿已经开始提交或已提交，不能再修改。"
            return page(request, "message.html", {"title": "不能修改", "message": message, "back": f"/review/{draft_id}"}, 409)
        except ValueError:
            message = "标题和正文都不能为空。"
            return page(request, "message.html", {"title": "没有保存", "message": message, "back": f"/review/{draft_id}"}, 422)
        return redirect(f"/review/{draft_id}?saved=1")

    def operator_action(request: Request, connection: sqlite3.Connection, draft_id: str, csrf_token: str):
        """Shared checks for the POST actions: logged in, valid CSRF, draft exists."""
        session = current_session(request, connection)
        if session is None:
            return None, login_redirect(request)
        if not csrf_ok(session, csrf_token):
            message = "表单已过期，请刷新页面后重试。没有执行任何操作。"
            return None, page(request, "message.html", {"title": "请求无效", "message": message, "back": f"/review/{draft_id}"}, 403)
        try:
            get_draft(connection, draft_id)
        except DraftNotFound:
            return None, page(request, "message.html", {"title": "找不到草稿", "message": "这个草稿不存在。", "back": "/review"}, 404)
        return session, None

    @router.post("/review/{draft_id}/submit")
    def submit(
        request: Request,
        draft_id: str,
        csrf_token: str = Form(""),
        revision: int = Form(...),
        body_hash: str = Form(""),
        connection: sqlite3.Connection = Depends(get_db),
    ):
        session, refusal = operator_action(request, connection, draft_id, csrf_token)
        if refusal is not None:
            return refusal
        try:
            submit_draft(
                connection,
                github_client,
                draft_id=draft_id,
                confirmed_revision=revision,
                confirmed_hash=body_hash,
                allowed_repo=settings.issue_repo,
                enabled=settings.issue_submit_enabled,
            )
        except SubmitRefused as exc:
            return page(request, "message.html", {"title": "没有提交", "message": str(exc), "back": f"/review/{draft_id}"}, 409)
        return redirect(f"/review/{draft_id}")

    @router.post("/review/{draft_id}/reconcile")
    def reconcile(request: Request, draft_id: str, csrf_token: str = Form(""), connection: sqlite3.Connection = Depends(get_db)):
        session, refusal = operator_action(request, connection, draft_id, csrf_token)
        if refusal is not None:
            return refusal
        try:
            reconcile_draft(connection, github_client, draft_id)
        except NotReconcilable as exc:
            return page(request, "message.html", {"title": "不能对账", "message": str(exc), "back": f"/review/{draft_id}"}, 409)
        return redirect(f"/review/{draft_id}")

    @router.post("/review/{draft_id}/mark-not-created")
    def not_created(request: Request, draft_id: str, csrf_token: str = Form(""), connection: sqlite3.Connection = Depends(get_db)):
        session, refusal = operator_action(request, connection, draft_id, csrf_token)
        if refusal is not None:
            return refusal
        try:
            mark_not_created(connection, draft_id)
        except NotReconcilable as exc:
            return page(request, "message.html", {"title": "不能标记", "message": str(exc), "back": f"/review/{draft_id}"}, 409)
        return redirect(f"/review/{draft_id}")

    @router.get("/review/{draft_id}/status")
    def status(request: Request, draft_id: str, connection: sqlite3.Connection = Depends(get_db)):
        session = current_session(request, connection)
        if session is None:
            return JSONResponse({"code": "UNAUTHORIZED", "message": "log in on the review page"}, status_code=401)
        try:
            draft = get_draft(connection, draft_id)
        except DraftNotFound:
            return JSONResponse({"code": "DRAFT_NOT_FOUND", "message": f"no draft {draft_id}"}, status_code=404)
        return JSONResponse(
            {
                "draft_id": draft["draft_id"],
                "status": draft["status"],
                "revision": draft["revision"],
                "submitted_revision": draft["submitted_revision"],
                "issue_url": draft["issue_url"],
                "failure_reason": draft["failure_reason"],
                "reconcile_result": draft["reconcile_result"],
            },
            headers={"Cache-Control": "no-store"},
        )

    return router
