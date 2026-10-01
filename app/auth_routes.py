"""Login / logout / account pages. Plain HTML forms (no JavaScript needed), so these routes are public."""
import logging
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .auth import (AuthError, COOKIE_NAME, client_ip, require_login, safe_next)
from .routes import TEMPLATES

log = logging.getLogger(__name__)

public = APIRouter()      # /login
private = APIRouter()     # /logout, /account (need a session; mounted with require_login)


async def form(request: Request) -> dict[str, str]:
    """application/x-www-form-urlencoded body, without needing python-multipart."""
    body = await request.body()
    if len(body) > 8192:
        return {}
    return {k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True).items()}


def secure_cookie(request: Request) -> bool:
    setting = request.app.state.settings.cookie_secure
    return request.url.scheme == "https" if setting is None else setting


def set_session_cookie(request: Request, response: Response, token: str) -> None:
    settings = request.app.state.settings
    response.set_cookie(COOKIE_NAME, token, max_age=int(settings.session_hours * 3600), httponly=True,
                        samesite="lax", secure=secure_cookie(request), path="/")


def login_page(request: Request, *, error: str = "", username: str = "", next_url: str = "/", status: int = 200):
    no_users = request.app.state.db.count_users() == 0
    return TEMPLATES.TemplateResponse(request, "login.html", {
        "error": error, "username": username, "next": next_url, "no_users": no_users}, status_code=status)


# ---------- public ----------

@public.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, next: str = "/"):
    settings = request.app.state.settings
    if not settings.auth_enabled or request.app.state.auth.user_for_token(request.cookies.get(COOKIE_NAME)):
        return RedirectResponse(safe_next(next), status_code=303)
    return login_page(request, next_url=safe_next(next))


@public.post("/login")
async def login_submit(request: Request):
    settings = request.app.state.settings
    if not settings.auth_enabled:
        return RedirectResponse("/", status_code=303)
    auth = request.app.state.auth
    data = await form(request)
    username, password, next_url = data.get("username", ""), data.get("password", ""), safe_next(data.get("next"))
    ip = client_ip(request)

    wait = auth.throttle.retry_after(ip, username)
    if wait:
        log.warning("login throttled for %s (user %r)", ip, username[:64])
        resp = login_page(request, error=f"Too many failed attempts. Try again in {max(wait // 60, 1)} minute(s).",
                          username=username, next_url=next_url, status=429)
        resp.headers["Retry-After"] = str(wait)
        return resp

    user = auth.authenticate(username, password)
    if not user:
        auth.throttle.failed(ip, username)
        log.warning("failed login from %s (user %r)", ip, username[:64])
        return login_page(request, error="Wrong username or password.", username=username, next_url=next_url, status=401)

    auth.throttle.succeeded(ip, username)
    log.info("login: %s from %s", user["username"], ip)
    resp = RedirectResponse(next_url, status_code=303)
    set_session_cookie(request, resp, auth.start_session(user["id"]))
    return resp


# ---------- signed in ----------

@private.post("/logout")
async def logout(request: Request):
    request.app.state.auth.end_session(request.cookies.get(COOKIE_NAME))
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


@private.get("/account", response_class=HTMLResponse)
async def account_page(request: Request):
    return TEMPLATES.TemplateResponse(request, "account.html", {"message": "", "error": ""})


@private.post("/account/password")
async def change_password(request: Request, user: dict = Depends(require_login)):
    if not user:  # auth disabled: there is no account to change
        return RedirectResponse("/", status_code=303)
    data = await form(request)
    if data.get("new_password") != data.get("confirm_password"):
        return TEMPLATES.TemplateResponse(request, "account.html", {"message": "", "error": "The new passwords do not match."},
                                          status_code=400)
    try:
        request.app.state.auth.change_password(user["id"], data.get("current_password", ""), data.get("new_password", ""),
                                               request.cookies.get(COOKIE_NAME))
    except AuthError as e:
        return TEMPLATES.TemplateResponse(request, "account.html", {"message": "", "error": str(e)}, status_code=400)
    log.info("password changed for %s", user["username"])
    return TEMPLATES.TemplateResponse(request, "account.html",
                                      {"message": "Password changed. Other browsers were signed out.", "error": ""})
