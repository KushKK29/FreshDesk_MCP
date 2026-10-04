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
import os
import secrets
import time
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pymongo import MongoClient

try:
    import freshdesk_client as fd  # flat import, local dev (--app-dir src)
except ImportError:
    from src import freshdesk_client as fd  # package-relative, Vercel (src.oauth_server:app)

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

_mongo = MongoClient(MONGO_URI)
_db = _mongo["freshdesk_connector"]
_clients = _db["oauth_clients"]       # dynamically registered MCP clients
_grants = _db["oauth_grants"]         # one doc per authorization: code -> tokens lifecycle
_sessions = {}  # in-memory consent-flow session, keyed by a short-lived cookie-less session id


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


@app.get("/.well-known/ai-plugin.json")
def ai_plugin_manifest():
    """ChatGPT's (legacy Plugins-format) action discovery manifest. Without
    this, OAuth can succeed but ChatGPT won't know the tools exist — exactly
    the "Authentication succeeded, action discovery failed" failure mode.

    Points at /openapi-tools.json (below), NOT FastAPI's auto-generated
    /openapi.json — that one lists every route including the OAuth plumbing
    (/oauth/register, /oauth/token, etc.), which confused ChatGPT's action
    extraction into finding zero usable tools. ChatGPT needs a spec that
    contains ONLY the callable tools."""
    return {
        "schema_version": "v1",
        "name_for_human": "Freshdesk Connector",
        "name_for_model": "freshdesk_connector",
        "description_for_human": "Read-only access to your Freshdesk support tickets — list recent "
        "tickets, look up one by id, or search by status, priority, type, or free text.",
        "description_for_model": "Plugin for reading Freshdesk support tickets. Use list_tickets to browse "
        "recent tickets, get_ticket to fetch one ticket's full details by id, and search_tickets to find "
        "tickets matching a query (structured like status:2, or free text which matches subject/description). "
        "Read-only — this plugin cannot create, update, or delete tickets.",
        "auth": {
            "type": "oauth",
            "client_url": f"{BASE_URL}/oauth/authorize",
            "scope": "tickets:read",
            "authorization_url": f"{BASE_URL}/oauth/token",
            "authorization_content_type": "application/json",
        },
        "api": {
            "type": "openapi",
            "url": f"{BASE_URL}/openapi-tools.json",
        },
        "logo_url": f"{BASE_URL}/logo.png",
        "contact_email": "support@example.com",
        "legal_info_url": f"{BASE_URL}/legal",
    }


@app.get("/openapi-tools.json")
def openapi_tools_spec():
    """Minimal OpenAPI 3.1 spec containing ONLY the 3 callable tools —
    deliberately excludes every /oauth/* route so ChatGPT's action
    extraction isn't confused by the auth plumbing mixed into the full
    auto-generated /openapi.json."""
    ticket_schema = {
        "type": "object",
        "description": "A Freshdesk ticket, fields as returned by the Freshdesk API.",
    }
    return {
        "openapi": "3.1.0",
        "info": {"title": "Freshdesk Connector", "version": "1.0.0"},
        "servers": [{"url": BASE_URL}],
        "paths": {
            "/tickets": {
                "get": {
                    "operationId": "list_tickets",
                    "summary": "List recent Freshdesk tickets",
                    "description": "Lists tickets for the connected Freshdesk account, newest first, "
                    "paginated. Optionally filter by status or by an updated_since timestamp (ISO 8601).",
                    "parameters": [
                        {"name": "status", "in": "query", "required": False, "schema": {"type": "string"}},
                        {"name": "updated_since", "in": "query", "required": False, "schema": {"type": "string"}},
                        {"name": "page", "in": "query", "required": False, "schema": {"type": "integer", "default": 1}},
                    ],
                    "responses": {
                        "200": {
                            "description": "A list of tickets.",
                            "content": {"application/json": {"schema": {"type": "array", "items": ticket_schema}}},
                        }
                    },
                }
            },
            "/tickets/{ticket_id}": {
                "get": {
                    "operationId": "get_ticket",
                    "summary": "Get one Freshdesk ticket by id",
                    "description": "Fetches the full details of a single ticket by its numeric id. "
                    "Optionally include related data such as conversations or the requester's info.",
                    "parameters": [
                        {"name": "ticket_id", "in": "path", "required": True, "schema": {"type": "integer"}},
                        {"name": "include", "in": "query", "required": False, "schema": {"type": "string"}},
                    ],
                    "responses": {
                        "200": {
                            "description": "The ticket.",
                            "content": {"application/json": {"schema": ticket_schema}},
                        }
                    },
                }
            },
            "/search": {
                "get": {
                    "operationId": "search_tickets",
                    "summary": "Search Freshdesk tickets",
                    "description": "Searches tickets matching a Freshdesk query string, "
                    "e.g. 'status:2 AND priority:3'.",
                    "parameters": [
                        {"name": "query", "in": "query", "required": True, "schema": {"type": "string"}},
                    ],
                    "responses": {
                        "200": {
                            "description": "Matching tickets.",
                            "content": {"application/json": {"schema": {"type": "array", "items": ticket_schema}}},
                        }
                    },
                }
            },
        },
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

_PAGE_STYLES = """
    :root {
      color-scheme: dark;
      --bg: #0b0e14;
      --card: #141821;
      --border: #242b3a;
      --text: #e8eaed;
      --muted: #9aa4b2;
      --accent: #2563eb;
      --accent-hover: #1d4ed8;
      --danger: #ef4444;
      --danger-border: #3a1f24;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      background: radial-gradient(circle at top, #151a24, var(--bg));
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      padding: 24px;
    }
    .card {
      width: 100%;
      max-width: 440px;
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 36px 32px;
      box-shadow: 0 20px 60px rgba(0, 0, 0, 0.4);
    }
    .logo-row {
      display: flex;
      align-items: center;
      gap: 12px;
      margin-bottom: 24px;
    }
    .logo-badge {
      width: 40px;
      height: 40px;
      border-radius: 10px;
      background: linear-gradient(135deg, #2563eb, #7c3aed);
      display: flex;
      align-items: center;
      justify-content: center;
      font-weight: 700;
      font-size: 16px;
      color: white;
      flex-shrink: 0;
    }
    .logo-text { font-size: 14px; color: var(--muted); }
    h1 {
      font-size: 20px;
      font-weight: 600;
      margin: 0 0 8px;
      line-height: 1.4;
    }
    .subtitle {
      color: var(--muted);
      font-size: 14px;
      line-height: 1.6;
      margin: 0 0 24px;
    }
    .scope-box {
      background: rgba(37, 99, 235, 0.08);
      border: 1px solid rgba(37, 99, 235, 0.25);
      border-radius: 10px;
      padding: 14px 16px;
      margin-bottom: 24px;
      font-size: 13px;
      color: var(--muted);
    }
    .scope-box strong { color: var(--text); }
    .scope-box ul { margin: 8px 0 0; padding-left: 18px; }
    .scope-box li { margin: 4px 0; }
    label {
      display: block;
      font-size: 13px;
      font-weight: 500;
      color: var(--text);
      margin-bottom: 6px;
      margin-top: 18px;
    }
    label:first-of-type { margin-top: 0; }
    input {
      width: 100%;
      padding: 11px 14px;
      background: #0d1117;
      border: 1px solid var(--border);
      border-radius: 8px;
      color: var(--text);
      font-size: 14px;
      transition: border-color 0.15s;
    }
    input:focus {
      outline: none;
      border-color: var(--accent);
    }
    input::placeholder { color: #5a6472; }
    .hint { font-size: 12px; color: var(--muted); margin-top: 6px; }
    .actions {
      display: flex;
      gap: 10px;
      margin-top: 28px;
    }
    button {
      flex: 1;
      padding: 12px 20px;
      border-radius: 8px;
      font-size: 14px;
      font-weight: 600;
      cursor: pointer;
      border: 1px solid transparent;
      transition: background 0.15s, border-color 0.15s;
    }
    button[value="approve"] {
      background: var(--accent);
      color: white;
    }
    button[value="approve"]:hover { background: var(--accent-hover); }
    button[value="deny"] {
      background: transparent;
      border-color: var(--border);
      color: var(--muted);
    }
    button[value="deny"]:hover { border-color: #3a4454; color: var(--text); }
    .footer-note {
      margin-top: 20px;
      font-size: 12px;
      color: #5a6472;
      text-align: center;
      line-height: 1.5;
    }
    .error-icon {
      width: 44px;
      height: 44px;
      border-radius: 50%;
      background: rgba(239, 68, 68, 0.12);
      border: 1px solid var(--danger-border);
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 20px;
      margin-bottom: 20px;
    }
"""


def _consent_page(session_id: str, client_name: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Connect to Freshdesk</title>
  <style>{_PAGE_STYLES}</style>
</head>
<body>
  <div class="card">
    <div class="logo-row">
      <div class="logo-badge">FD</div>
      <div class="logo-text">Freshdesk Connector</div>
    </div>
    <h1>{client_name} wants to access your Freshdesk tickets</h1>
    <p class="subtitle">Sign in with your Freshdesk domain and API key to grant read-only access.</p>
    <div class="scope-box">
      <strong>This will allow {client_name} to:</strong>
      <ul>
        <li>List and search your support tickets</li>
        <li>View full ticket details</li>
      </ul>
      It will <strong>not</strong> be able to create, edit, or delete anything in Freshdesk.
    </div>
    <form method="post" action="/oauth/authorize/decision">
      <input type="hidden" name="session_id" value="{session_id}">
      <label for="freshdesk_domain">Freshdesk domain</label>
      <input id="freshdesk_domain" name="freshdesk_domain" placeholder="yourcompany.freshdesk.com" required autocomplete="off">
      <label for="freshdesk_api_key">Freshdesk API key</label>
      <input id="freshdesk_api_key" name="freshdesk_api_key" type="password" placeholder="Found under Profile Settings → API Key" required autocomplete="off">
      <div class="actions">
        <button type="submit" name="decision" value="deny">Deny</button>
        <button type="submit" name="decision" value="approve">Approve access</button>
      </div>
    </form>
    <p class="footer-note">Your credentials are used only to verify access and are never shared with {client_name} directly.</p>
  </div>
</body>
</html>"""


def _error_page(title: str, message: str) -> str:
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

    session_id = secrets.token_urlsafe(16)
    _sessions[session_id] = {
        "client_id": client_id,
        "client_name": client["client_name"],
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "state": state,
        "created_at": time.time(),
    }
    return HTMLResponse(_consent_page(session_id, client["client_name"]))


@app.post("/oauth/authorize/decision")
def authorize_decision(
    session_id: str = Form(...),
    decision: str = Form(...),
    freshdesk_domain: str = Form(""),
    freshdesk_api_key: str = Form(""),
):
    oauth_req = _sessions.pop(session_id, None)
    if not oauth_req or time.time() - oauth_req["created_at"] > 600:
        raise HTTPException(status_code=400, detail="invalid or expired consent session")

    target_params = {}
    if oauth_req["state"] is not None:
        target_params["state"] = oauth_req["state"]

    if decision != "approve":
        target_params["error"] = "access_denied"
        return RedirectResponse(f"{oauth_req['redirect_uri']}?{urlencode(target_params)}", status_code=302)

    # Verify the Freshdesk credentials actually work before issuing a code.
    resp = httpx.get(
        f"https://{freshdesk_domain}/api/v2/tickets",
        auth=(freshdesk_api_key, "X"),
        params={"per_page": 1},
        timeout=15,
    )
    if resp.status_code == 401:
        return HTMLResponse(_error_page("Couldn't verify your credentials", "That domain and API key combination was rejected by Freshdesk. Double-check both and try again."), status_code=401)
    if resp.status_code >= 400:
        return HTMLResponse(_error_page("Connection failed", f"Freshdesk returned an unexpected error (status {resp.status_code}). Please try again in a moment."), status_code=502)

    code = _rand_token("code_")
    _grants.insert_one({
        "code_hash": _sha256(code),
        "client_id": oauth_req["client_id"],
        "redirect_uri": oauth_req["redirect_uri"],
        "code_challenge": oauth_req["code_challenge"],
        "code_expires_at": time.time() + CODE_TTL_SECONDS,
        "freshdesk_domain": freshdesk_domain,
        "freshdesk_api_key": freshdesk_api_key,  # see module docstring: demo-only plaintext storage
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
        _grants.update_many(
            {"$or": [{"access_token_hash": _sha256(tok)}, {"refresh_token_hash": _sha256(tok)}]},
            {"$set": {"revoked_at": time.time()}},
        )
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
    return fd.list_tickets_for(grant["freshdesk_domain"], grant["freshdesk_api_key"], status, updated_since, page)


@app.get(
    "/tickets/{ticket_id}",
    operation_id="get_ticket",
    summary="Get one Freshdesk ticket by id",
    description="Fetches the full details of a single ticket by its numeric id. "
    "Optionally include related data such as conversations or the requester's info.",
)
def get_ticket(ticket_id: int, request: Request, include: str | None = None):
    grant = _resolve_grant(request.headers.get("authorization"))
    return fd.get_ticket_for(grant["freshdesk_domain"], grant["freshdesk_api_key"], ticket_id, include)


@app.get(
    "/search",
    operation_id="search_tickets",
    summary="Search Freshdesk tickets",
    description="Searches tickets matching a Freshdesk query string, e.g. 'status:2 AND priority:3'.",
)
def search_tickets(request: Request, query: str):
    grant = _resolve_grant(request.headers.get("authorization"))
    return fd.search_tickets_for(grant["freshdesk_domain"], grant["freshdesk_api_key"], query)
