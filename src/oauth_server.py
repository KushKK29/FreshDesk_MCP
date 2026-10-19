"""OAuth 2.1 authorization server for the Freshdesk connector.

This server IS the identity provider an MCP client (Claude, ChatGPT) talks
to — not Freshdesk. Design follows the standard MCP OAuth pattern:

- Dynamic Client Registration (RFC 7591): any MCP client can self-register
  and get a client_id, no manual app registration needed on our side.
- Authorization endpoint with a consent page: the user proves they have
  Freshdesk access by entering their Freshdesk domain + API key here (this
  connector's one and only account system — no separate signup/password,
  the Freshdesk API key itself is the credential).
- PKCE (S256) required on every authorization request (OAuth 2.1 mandates
  this for public clients — MCP clients are public clients).
- Token endpoint: authorization_code and refresh_token grants, single-use
  codes, rotating refresh tokens.
- Storage: MongoDB. One document per grant carries the whole lifecycle —
  consent writes the code; token exchange atomically claims it and fills in
  the token hashes; refresh rotates them in place. Only hashes are stored,
  never raw tokens/codes/API keys in plaintext at rest... except the
  Freshdesk API key, which genuinely needs to be used again on every tool
  call, so it's encrypted-at-rest conceptually but stored as-is here for the
  demo — see PLAN.md for the production fix (field-level encryption, or a
  secrets manager instead of the DB).
- RFC 8414 discovery endpoint so MCP clients can find all of this automatically.

This is a materially bigger build than "paste your API key into .env" (the
other option the assignment allows) — built this way because the ask was
specifically for an OAuth 2.1 flow in the Humantic MCP server's style.
"""
import hashlib
import html
import os
import secrets
import time
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

try:
    import freshdesk_client as fd  # flat import, local dev (--app-dir src)
    import auth
    import db as _dbmod
    import connections
except ImportError:
    from src import freshdesk_client as fd  # package-relative, Vercel (src.oauth_server:app)
    from src import auth
    from src import db as _dbmod
    from src import connections

app = FastAPI()

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
BASE_URL = os.environ.get("OAUTH_BASE_URL", "http://localhost:8004").rstrip("/")
# This server's own public URL. rstrip is required: if OAUTH_BASE_URL has a trailing slash
# (confirmed on Vercel), every endpoint built from it got a double slash — e.g.
# "https://fresh-desk-mcp.vercel.app//oauth/authorize" — which ChatGPT rejected outright with
# "Some app settings were rejected. Check the server URL..." observed live.

CODE_TTL_SECONDS = 60
ACCESS_TTL_SECONDS = 60 * 60
REFRESH_TTL_SECONDS = 90 * 24 * 60 * 60
PENDING_AUTH_TTL_SECONDS = 600  # consent-flow window: how long a pending /oauth/authorize request stays valid

_db = _dbmod.db  # shared connection/db (src/db.py) — one pool for the whole connector, not one per module
_clients = _db["oauth_clients"]       # dynamically registered MCP clients
_grants = _db["oauth_grants"]         # one doc per authorization: code -> tokens lifecycle
_pending_authorizations = _db["pending_authorizations"]
# Was an in-memory `_sessions = {}` dict — lost every pending consent flow on
# a restart and couldn't be shared across instances (PLAN.md Limitations).
# Now Mongo-backed, same way _clients/_grants are; a TTL index does the
# cleanup, and _authorize_decision still checks created_at explicitly below
# since a TTL index can lag its target by up to ~60s (Mongo docs) — that lag
# is fine for cleanup, not for the correctness boundary.
_pending_authorizations.create_index("created_at", expireAfterSeconds=PENDING_AUTH_TTL_SECONDS)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _b64url_sha256(verifier: str) -> str:
    import base64
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _rand_token(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(24)


# ── Discovery (RFC 8414) ────────────────────────────────────────────────

@app.get("/.well-known/oauth-authorization-server")
def discovery():
    return {
        "issuer": BASE_URL,
        "authorization_endpoint": f"{BASE_URL}/oauth/authorize",
        "token_endpoint": f"{BASE_URL}/oauth/token",
        "registration_endpoint": f"{BASE_URL}/oauth/register",
        "revocation_endpoint": f"{BASE_URL}/oauth/revoke",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported": ["tickets:read"],
        "token_endpoint_auth_methods_supported": ["none"],
    }


# ── Dynamic Client Registration (RFC 7591) ──────────────────────────────

@app.post("/oauth/register")
async def register(request: Request):
    body = await request.json()
    redirect_uris = body.get("redirect_uris")
    if not isinstance(redirect_uris, list) or not redirect_uris:
        raise HTTPException(status_code=400, detail="redirect_uris must be a non-empty array")
    for uri in redirect_uris:
        if not isinstance(uri, str) or not uri.startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail=f"invalid redirect_uri: {uri!r}")

    client_id = _rand_token("client_")
    _clients.insert_one({
        "client_id": client_id,
        "client_name": body.get("client_name", "Unnamed MCP client")[:200],
        "redirect_uris": redirect_uris,
        "created_at": time.time(),
    })
    return JSONResponse(status_code=201, content={
        "client_id": client_id,
        "client_name": body.get("client_name"),
        "redirect_uris": redirect_uris,
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    })


# ── Authorize + consent ─────────────────────────────────────────────────

# Shared design system (PRD-redesign): flat, bordered, no gradients — Inter
# font, light by default, dark via prefers-color-scheme. Same class names
# as before (card/logo-row/logo-badge/logo-text/subtitle/scope-box/
# error-icon/actions) so every page that already composes them (consent,
# error, login, signup) keeps working unchanged.
_PAGE_STYLES = """
    :root {
      color-scheme: light dark;
      --bg: #fafafa;
      --card: #ffffff;
      --border: #e4e4e7;
      --border-soft: #f0f0f1;
      --text: #18181b;
      --muted: #71717a;
      --muted-soft: #a1a1aa;
      --accent: #18181b;
      --accent-hover: #27272a;
      --accent-text: #ffffff;
      --danger: #b45309;
      --danger-bg: #fef3c7;
      --danger-border: #fde68a;
      --success: #16a34a;
      --success-bg: #dcfce7;
    }
    @media (prefers-color-scheme: dark) {
      :root:not([data-theme="light"]) {
        --bg: #0a0a0b;
        --card: #111113;
        --border: #27272a;
        --border-soft: #1c1c1f;
        --text: #f4f4f5;
        --muted: #a1a1aa;
        --muted-soft: #71717a;
        --accent: #f4f4f5;
        --accent-hover: #ffffff;
        --accent-text: #18181b;
        --danger: #fbbf24;
        --danger-bg: #422006;
        --danger-border: #713f12;
        --success: #4ade80;
        --success-bg: #052e16;
      }
    }
    :root[data-theme="dark"] {
      --bg: #0a0a0b;
      --card: #111113;
      --border: #27272a;
      --border-soft: #1c1c1f;
      --text: #f4f4f5;
      --muted: #a1a1aa;
      --muted-soft: #71717a;
      --accent: #f4f4f5;
      --accent-hover: #ffffff;
      --accent-text: #18181b;
      --danger: #fbbf24;
      --danger-bg: #422006;
      --danger-border: #713f12;
      --success: #4ade80;
      --success-bg: #052e16;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      background: var(--bg);
      color: var(--text);
      font-family: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      -webkit-font-smoothing: antialiased;
      padding: 24px;
    }
    .card {
      width: 100%;
      max-width: 400px;
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 32px 28px;
    }
    .logo-row {
      display: flex;
      align-items: center;
      gap: 9px;
      margin-bottom: 24px;
    }
    .logo-badge {
      width: 22px;
      height: 22px;
      border-radius: 6px;
      background: var(--text);
      flex-shrink: 0;
    }
    .logo-text { font-size: 14px; font-weight: 600; color: var(--text); }
    h1 {
      font-size: 19px;
      font-weight: 700;
      letter-spacing: -0.01em;
      margin: 0 0 6px;
      line-height: 1.4;
    }
    .subtitle {
      color: var(--muted);
      font-size: 13px;
      line-height: 1.6;
      margin: 0 0 24px;
    }
    .scope-box {
      background: var(--bg);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 14px 16px;
      margin-bottom: 24px;
      font-size: 12.5px;
      color: var(--muted);
    }
    .scope-box strong { color: var(--text); }
    .scope-box ul { margin: 8px 0 0; padding-left: 18px; }
    .scope-box li { margin: 4px 0; }
    label {
      display: block;
      font-size: 12.5px;
      font-weight: 500;
      color: var(--text);
      margin-bottom: 6px;
      margin-top: 14px;
    }
    label:first-of-type { margin-top: 0; }
    input {
      width: 100%;
      padding: 9px 11px;
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 8px;
      color: var(--text);
      font-size: 13.5px;
      font-family: inherit;
      transition: border-color 0.15s;
    }
    input:focus {
      outline: none;
      border-color: var(--text);
    }
    input::placeholder { color: var(--muted-soft); }
    .hint { font-size: 11.5px; color: var(--muted); margin-top: 6px; }
    .actions {
      display: flex;
      gap: 10px;
      margin-top: 22px;
    }
    button {
      flex: 1;
      padding: 10px 18px;
      border-radius: 8px;
      font-size: 13.5px;
      font-weight: 600;
      font-family: inherit;
      cursor: pointer;
      border: 1px solid transparent;
      transition: background 0.15s, border-color 0.15s;
    }
    button[value="approve"], button[type="submit"]:not([value]) {
      background: var(--accent);
      color: var(--accent-text);
    }
    button[value="approve"]:hover, button[type="submit"]:not([value]):hover { background: var(--accent-hover); }
    button[value="deny"] {
      background: transparent;
      border-color: var(--border);
      color: var(--muted);
    }
    button[value="deny"]:hover { border-color: var(--muted-soft); color: var(--text); }
    .footer-note {
      margin-top: 18px;
      font-size: 11.5px;
      color: var(--muted-soft);
      text-align: center;
      line-height: 1.5;
    }
    .error-icon {
      width: 40px;
      height: 40px;
      border-radius: 10px;
      background: var(--danger-bg);
      border: 1px solid var(--danger-border);
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 18px;
      color: var(--danger);
      margin-bottom: 18px;
    }
"""


def _consent_page(session_id: str, client_name: str, csrf: str, hub_email: str, connected_providers: list[str]) -> str:
    # client_name comes from dynamic client registration (any caller, no
    # review) and hub_email from signup — both untrusted, HTML-escaped
    # before interpolation so neither can break out of the markup.
    client_name = html.escape(client_name)
    hub_email = html.escape(hub_email)
    if connected_providers:
        providers_line = f"It will see tools for: <strong>{html.escape(', '.join(connected_providers))}</strong> — whatever you've connected on the Connected accounts page."
    else:
        providers_line = 'You haven\'t connected any providers yet — connect one from <a href="/connected-accounts" style="color:var(--accent);">Connected accounts</a> after approving, and its tools will appear automatically.'
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Connect to Connector Hub</title>
  <style>{_PAGE_STYLES}</style>
</head>
<body>
  <div class="card">
    <div class="logo-row">
      <div class="logo-badge">FD</div>
      <div class="logo-text">Connector Hub</div>
    </div>
    <h1>{client_name} wants to access your Connector Hub account</h1>
    <p class="subtitle">Signed in as {hub_email}.</p>
    <div class="scope-box">
      <strong>This will allow {client_name} to:</strong>
      <ul>
        <li>Use tools for every provider you've connected (Freshdesk, Salesforce, HubSpot, etc.)</li>
      </ul>
      {providers_line}
    </div>
    <form method="post" action="/oauth/authorize/decision">
      <input type="hidden" name="session_id" value="{session_id}">
      <input type="hidden" name="csrf" value="{csrf}">
      <div class="actions">
        <button type="submit" name="decision" value="deny">Deny</button>
        <button type="submit" name="decision" value="approve">Approve access</button>
      </div>
    </form>
    <p class="footer-note">Provider credentials are managed separately on Connected accounts and are never shared with {client_name}.</p>
  </div>
</body>
</html>"""


def _error_page(title: str, message: str) -> str:
    # Escaped here, not at each call site — callers sometimes interpolate
    # untrusted path/query params (e.g. connect_oauth.py's provider) into
    # title/message, so this is the one place that must not be skippable.
    title = html.escape(title)
    message = html.escape(message)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>{_PAGE_STYLES}</style>
</head>
<body>
  <div class="card">
    <div class="error-icon">⚠</div>
    <h1>{title}</h1>
    <p class="subtitle">{message}</p>
  </div>
</body>
</html>"""


@app.get("/oauth/authorize")
def authorize(
    request: Request,
    client_id: str = Query(...),
    redirect_uri: str = Query(...),
    response_type: str = Query(...),
    code_challenge: str = Query(...),
    code_challenge_method: str = Query(...),
    state: str | None = Query(None),
):
    client = _clients.find_one({"client_id": client_id})
    # redirect_uri is untrusted until proven registered — on ANY validation failure,
    # show an error page, never redirect (OAuth 2.1 / RFC 9700 open-redirect guard).
    if (
        not client
        or response_type != "code"
        or redirect_uri not in client["redirect_uris"]
        or len(code_challenge) < 43
        or code_challenge_method != "S256"
    ):
        return HTMLResponse(_error_page("Invalid authorization request", "This link is invalid or has expired. Start again from your MCP client."), status_code=400)

    # Hub login gate: the MCP client's OAuth request only proceeds to the
    # Freshdesk consent screen once there's a real hub account behind it —
    # mirrors Humantic's own mcp-oauth.js, which checks req.session.isLoggedin
    # before rendering its MCP consent page and redirects to the login/home
    # page otherwise. Here that's a stateless redirect (no server session
    # framework in FastAPI by default) back to this same URL after login.
    hub_user = auth.resolve_session(request.cookies.get(login_pages.SESSION_COOKIE_NAME))
    if not hub_user:
        return RedirectResponse(f"/login?{urlencode({'next': str(request.url)})}", status_code=302)

    session_id = secrets.token_urlsafe(16)
    csrf = secrets.token_urlsafe(16)
    _pending_authorizations.insert_one({
        "session_id": session_id,
        "client_id": client_id,
        "client_name": client["client_name"],
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "state": state,
        "csrf": csrf,
        "hub_user_id": hub_user.user_id,
        "created_at": time.time(),
    })
    connected_providers = [c["provider"] for c in connections.list_connections_for_user(hub_user.user_id) if c["status"] == "active"]
    return HTMLResponse(_consent_page(session_id, client["client_name"], csrf, hub_user.email, connected_providers))


@app.post("/oauth/authorize/decision")
def authorize_decision(
    request: Request,
    session_id: str = Form(...),
    csrf: str = Form(...),
    decision: str = Form(...),
):
    # The GET that rendered this form already required a hub login — the
    # POST that actually issues the grant must require it too, and must be
    # the SAME hub user the pending record was created for. Without this,
    # anyone who obtains a leaked session_id+csrf pair (Referer header,
    # shared browser, server logs) could mint a grant tied to the victim's
    # hub_user_id.
    hub_user = auth.resolve_session(request.cookies.get(login_pages.SESSION_COOKIE_NAME))
    oauth_req = _pending_authorizations.find_one_and_delete({"session_id": session_id})
    # Single-use: find_one_and_delete both reads and consumes the pending
    # record atomically, so the same consent page can't be submitted twice.
    if (
        not oauth_req
        or time.time() - oauth_req["created_at"] > PENDING_AUTH_TTL_SECONDS
        or csrf != oauth_req["csrf"]
        or not hub_user
        or hub_user.user_id != oauth_req["hub_user_id"]
    ):
        # Deliberately one error for "expired" and "bad csrf" — distinguishing
        # them in the response would tell an attacker which guess was closer.
        raise HTTPException(status_code=400, detail="invalid or expired consent session")

    target_params = {}
    if oauth_req["state"] is not None:
        target_params["state"] = oauth_req["state"]

    if decision != "approve":
        target_params["error"] = "access_denied"
        return RedirectResponse(f"{oauth_req['redirect_uri']}?{urlencode(target_params)}", status_code=302)

    # No provider credentials are collected here anymore — this grant is
    # the MCP client's access to the hub account, nothing more. Which
    # providers' tools it can see depends entirely on what's connected on
    # /connected-accounts at call time (mcp_http_server.py), not on anything
    # decided during this consent step (connector-hub plan: credential split
    # from the OAuth grant, extended to cover every provider, not just
    # Freshdesk's original M0a design).
    code = _rand_token("code_")
    _grants.insert_one({
        "code_hash": _sha256(code),
        "client_id": oauth_req["client_id"],
        "redirect_uri": oauth_req["redirect_uri"],
        "code_challenge": oauth_req["code_challenge"],
        "code_expires_at": time.time() + CODE_TTL_SECONDS,
        "user_id": oauth_req["hub_user_id"],  # the hub account that approved this grant — PRD Section 7.1
        "access_token_hash": None,
        "refresh_token_hash": None,
        "revoked_at": None,
        "created_at": time.time(),
    })
    target_params["code"] = code
    return RedirectResponse(f"{oauth_req['redirect_uri']}?{urlencode(target_params)}", status_code=302)


# ── Token endpoint ───────────────────────────────────────────────────────

def _token_reply(access_token: str, refresh_token: str) -> dict:
    return {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": ACCESS_TTL_SECONDS,
        "refresh_token": refresh_token,
    }


@app.post("/oauth/token")
async def token(request: Request):
    form = await request.form()
    grant_type = form.get("grant_type")
    now = time.time()
    access_token = _rand_token("at_")
    refresh_token = _rand_token("rt_")

    if grant_type == "authorization_code":
        code = form.get("code")
        code_verifier = form.get("code_verifier")
        redirect_uri = form.get("redirect_uri")
        client_id = form.get("client_id")
        if not all([code, code_verifier, redirect_uri, client_id]):
            raise HTTPException(status_code=400, detail="invalid_request")

        result = _grants.update_one(
            {
                "code_hash": _sha256(code),
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "code_challenge": _b64url_sha256(code_verifier),
                "code_expires_at": {"$gte": now},
                "revoked_at": None,
            },
            {"$set": {
                "access_token_hash": _sha256(access_token),
                "access_expires_at": now + ACCESS_TTL_SECONDS,
                "refresh_token_hash": _sha256(refresh_token),
                "refresh_expires_at": now + REFRESH_TTL_SECONDS,
                "code_hash": None,  # single-use: code can't be claimed again
            }},
        )
        if result.modified_count != 1:
            raise HTTPException(status_code=400, detail="invalid_grant: unknown, expired, or already-used code, or PKCE mismatch")
        return _token_reply(access_token, refresh_token)

    if grant_type == "refresh_token":
        refresh = form.get("refresh_token")
        client_id = form.get("client_id")
        if not refresh or not client_id:
            raise HTTPException(status_code=400, detail="invalid_request")

        result = _grants.update_one(
            {
                "refresh_token_hash": _sha256(refresh),
                "client_id": client_id,
                "refresh_expires_at": {"$gte": now},
                "revoked_at": None,
            },
            {"$set": {
                "access_token_hash": _sha256(access_token),
                "access_expires_at": now + ACCESS_TTL_SECONDS,
                "refresh_token_hash": _sha256(refresh_token),
                "refresh_expires_at": now + REFRESH_TTL_SECONDS,
            }},
        )
        if result.modified_count != 1:
            raise HTTPException(status_code=400, detail="invalid_grant: unknown, expired, or revoked refresh token")
        return _token_reply(access_token, refresh_token)

    raise HTTPException(status_code=400, detail="unsupported_grant_type")


@app.post("/oauth/revoke")
async def revoke(request: Request):
    form = await request.form()
    tok = form.get("token")
    if tok:
        grant = _grants.find_one({"$or": [{"access_token_hash": _sha256(tok)}, {"refresh_token_hash": _sha256(tok)}]})
        _grants.update_many(
            {"$or": [{"access_token_hash": _sha256(tok)}, {"refresh_token_hash": _sha256(tok)}]},
            {"$set": {"revoked_at": time.time()}},
        )
        if grant:
            # PRD F6: revoking the MCP OAuth grant also disconnects the
            # provider connection it was gating — deletes the encrypted
            # credentials, not just the hub-session token.
            connections.disconnect(grant["user_id"], "freshdesk")
    return JSONResponse(status_code=200, content={})


# ── Connector primitives, usable once a token has been issued ──────────

def _resolve_grant(authorization_header: str | None) -> dict:
    if not authorization_header or not authorization_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing Bearer token")
    token_value = authorization_header.removeprefix("Bearer ")
    grant = _grants.find_one({
        "access_token_hash": _sha256(token_value),
        "access_expires_at": {"$gte": time.time()},
        "revoked_at": None,
    })
    if not grant:
        raise HTTPException(status_code=401, detail="invalid or expired access token")
    return grant


@app.get(
    "/tickets",
    operation_id="list_tickets",
    summary="List recent Freshdesk tickets",
    description="Lists tickets for the connected Freshdesk account, newest first, paginated. "
    "Optionally filter by status or by an updated_since timestamp (ISO 8601).",
)
def list_tickets(request: Request, status: str | None = None, updated_since: str | None = None, page: int = 1):
    grant = _resolve_grant(request.headers.get("authorization"))
    conn = connections.get_connection(grant["user_id"], "freshdesk")
    if not conn:
        raise HTTPException(status_code=409, detail="Freshdesk is not connected or needs reauthorization")
    return fd.list_tickets_for(conn["credentials"]["domain"], conn["credentials"]["api_key"], status, updated_since, page)


@app.get(
    "/tickets/{ticket_id}",
    operation_id="get_ticket",
    summary="Get one Freshdesk ticket by id",
    description="Fetches the full details of a single ticket by its numeric id. "
    "Optionally include related data such as conversations or the requester's info.",
)
def get_ticket(ticket_id: int, request: Request, include: str | None = None):
    grant = _resolve_grant(request.headers.get("authorization"))
    conn = connections.get_connection(grant["user_id"], "freshdesk")
    if not conn:
        raise HTTPException(status_code=409, detail="Freshdesk is not connected or needs reauthorization")
    return fd.get_ticket_for(conn["credentials"]["domain"], conn["credentials"]["api_key"], ticket_id, include)


@app.get(
    "/search",
    operation_id="search_tickets",
    summary="Search Freshdesk tickets",
    description="Searches tickets matching a Freshdesk query string, e.g. 'status:2 AND priority:3'.",
)
def search_tickets(request: Request, query: str):
    grant = _resolve_grant(request.headers.get("authorization"))
    conn = connections.get_connection(grant["user_id"], "freshdesk")
    if not conn:
        raise HTTPException(status_code=409, detail="Freshdesk is not connected or needs reauthorization")
    return fd.search_tickets_for(conn["credentials"]["domain"], conn["credentials"]["api_key"], query)


# ── Hub login routes (signup/login/logout) ──────────────────────────────
# Imported and mounted last, not at module top: login_pages.py imports
# _PAGE_STYLES and _error_page from this module, so importing it earlier
# (before those names exist in this module's namespace) would be a circular
# import that fails at startup.
try:
    import login_pages
    import connected_accounts
    import connect_oauth
    import provider_detail
    import crm_ops_page
    import ops_api
    from adapters import register_all_adapters
except ImportError:
    from src import login_pages
    from src import connected_accounts
    from src import connect_oauth
    from src import provider_detail
    from src import crm_ops_page
    from src import ops_api
    from src.adapters import register_all_adapters

register_all_adapters()  # every adapter module registers itself into ADAPTERS — needed by connect_oauth.py's verify() call and mcp_http_server.py's tool dispatch, whichever entrypoint runs first
app.include_router(login_pages.router)
app.include_router(connected_accounts.router)
app.include_router(provider_detail.router)
app.include_router(connect_oauth.router)
app.include_router(crm_ops_page.router)
app.include_router(ops_api.router)

# The Ops UI is a separate static site. Bearer-token auth (no cookies), so
# any origin is safe to allow; set OPS_UI_ORIGIN to lock it down in prod.
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
app.add_middleware(CORSMiddleware, allow_origins=[os.environ.get("OPS_UI_ORIGIN", "*")], allow_methods=["*"], allow_headers=["*"])
