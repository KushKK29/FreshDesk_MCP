"""Per-provider tool detail page: shows every tool a connected (or
connectable) provider exposes, with its real name and description pulled
live from the adapter's own ToolSpec list — never hand-maintained copy that
drifts from the actual tool surface — plus how to point ChatGPT, Claude, or
any other MCP client at this server.

Mounted the same way connected_accounts.py is, behind the hub-login gate.
"""
import html

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

try:
    import auth
    import connections
    from adapters import ADAPTERS
    from connected_accounts import PROVIDER_DISPLAY_NAMES, PROVIDER_DOMAINS, favicon_url, _page_shell, _fmt_time, _EXTRA_STYLES
    from login_pages import SESSION_COOKIE_NAME
    from oauth_server import BASE_URL
except ImportError:
    from src import auth
    from src import connections
    from src.adapters import ADAPTERS
    from src.connected_accounts import PROVIDER_DISPLAY_NAMES, PROVIDER_DOMAINS, favicon_url, _page_shell, _fmt_time, _EXTRA_STYLES
    from src.login_pages import SESSION_COOKIE_NAME
    from src.oauth_server import BASE_URL

router = APIRouter()

_DETAIL_STYLES = """
    .backlink { display:inline-flex; align-items:center; gap:6px; font-size:13px; color:var(--muted); margin-bottom:20px; text-decoration:none; }
    .backlink:hover { color:var(--text); }
    .detail-header { display:flex; align-items:center; gap:14px; margin-bottom:6px; }
    .detail-favicon { width:32px; height:32px; border-radius:7px; }
    .detail-title { margin:0; font-size:21px; font-weight:700; letter-spacing:-0.01em; }
    .detail-sub { font-size:13.5px; color:var(--muted); margin-bottom:24px; margin-left:46px; }
    .setup-heading { font-size:14px; font-weight:600; margin-bottom:4px; }
    .setup-desc { font-size:13px; color:var(--muted); margin-bottom:16px; }
    .url-box { border:1px solid var(--border); border-radius:10px; overflow:hidden; background:var(--card); margin-bottom:14px; }
    .url-box-label { padding:12px 16px; border-bottom:1px solid var(--border-soft); font-size:13px; font-weight:600; }
    .url-box-code { margin:14px 16px; font-size:12.5px; font-family:"JetBrains Mono", ui-monospace, monospace; background:var(--text); color:var(--bg); border-radius:8px; padding:12px 14px; word-break:break-all; }
    .setup-grid { display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-bottom:36px; }
    .setup-card { border:1px solid var(--border); border-radius:10px; padding:16px; background:var(--card); }
    .setup-card.wide { grid-column:span 2; }
    .setup-card-title { font-size:13px; font-weight:600; margin-bottom:8px; }
    .setup-card ol { margin:0; padding-left:18px; font-size:12.5px; color:var(--text); line-height:1.8; }
    .setup-card p { margin:0; font-size:12.5px; color:var(--text); line-height:1.7; }
    .mono { font-family:"JetBrains Mono", ui-monospace, monospace; }
    .tool-row { border:1px solid var(--border); border-radius:10px; padding:13px 16px; background:var(--card); display:flex; align-items:flex-start; gap:12px; margin-bottom:8px; }
    .tool-name { font-size:12px; font-weight:500; padding:3px 8px; background:var(--border-soft); border-radius:6px; white-space:nowrap; flex:none; }
    .tool-desc { flex:1 1 auto; font-size:13px; color:var(--text); line-height:1.5; }
    .tool-confirm-badge { font-size:10.5px; color:var(--danger); background:var(--danger-bg); padding:3px 8px; border-radius:6px; white-space:nowrap; flex:none; font-weight:500; }
"""


def _setup_instructions_html() -> str:
    url = html.escape(BASE_URL)
    return f"""
    <div style="margin-bottom:36px;">
      <div class="setup-heading">Use these tools in your agent</div>
      <div class="setup-desc">Every connected provider's tools are reachable through the same MCP server URL — connect it once, every tool from every provider you've linked shows up automatically.</div>

      <div class="url-box">
        <div class="url-box-label">Server URL</div>
        <div class="url-box-code mono">{url}</div>
      </div>

      <div class="setup-grid">
        <div class="setup-card">
          <div class="setup-card-title">ChatGPT</div>
          <ol>
            <li>Settings → Connectors → Add custom MCP server</li>
            <li>Paste the server URL above</li>
            <li>Authentication: OAuth</li>
            <li>Log in with your hub account, approve access</li>
          </ol>
        </div>
        <div class="setup-card">
          <div class="setup-card-title">Claude (claude.ai / Claude Code)</div>
          <ol>
            <li>Settings → Connectors → Add custom connector</li>
            <li>Paste the server URL above</li>
            <li>Approve the OAuth consent screen</li>
            <li>In Claude Code: <span class="mono">/mcp</span> → Reconnect</li>
          </ol>
        </div>
        <div class="setup-card wide">
          <div class="setup-card-title">Any other MCP-compatible agent</div>
          <p>This server speaks standard MCP over streamable-http with OAuth 2.1 discovery (RFC 8414 / RFC 9728). Point any MCP client at the server URL above — client_id/secret are issued automatically via dynamic client registration, no manual app setup needed on your end.</p>
        </div>
      </div>
    </div>
    """


def _tool_row_html(spec) -> str:
    confirm_badge = '<div class="tool-confirm-badge">needs confirm</div>' if spec.requires_confirm else ""
    return f"""
    <div class="tool-row">
      <div class="tool-name mono">{html.escape(spec.name)}</div>
      <div class="tool-desc">{html.escape(spec.description)}</div>
      {confirm_badge}
    </div>
    """


@router.get("/connected-accounts/{provider}")
def provider_detail_page(provider: str, request: Request):
    user = auth.resolve_session(request.cookies.get(SESSION_COOKIE_NAME))
    if not user:
        return RedirectResponse(f"/login?next=/connected-accounts/{provider}", status_code=302)

    if provider not in PROVIDER_DISPLAY_NAMES or provider not in ADAPTERS:
        return RedirectResponse("/connected-accounts", status_code=302)

    name = html.escape(PROVIDER_DISPLAY_NAMES[provider])
    adapter = ADAPTERS[provider]
    tools = adapter.tools()

    conn = connections.get_connection(user.user_id, provider)
    if conn:
        status_badge = '<span style="display:inline-flex; align-items:center; gap:5px; padding:3px 9px; border-radius:999px; background:var(--success-bg);"><span style="width:6px; height:6px; border-radius:50%; background:var(--success);"></span><span style="font-size:11px; color:var(--success); font-weight:600;">Connected</span></span>'
        meta = f"{len(tools)} tools · last used {html.escape(_fmt_time(conn.get('last_used_at')))}"
    else:
        status_badge = ""
        meta = f"{len(tools)} tools · not connected yet"

    tools_html = "".join(_tool_row_html(t) for t in tools)

    body = f"""
      <a href="/connected-accounts" class="backlink">← Connected accounts</a>

      <div class="detail-header">
        <img src="{favicon_url(provider)}" class="detail-favicon" alt="">
        <div>
          <div style="display:flex; align-items:center; gap:9px;">
            <h1 class="detail-title">{name}</h1>
            {status_badge}
          </div>
        </div>
      </div>
      <div class="detail-sub">{meta}</div>

      {_setup_instructions_html()}

      <div class="setup-heading">Available tools</div>
      <div class="setup-desc">What your agent can call once {name} is connected.</div>
      {tools_html}
    """
    return HTMLResponse(_page_shell(f"{PROVIDER_DISPLAY_NAMES[provider]} — tools", user.email, body).replace("</style>", _DETAIL_STYLES + "</style>"))
