"""Connected accounts page (PRD F10): shows every provider the hub supports —
connected (with tool count, last used, disconnect), ready-to-connect (OAuth
app registered, one-click connect), or not-yet-configured (OAuth app missing,
explains exactly what env vars are needed) — plus Freshdesk's API-key form.
Redesigned so the whole picture (what's live, what's possible, what's
blocked and why) is visible at a glance, not just a list of what's already
connected (PRD F10: "status, last used, and scopes").

Mounted the same way login_pages.router is — behind the hub-login gate,
reusing oauth_server's _PAGE_STYLES/_error_page so it looks the same without
duplicating CSS. Route paths and form field names are unchanged from the
prior version — only the HTML this module renders is different.
"""
import html
import os
import time

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

try:
    import auth
    import connections
    from adapters import ADAPTERS, register_all_adapters
    from adapters.freshdesk import FreshdeskAdapter, validate_freshdesk_domain
    from connect_oauth import PROVIDER_OAUTH_CONFIG
    from login_pages import SESSION_COOKIE_NAME, _CSRF_COOKIE_NAME, _new_csrf_token, _set_csrf_cookie, _check_csrf
    from oauth_server import _PAGE_STYLES
except ImportError:
    from src import auth
    from src import connections
    from src.adapters import ADAPTERS, register_all_adapters
    from src.adapters.freshdesk import FreshdeskAdapter, validate_freshdesk_domain
    from src.connect_oauth import PROVIDER_OAUTH_CONFIG
    from src.login_pages import SESSION_COOKIE_NAME, _CSRF_COOKIE_NAME, _new_csrf_token, _set_csrf_cookie, _check_csrf
    from src.oauth_server import _PAGE_STYLES

router = APIRouter()
_freshdesk_adapter = FreshdeskAdapter()
register_all_adapters()  # idempotent — ensures ADAPTERS is populated even if this page loads before mcp_http_server.py does

_PROVIDER_DISPLAY_NAMES = {
    "freshdesk": "Freshdesk", "asana": "Asana", "hubspot": "HubSpot", "salesforce": "Salesforce",
    "zoho": "Zoho CRM", "outreach": "Outreach", "salesloft": "SalesLoft",
}
_ALL_PROVIDER_KEYS = list(_PROVIDER_DISPLAY_NAMES.keys())


def _fmt_time(ts: float | None) -> str:
    if not ts:
        return "never"
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts))


def _tool_count(provider: str) -> int:
    adapter = ADAPTERS.get(provider)
    return len(adapter.tools()) if adapter else 0


def _oauth_app_configured(provider: str) -> bool:
    config = PROVIDER_OAUTH_CONFIG.get(provider)
    return bool(config and os.environ.get(config["client_id_env"]))


def _status_badge(label: str, color: str) -> str:
    return f'<span style="display:inline-block; padding:2px 10px; border-radius:999px; font-size:12px; font-weight:600; background:{color}1f; color:{color};">{label}</span>'


def _connected_card(conn: dict, csrf: str) -> str:
    provider = conn["provider"]
    name = html.escape(_PROVIDER_DISPLAY_NAMES.get(provider, provider))
    status = conn["status"]
    badge = {
        "active": _status_badge("Connected", "#4ade80"),
        "needs_reauth": _status_badge("Needs reauthorization", "#fbbf24"),
    }.get(status, _status_badge(status, "#9aa4b2"))
    tool_count = _tool_count(provider)
    return f"""
    <div style="display:flex; align-items:center; justify-content:space-between; padding:16px; border:1px solid var(--border); border-radius:10px; margin-bottom:10px; background:rgba(74,222,128,0.04);">
      <div>
        <div style="display:flex; align-items:center; gap:10px;">
          <span style="font-weight:600; font-size:15px;">{name}</span>
          {badge}
        </div>
        <div style="font-size:12px; color:var(--muted); margin-top:6px;">
          {tool_count} tools available · last used {html.escape(_fmt_time(conn.get("last_used_at")))} · scopes: {html.escape(", ".join(conn.get("scopes") or []) or "n/a")}
        </div>
      </div>
      <form method="post" action="/connected-accounts/disconnect">
        <input type="hidden" name="provider" value="{html.escape(provider)}">
        <input type="hidden" name="csrf" value="{csrf}">
        <button type="submit" style="background:transparent; border:1px solid var(--border); color:var(--muted); padding:8px 16px; border-radius:8px; cursor:pointer; font-size:13px;">Disconnect</button>
      </form>
    </div>
    """


def _available_card(provider: str) -> str:
    """OAuth provider with no connection yet, but its app IS configured
    (client_id env var set) — one click away."""
    name = html.escape(_PROVIDER_DISPLAY_NAMES[provider])
    tool_count = _tool_count(provider)
    return f"""
    <div style="display:flex; align-items:center; justify-content:space-between; padding:16px; border:1px solid var(--border); border-radius:10px; margin-bottom:10px;">
      <div>
        <div style="font-weight:600; font-size:15px;">{name}</div>
        <div style="font-size:12px; color:var(--muted); margin-top:6px;">{tool_count} tools available once connected</div>
      </div>
      <a href="/connect/{provider}/start" style="text-decoration:none;">
        <button type="button" style="background: var(--accent); color: white; border:none; padding:8px 18px; border-radius:8px; cursor:pointer; font-size:13px; font-weight:600;">Connect</button>
      </a>
    </div>
    """


def _not_configured_card(provider: str) -> str:
    """OAuth provider whose app hasn't been registered yet — tells the
    operator exactly which env vars are missing instead of a dead button."""
    name = html.escape(_PROVIDER_DISPLAY_NAMES[provider])
    tool_count = _tool_count(provider)
    config = PROVIDER_OAUTH_CONFIG[provider]
    return f"""
    <div style="display:flex; align-items:center; justify-content:space-between; padding:16px; border:1px dashed var(--border); border-radius:10px; margin-bottom:10px; opacity:0.6;">
      <div>
        <div style="font-weight:600; font-size:15px;">{name}</div>
        <div style="font-size:12px; color:var(--muted); margin-top:6px;">
          {tool_count} tools — not yet set up. Needs <code style="color:#9aa4b2;">{html.escape(config['client_id_env'])}</code> and <code style="color:#9aa4b2;">{html.escape(config['client_secret_env'])}</code> env vars.
        </div>
      </div>
      <button type="button" disabled style="background:transparent; border:1px solid var(--border); color:#5a6472; padding:8px 18px; border-radius:8px; font-size:13px; cursor:not-allowed;">Not configured</button>
    </div>
    """


def _page(hub_email: str, connections_rows: list[dict], csrf: str, error: str | None = None) -> str:
    connected_by_provider = {c["provider"]: c for c in connections_rows}
    connected_count = len(connected_by_provider)
    total_providers = len(_ALL_PROVIDER_KEYS)
    total_tools_live = sum(_tool_count(p) for p in connected_by_provider)

    error_html = f'<div class="scope-box" style="border-color: rgba(239,68,68,0.3); color: #f3b4b4;">{html.escape(error)}</div>' if error else ""

    connected_html = "".join(_connected_card(connected_by_provider[p], csrf) for p in _ALL_PROVIDER_KEYS if p in connected_by_provider)
    if not connected_html:
        connected_html = '<p class="subtitle" style="margin:0 0 20px;">Nothing connected yet — connect a provider below to get started.</p>'

    other_cards = []
    for provider in _ALL_PROVIDER_KEYS:
        if provider == "freshdesk" or provider in connected_by_provider:
            continue
        other_cards.append(_available_card(provider) if _oauth_app_configured(provider) else _not_configured_card(provider))
    other_html = "".join(other_cards)

    freshdesk_section = ""
    if "freshdesk" not in connected_by_provider:
        freshdesk_tool_count = _tool_count("freshdesk")
        freshdesk_section = f"""
        <div style="padding:16px; border:1px solid var(--border); border-radius:10px; margin-bottom:10px;">
          <div style="font-weight:600; font-size:15px; margin-bottom:4px;">Freshdesk</div>
          <div style="font-size:12px; color:var(--muted); margin-bottom:14px;">{freshdesk_tool_count} tools — no OAuth app for Freshdesk yet, connect with your domain and API key instead.</div>
          <form method="post" action="/connected-accounts/connect-freshdesk">
            <input type="hidden" name="csrf" value="{csrf}">
            <label for="freshdesk_domain">Freshdesk domain</label>
            <input id="freshdesk_domain" name="freshdesk_domain" placeholder="yourcompany.freshdesk.com" required autocomplete="off">
            <label for="freshdesk_api_key">Freshdesk API key</label>
            <input id="freshdesk_api_key" name="freshdesk_api_key" type="password" placeholder="Found under Profile Settings → API Key" required autocomplete="off">
            <div class="actions">
              <button type="submit" style="background: var(--accent); color: white;">Connect Freshdesk</button>
            </div>
          </form>
        </div>
        """

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Connected accounts</title>
  <style>{_PAGE_STYLES}</style>
</head>
<body>
  <div class="card" style="max-width:640px;">
    <div class="logo-row">
      <div class="logo-badge">FD</div>
      <div class="logo-text">Connector Hub</div>
    </div>
    <h1>Connected accounts</h1>
    <p class="subtitle">Signed in as {html.escape(hub_email)}. {connected_count} of {total_providers} providers connected, {total_tools_live} tools live right now.
      <form method="post" action="/logout" style="display:inline;"><button type="submit" style="background:none; border:none; color:var(--muted); text-decoration:underline; cursor:pointer; font-size:inherit; padding:0;">Log out</button></form>
    </p>
    {error_html}

    <h1 style="font-size:14px; letter-spacing:0.04em; text-transform:uppercase; color:var(--muted); margin-top:24px; margin-bottom:12px;">Connected</h1>
    {connected_html}

    <h1 style="font-size:14px; letter-spacing:0.04em; text-transform:uppercase; color:var(--muted); margin-top:28px; margin-bottom:12px;">Available to connect</h1>
    {freshdesk_section}
    {other_html}
  </div>
</body>
</html>"""


@router.get("/connected-accounts")
def connected_accounts_page(request: Request):
    user = auth.resolve_session(request.cookies.get(SESSION_COOKIE_NAME))
    if not user:
        return RedirectResponse("/login?next=/connected-accounts", status_code=302)
    rows = connections.list_connections_for_user(user.user_id)
    csrf = _new_csrf_token()
    resp = HTMLResponse(_page(user.email, rows, csrf))
    _set_csrf_cookie(resp, csrf)
    return resp


@router.post("/connected-accounts/connect-freshdesk")
def connect_freshdesk(request: Request, freshdesk_domain: str = Form(...), freshdesk_api_key: str = Form(...), csrf: str = Form(...)):
    user = auth.resolve_session(request.cookies.get(SESSION_COOKIE_NAME))
    if not user:
        return RedirectResponse("/login?next=/connected-accounts", status_code=302)
    if not _check_csrf(request, csrf):
        return HTMLResponse(_page(user.email, connections.list_connections_for_user(user.user_id), _new_csrf_token(), error="Your session expired. Please try again."), status_code=400)

    validated_domain = validate_freshdesk_domain(freshdesk_domain)
    if not validated_domain:
        rows = connections.list_connections_for_user(user.user_id)
        return HTMLResponse(_page(user.email, rows, _new_csrf_token(), error="That doesn't look like a valid Freshdesk domain (expected something.freshdesk.com)."), status_code=400)

    from adapters import Connection
    try:
        _freshdesk_adapter.verify(Connection(
            user_id=user.user_id, provider="freshdesk", auth_type="api_key",
            credentials={"domain": validated_domain, "api_key": freshdesk_api_key},
        ))
    except Exception:
        rows = connections.list_connections_for_user(user.user_id)
        return HTMLResponse(_page(user.email, rows, _new_csrf_token(), error="That domain and API key combination was rejected by Freshdesk."), status_code=401)

    connections.create_connection(
        user_id=user.user_id, provider="freshdesk", auth_type="api_key",
        credentials={"domain": validated_domain, "api_key": freshdesk_api_key},
    )
    return RedirectResponse("/connected-accounts", status_code=302)


@router.post("/connected-accounts/disconnect")
def disconnect_provider(request: Request, provider: str = Form(...), csrf: str = Form(...)):
    user = auth.resolve_session(request.cookies.get(SESSION_COOKIE_NAME))
    if not user:
        return RedirectResponse("/login?next=/connected-accounts", status_code=302)
    if not _check_csrf(request, csrf):
        return HTMLResponse(_page(user.email, connections.list_connections_for_user(user.user_id), _new_csrf_token(), error="Your session expired. Please try again."), status_code=400)
    connections.disconnect(user.user_id, provider)
    return RedirectResponse("/connected-accounts", status_code=302)
