"""Hub signup/login/logout — the HTML forms and routes in front of the MCP
OAuth consent flow (oauth_server.py). Separate module so oauth_server.py's
own "security-critical code stays isolated" principle holds for this piece
too: hub login and MCP-client consent are different security surfaces.

Reuses oauth_server's _PAGE_STYLES so the two sets of pages look the same
without duplicating CSS — imported, not copied.
"""
import html
import os
import secrets
from urllib.parse import urlparse

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

try:
    import auth
    from oauth_server import _PAGE_STYLES, _error_page
except ImportError:
    from src import auth
    from src.oauth_server import _PAGE_STYLES, _error_page

router = APIRouter()

SESSION_COOKIE_NAME = "hub_session"
_CSRF_COOKIE_NAME = "hub_csrf"
# True in production (HTTPS); the local dev server (uvicorn --port 8004) runs
# over plain http://, and browsers silently drop a Secure cookie on a
# non-HTTPS origin — so this is driven by OAUTH_BASE_URL's scheme, same
# signal oauth_server.py already uses to know whether it's deployed.
_COOKIE_SECURE = os.environ.get("OAUTH_BASE_URL", "http://localhost:8004").startswith("https://")


def _safe_next(next_param: str) -> str:
    """Only ever redirect to a same-origin relative path after login/signup.
    A bare "https://evil.example" or "//evil.example" (schemeless but still
    absolute) would otherwise send an authenticated response straight to an
    attacker's domain — an open redirect. Reject anything with a scheme or
    netloc, and anything not starting with a single "/".

    Backslashes and control/whitespace characters are rejected before
    urlparse ever sees them: some browsers normalize a leading "/\\" or
    "\\/" to "//", turning what looks like a same-origin path into a
    protocol-relative absolute URL — a known urlparse-based open-redirect
    bypass, not caught by the scheme/netloc check alone."""
    if not next_param or not next_param.startswith("/") or next_param.startswith("//"):
        return "/"
    if "\\" in next_param or any(ord(c) < 0x21 or ord(c) == 0x7f for c in next_param):
        return "/"
    parsed = urlparse(next_param)
    if parsed.scheme or parsed.netloc:
        return "/"
    return next_param


# (form_action, submit_label, switch_action) per auth flow — named once here
# instead of restated as a 3-arg tuple at every GET/POST call site.
_FORMS = {
    "login": ("Log in", "/login", "Log in", "signup", "Don't have an account? Sign up"),
    "signup": ("Create your account", "/signup", "Sign up", "login", "Already have an account? Log in"),
}


def _new_csrf_token() -> str:
    return secrets.token_urlsafe(16)


def _set_csrf_cookie(resp, token: str) -> None:
    """Double-submit-cookie CSRF: a random token set as a cookie the form
    also carries as a hidden field. A cross-site form post can set its own
    hidden field value but can't read this cookie to match it (CORS/cookie
    isolation), so the two only agree on a same-origin submission. No new
    dependency or server-side store needed for this, unlike the OAuth
    consent flow's CSRF nonce (oauth_server.py), which already has a Mongo
    record to carry it in."""
    resp.set_cookie(_CSRF_COOKIE_NAME, token, httponly=False, secure=_COOKIE_SECURE, samesite="lax", max_age=600)


def _auth_form_page(form: str, error: str | None, next_param: str, csrf: str) -> str:
    title, action, submit_label, switch_form, switch_label = _FORMS[form]
    switch_href = f"/{switch_form}?next={next_param}"
    # error comes from auth.AuthError messages (static strings we wrote, but
    # escaped anyway since nothing enforces that at the call site), next_param
    # and switch_href carry an attacker-influenced query string — escape all
    # three before they land in the page.
    safe_next = html.escape(_safe_next(next_param))
    safe_switch_href = html.escape(switch_href)
    error_html = f'<div class="scope-box" style="border-color: rgba(239,68,68,0.3); color: #f3b4b4;">{html.escape(error)}</div>' if error else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>{_PAGE_STYLES}</style>
</head>
<body>
  <div class="card">
    <div class="logo-row">
      <div class="logo-badge">FD</div>
      <div class="logo-text">Freshdesk Connector</div>
    </div>
    <h1>{html.escape(title)}</h1>
    {error_html}
    <form method="post" action="{action}">
      <input type="hidden" name="next" value="{safe_next}">
      <input type="hidden" name="csrf" value="{html.escape(csrf)}">
      <label for="email">Email</label>
      <input id="email" name="email" type="email" placeholder="you@example.com" required autocomplete="email">
      <label for="password">Password</label>
      <input id="password" name="password" type="password" placeholder="At least 8 characters" required autocomplete="current-password">
      <div class="actions">
        <button type="submit" value="submit" style="background: var(--accent); color: white; border-color: transparent;">{html.escape(submit_label)}</button>
      </div>
    </form>
    <p class="footer-note"><a href="{safe_switch_href}" style="color: var(--muted);">{html.escape(switch_label)}</a></p>
  </div>
</body>
</html>"""


def _render_form(form: str, next_param: str, error: str | None = None, status_code: int = 200) -> HTMLResponse:
    csrf = _new_csrf_token()
    resp = HTMLResponse(_auth_form_page(form, error, next_param, csrf), status_code=status_code)
    _set_csrf_cookie(resp, csrf)
    return resp


def _check_csrf(request: Request, csrf_form_value: str) -> bool:
    cookie_value = request.cookies.get(_CSRF_COOKIE_NAME)
    return bool(cookie_value) and secrets.compare_digest(cookie_value, csrf_form_value)


def _complete_auth(form: str, request: Request, email: str, password: str, next: str, csrf: str) -> HTMLResponse:
    """Shared body of login_submit/signup_submit: both are "check CSRF, run
    auth.<verb>, on failure re-render the same form with an error, on
    success start a session and redirect" — the only thing that differs is
    which auth.py function to call, passed in as `form`."""
    if not _check_csrf(request, csrf):
        return HTMLResponse(_error_page("Invalid request", "Your session expired. Please try again."), status_code=400)

    action = auth.verify_password if form == "login" else auth.signup
    try:
        user = action(email, password)
    except auth.AuthError as exc:
        return _render_form(form, next, error=str(exc), status_code=401 if form == "login" else 400)

    token = auth.create_session(user.user_id)
    resp = RedirectResponse(_safe_next(next), status_code=302)
    resp.set_cookie(SESSION_COOKIE_NAME, token, httponly=True, secure=_COOKIE_SECURE, samesite="lax", max_age=auth.SESSION_TTL_SECONDS)
    resp.delete_cookie(_CSRF_COOKIE_NAME)
    return resp


@router.get("/login")
def login_page(next: str = ""):
    return _render_form("login", next)


@router.post("/login")
def login_submit(request: Request, email: str = Form(...), password: str = Form(...), next: str = Form(""), csrf: str = Form("")):
    return _complete_auth("login", request, email, password, next, csrf)


@router.get("/signup")
def signup_page(next: str = ""):
    return _render_form("signup", next)


@router.post("/signup")
def signup_submit(request: Request, email: str = Form(...), password: str = Form(...), next: str = Form(""), csrf: str = Form("")):
    return _complete_auth("signup", request, email, password, next, csrf)


@router.post("/logout")
def logout_submit(request: Request):
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        auth.destroy_session(token)
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie(SESSION_COOKIE_NAME)
    return resp


@router.get("/")
def hub_home(request: Request):
    # A signed-in browser visiting "/" is a person, not an MCP client (MCP
    # speaks JSON-RPC via POST, this is GET) — send them straight to the
    # page that actually shows what's connected, instead of a bare "signed
    # in as X" page that told them nothing.
    user = auth.resolve_session(request.cookies.get(SESSION_COOKIE_NAME))
    if not user:
        return RedirectResponse("/login", status_code=302)
    return RedirectResponse("/connected-accounts", status_code=302)
