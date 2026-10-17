"""Connected accounts page (PRD F10): shows every provider the hub supports —
connected (with tool count, last used, disconnect), ready-to-connect (OAuth
app registered, one-click connect), or not-yet-configured (OAuth app missing,
explains exactly what env vars are needed) — plus Freshdesk's API-key form.

Redesigned (second pass) as a proper admin-panel layout: sidebar + summary
row + provider list, real favicons via Google's favicon service, clicking a
connected provider opens its tool detail page (provider_detail.py) showing
every tool's name/description and exactly how to point ChatGPT, Claude, or
any other MCP client at this server.

Mounted the same way login_pages.router is — behind the hub-login gate,
reusing oauth_server's _PAGE_STYLES so it looks the same without duplicating
CSS. Route paths and form field names are unchanged from the prior version —
existing tests and the connect/disconnect flow work exactly as before.
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

PROVIDER_DISPLAY_NAMES = {
    "freshdesk": "Freshdesk", "asana": "Asana", "hubspot": "HubSpot", "salesforce": "Salesforce",
    "zoho": "Zoho CRM", "outreach": "Outreach", "salesloft": "SalesLoft",
}
PROVIDER_DOMAINS = {
    "freshdesk": "freshdesk.com", "asana": "asana.com", "hubspot": "hubspot.com", "salesforce": "salesforce.com",
    "zoho": "zoho.com", "outreach": "outreach.io", "salesloft": "salesloft.com",
}
_ALL_PROVIDER_KEYS = list(PROVIDER_DISPLAY_NAMES.keys())


def favicon_url(provider: str) -> str:
    domain = PROVIDER_DOMAINS.get(provider, f"{provider}.com")
    return f"https://www.google.com/s2/favicons?sz=64&domain={domain}"


# Shared admin-panel chrome (sidebar + content shell) — used here and by
# provider_detail.py so both pages read as one product, not two different apps.
_EXTRA_STYLES = """
    body { align-items: stretch; justify-content: stretch; padding: 0; background: var(--bg); }
    .shell { width: 100%; height: 100vh; display: flex; }
    .sidebar {
      width: 232px; flex: none; box-sizing: border-box; padding: 20px 14px;
      display: flex; flex-direction: column; background: var(--card); border-right: 1px solid var(--border);
      height: 100%; overflow-y: auto;
    }
    .theme-toggle-btn {
      background: none; border: 1px solid var(--border); cursor: pointer; width: 100%;
      text-align: left; font-family: inherit; color: var(--muted); font-size: 12px;
      font-weight: 500; padding: 7px 10px; border-radius: 7px; margin-top: 10px;
    }
    .theme-toggle-btn:hover { background: var(--border-soft); color: var(--text); }
    .sidebar-logo { display: flex; align-items: center; gap: 9px; padding: 4px 8px 20px 8px; }
    .sidebar-logo .logo-badge { width: 22px; height: 22px; border-radius: 6px; background: var(--text); }
    .sidebar-logo .logo-text { font-weight: 600; font-size: 14px; color: var(--text); }
    .nav-item { display: flex; align-items: center; gap: 9px; padding: 7px 10px; border-radius: 7px; margin-bottom: 1px; font-size: 13.5px; }
    .nav-item.active { background: var(--border-soft); color: var(--text); font-weight: 600; }
    .nav-item:not(.active) { color: var(--muted); font-weight: 500; }
    .sidebar-user { padding: 10px; border-radius: 9px; border: 1px solid var(--border); display: flex; align-items: center; gap: 9px; margin-top: auto; }
    .sidebar-avatar { width: 26px; height: 26px; border-radius: 50%; background: var(--border-soft); flex: none; display: flex; align-items: center; justify-content: center; font-size: 11px; font-weight: 600; color: var(--muted); }
    .sidebar-email { font-size: 12px; font-weight: 500; color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .logout-btn { background: var(--border-soft); border: 1px solid var(--border); color: var(--text); text-decoration: none; cursor: pointer; font-size: 11px; font-weight: 500; padding: 4px 10px; border-radius: 6px; font-family: inherit; margin-top: 4px; }
    .logout-btn:hover { background: var(--border); }
    .main { flex: 1 1 auto; min-height: 0; overflow-y: auto; }
    .main-inner { max-width: 920px; margin: 0 auto; padding: 36px 40px 48px 40px; }
    .page-title { margin: 0 0 5px 0; font-size: 22px; font-weight: 700; letter-spacing: -0.01em; color: var(--text); }
    .page-sub { font-size: 13.5px; color: var(--muted); }
    .stat-row { display: flex; gap: 32px; padding: 18px 0; border-top: 1px solid var(--border); border-bottom: 1px solid var(--border); margin-bottom: 28px; }
    .stat-value { font-size: 20px; font-weight: 700; color: var(--text); font-variant-numeric: tabular-nums; line-height: 1.1; }
    .stat-label { font-size: 12px; color: var(--muted); margin-top: 3px; }
    .section-title { font-size: 13px; font-weight: 600; color: var(--text); margin-bottom: 10px; }
    .provider-list { border: 1px solid var(--border); border-radius: 12px; overflow: hidden; margin-bottom: 32px; background: var(--card); }
    .provider-row { display: flex; align-items: center; gap: 14px; padding: 14px 18px; border-bottom: 1px solid var(--border-soft); text-decoration: none; color: inherit; }
    .provider-row:last-child { border-bottom: none; }
    .provider-row.clickable:hover { background: var(--bg); cursor: pointer; }
    .provider-favicon { width: 24px; height: 24px; border-radius: 6px; flex: none; }
    .provider-name { font-weight: 600; font-size: 14px; color: var(--text); }
    .provider-meta { font-size: 12.5px; color: var(--muted); margin-top: 2px; }
    .status-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--success); display: inline-block; }
    .status-text { font-size: 11.5px; color: var(--success); font-weight: 500; }
    .connect-btn { padding: 7px 14px; border-radius: 7px; border: none; background: var(--accent); color: var(--accent-text); font-size: 12.5px; font-weight: 500; cursor: pointer; flex: none; font-family: inherit; }
    .not-configured { font-size: 12px; color: var(--muted-soft); flex: none; }
    code.envvar { color: var(--muted); background: var(--border-soft); padding: 1px 5px; border-radius: 4px; font-size: 11.5px; }
"""


def _page_shell(title: str, hub_email: str, body: str, active_nav: str = "connected") -> str:
    nav_items = [
        ("connected", "Connected accounts", "/connected-accounts"),
        ("crm-ops", "CRM Ops", "/crm-ops"),
    ]
    nav_html = "".join(
        f'<a href="{href}" class="nav-item {"active" if key == active_nav else ""}">{html.escape(label)}</a>'
        for key, label, href in nav_items
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>{_PAGE_STYLES}{_EXTRA_STYLES}</style>
  <script>
    try {{
      var t = localStorage.getItem("hub_theme");
      if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
    }} catch (e) {{}}
    // ponytail: no WebSocket/SSE push exists, so another tab's changes (connect/disconnect,
    // reauth) only show up here on reload — poll instead of a real push channel.
    // Only reload when the tab is actually visible, so a background tab doesn't yank state.
    setInterval(function() {{ if (document.visibilityState === "visible") location.reload(); }}, 30000);
  </script>
</head>
<body>
  <div class="shell">
    <div class="sidebar">
      <div class="sidebar-logo">
        <div class="logo-badge"></div>
        <div class="logo-text">Connector Hub</div>
      </div>
      {nav_html}
      <div class="sidebar-user">
        <div class="sidebar-avatar">{html.escape(hub_email[:1].upper())}</div>
        <div style="flex:1 1 auto; min-width:0;">
          <div class="sidebar-email">{html.escape(hub_email)}</div>
          <form method="post" action="/logout"><button type="submit" class="logout-btn">Log out</button></form>
        </div>
      </div>
      <button type="button" class="theme-toggle-btn" onclick="(function(){{
        var cur = document.documentElement.getAttribute('data-theme') || (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
        var next = cur === 'dark' ? 'light' : 'dark';
        document.documentElement.setAttribute('data-theme', next);
        try {{ localStorage.setItem('hub_theme', next); }} catch (e) {{}}
      }})()">Toggle theme</button>
    </div>
    <div class="main">
      <div class="main-inner">
        {body}
      </div>
    </div>
  </div>
</body>
</html>"""


def _status_row_dot_and_text(conn: dict) -> str:
    status = conn["status"]
    if status == "active":
        return '<span class="status-dot"></span><span class="status-text">Connected</span>'
    if status == "needs_reauth":
        return '<span class="status-dot" style="background:var(--danger);"></span><span class="status-text" style="color:var(--danger);">Needs reauthorization</span>'
    return f'<span class="status-text" style="color:var(--muted);">{html.escape(status)}</span>'


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


def _connected_row(conn: dict, csrf: str) -> str:
    provider = conn["provider"]
    name = html.escape(PROVIDER_DISPLAY_NAMES.get(provider, provider))
    tool_count = _tool_count(provider)
    return f"""
    <div class="provider-row">
      <a href="/connected-accounts/{html.escape(provider)}" style="display:flex; align-items:center; gap:14px; flex:1 1 auto; min-width:0; text-decoration:none; color:inherit;">
        <img src="{favicon_url(provider)}" class="provider-favicon" alt="">
        <div style="flex:1 1 auto; min-width:0;">
          <div style="display:flex; align-items:center; gap:8px;">
            <span class="provider-name">{name}</span>
            {_status_row_dot_and_text(conn)}
          </div>
          <div class="provider-meta">{tool_count} tools · last used {html.escape(_fmt_time(conn.get("last_used_at")))}</div>
        </div>
      </a>
      <div style="font-size:13px; color:var(--muted-soft); flex:none; display:flex; align-items:center; gap:14px;">
        <a href="/connected-accounts/{html.escape(provider)}">View tools →</a>
        <form method="post" action="/connected-accounts/disconnect" onsubmit="return confirm('Disconnect {name}? This deletes its stored credentials.');">
          <input type="hidden" name="provider" value="{html.escape(provider)}">
          <input type="hidden" name="csrf" value="{html.escape(csrf)}">
          <button type="submit" style="padding:6px 12px; border-radius:7px; border:1px solid var(--border); background:transparent; color:var(--danger); font-size:12px; font-weight:500; cursor:pointer; font-family:inherit;">Disconnect</button>
        </form>
      </div>
    </div>
    """


def _available_row(provider: str) -> str:
    # No tool count here: tool names/descriptions are only shown to a user
    # who has actually connected the provider (provider_detail.py gates on
    # that) — an uncounted "ready to connect" row keeps that boundary at the
    # list level too, instead of leaking the tool surface before connecting.
    name = html.escape(PROVIDER_DISPLAY_NAMES[provider])
    return f"""
    <div class="provider-row">
      <img src="{favicon_url(provider)}" class="provider-favicon" alt="">
      <div style="flex:1 1 auto; min-width:0;">
        <div class="provider-name">{name}</div>
        <div class="provider-meta">OAuth app configured</div>
      </div>
      <a href="/connect/{provider}/start"><button type="button" class="connect-btn">Connect</button></a>
    </div>
    """


def _not_configured_row(provider: str) -> str:
    name = html.escape(PROVIDER_DISPLAY_NAMES[provider])
    config = PROVIDER_OAUTH_CONFIG[provider]
    return f"""
    <div class="provider-row">
      <img src="{favicon_url(provider)}" class="provider-favicon" alt="" style="opacity:0.45;">
      <div style="flex:1 1 auto; min-width:0;">
        <div class="provider-name" style="color:var(--muted);">{name}</div>
        <div class="provider-meta">needs <code class="envvar">{html.escape(config['client_id_env'])}</code> and <code class="envvar">{html.escape(config['client_secret_env'])}</code></div>
      </div>
      <div class="not-configured">Not configured</div>
    </div>
    """


def _page(hub_email: str, connections_rows: list[dict], csrf: str, error: str | None = None) -> str:
    connected_by_provider = {c["provider"]: c for c in connections_rows}
    connected_count = len(connected_by_provider)
    total_providers = len(_ALL_PROVIDER_KEYS)
    total_tools_live = sum(_tool_count(p) for p in connected_by_provider)

    error_html = f'<div class="scope-box" style="border-color:var(--danger-border); color:var(--danger); margin-bottom:20px;">{html.escape(error)}</div>' if error else ""

    connected_html = "".join(_connected_row(connected_by_provider[p], csrf) for p in _ALL_PROVIDER_KEYS if p in connected_by_provider)
    if not connected_html:
        connected_html = '<div style="padding:24px 18px; text-align:center; color:var(--muted); font-size:13px;">Nothing connected yet — connect a provider below to get started.</div>'

    other_rows = []
    for provider in _ALL_PROVIDER_KEYS:
        if provider == "freshdesk" or provider in connected_by_provider:
            continue
        other_rows.append(_available_row(provider) if _oauth_app_configured(provider) else _not_configured_row(provider))

    freshdesk_section = ""
    if "freshdesk" not in connected_by_provider:
        freshdesk_tool_count = _tool_count("freshdesk")
        freshdesk_section = f"""
        <div class="provider-row" style="display:block;">
          <div class="provider-name" style="margin-bottom:2px;">Freshdesk</div>
          <div class="provider-meta" style="margin-bottom:14px;">{freshdesk_tool_count} tools · no OAuth app yet — connect with your domain and API key instead</div>
          <form method="post" action="/connected-accounts/connect-freshdesk">
            <input type="hidden" name="csrf" value="{csrf}">
            <label for="freshdesk_domain">Freshdesk domain</label>
            <input id="freshdesk_domain" name="freshdesk_domain" placeholder="yourcompany.freshdesk.com" required autocomplete="off">
            <label for="freshdesk_api_key">Freshdesk API key</label>
            <input id="freshdesk_api_key" name="freshdesk_api_key" type="password" placeholder="Found under Profile Settings → API Key" required autocomplete="off">
            <div class="actions">
              <button type="submit" style="flex:none; background: var(--accent); color: var(--accent-text);">Connect Freshdesk</button>
            </div>
          </form>
        </div>
        """

    body = f"""
      <div style="margin-bottom:28px;">
        <h1 class="page-title">Connected accounts</h1>
        <div class="page-sub">Tools Claude, ChatGPT, and other agents can use through this hub.</div>
      </div>
      {error_html}
      <div class="stat-row">
        <div><div class="stat-value">{connected_count} of {total_providers}</div><div class="stat-label">providers connected</div></div>
        <div><div class="stat-value">{total_tools_live}</div><div class="stat-label">tools available</div></div>
      </div>

      <div class="section-title">Connected</div>
      <div class="provider-list">{connected_html}</div>

      <div class="section-title">Available to connect</div>
      <div class="provider-list">{freshdesk_section}{"".join(other_rows)}</div>
    """
    return _page_shell("Connected accounts", hub_email, body)


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
