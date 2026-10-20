"""JSON API for the standalone Ops UI (separate static repo). The HTML pages
(crm_ops_page.py etc.) render server-side; this serves the same support_ops
tools as plain JSON so a client app does the rendering.

Auth is a bearer hub-session token (auth.create_session), not the browser
cookie: the UI lives on another origin, and a bearer header needs no
SameSite/CSRF handling. Tools marked requires_confirm use the same
preview -> confirm_token flow as the MCP path.
"""
import hmac
import os

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

try:
    import adapters
    import auth
    import confirm_tokens
    import connect_oauth
    import connected_accounts as accts
    import connections
    import crm_contacts
    import crm_ops
    import oauth_refresh
    import support_ops
    from adapters.freshdesk import validate_freshdesk_domain
    from oauth_server import BASE_URL
except ImportError:
    from src import adapters
    from src import auth
    from src import confirm_tokens
    from src import connect_oauth
    from src import connected_accounts as accts
    from src import connections
    from src import crm_contacts
    from src import crm_ops
    from src import oauth_refresh
    from src import support_ops
    from src.adapters.freshdesk import validate_freshdesk_domain
    from src.oauth_server import BASE_URL

router = APIRouter(prefix="/api")
_PROVIDER = "support_ops"


def _user(authorization: str | None) -> auth.HubUser:
    token = authorization[7:] if authorization and authorization.startswith("Bearer ") else None
    user = auth.resolve_session(token)
    if not user:
        raise HTTPException(401, "not authenticated")
    return user


class Login(BaseModel):
    email: str
    password: str


@router.post("/session/login")
def login(body: Login):
    try:
        user = auth.verify_password(body.email, body.password)
    except auth.AuthError as exc:
        raise HTTPException(401, str(exc))
    return {"token": auth.create_session(user.user_id), "email": user.email}


@router.post("/session/signup")
def signup(body: Login):
    try:
        user = auth.signup(body.email, body.password)
    except auth.AuthError as exc:
        raise HTTPException(400, str(exc))
    return {"token": auth.create_session(user.user_id), "email": user.email}


@router.post("/session/logout")
def logout(authorization: str | None = Header(None)):
    _user(authorization)
    auth.destroy_session(authorization[7:])
    return {"ok": True}


@router.get("/ops/status")
def status(authorization: str | None = Header(None)):
    user = _user(authorization)
    conns = {c["provider"]: c["status"] for c in connections.list_connections_for_user(user.user_id)}
    tools = [
        {"name": s.name, "description": s.description, "requires_confirm": s.requires_confirm, "schema": s.input_schema}
        for s in adapters.ADAPTERS[_PROVIDER].tools()
    ]
    return {"email": user.email, "connections": conns, "tools": tools,
            "digest_project_gid": support_ops.get_digest_project(user.user_id)}


class ToolCall(BaseModel):
    args: dict = {}
    confirm_token: str | None = None


def _confirm_gate(spec: adapters.ToolSpec, provider: str, name: str, body: "ToolCall", user_id: str) -> dict | None:
    """None if the call may proceed; otherwise the {preview, confirm_token}
    response to return as-is."""
    if not spec.requires_confirm or (
        body.confirm_token and confirm_tokens.consume(body.confirm_token, user_id, provider, name, body.args)
    ):
        return None
    return {"preview": body.args, "confirm_token": confirm_tokens.issue(user_id, provider, name, body.args)}


@router.post("/ops/tools/{name}")
def call_tool(name: str, body: ToolCall, authorization: str | None = Header(None)):
    user = _user(authorization)
    spec = next((s for s in adapters.ADAPTERS[_PROVIDER].tools() if s.name == name), None)
    if not spec:
        raise HTTPException(404, f"unknown tool: {name}")
    gated = _confirm_gate(spec, _PROVIDER, name, body, user.user_id)
    if gated is not None:
        return gated
    conn = adapters.Connection(user_id=user.user_id, provider=_PROVIDER, auth_type="none", credentials={})
    try:
        return adapters.ADAPTERS[_PROVIDER].call(name, body.args, conn)
    except TypeError as exc:  # missing/unknown argument
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(502, str(exc))


@router.post("/ops/crm-tools/{provider}/{name}")
def call_crm_tool(provider: str, name: str, body: ToolCall, authorization: str | None = Header(None)):
    """Same preview/confirm-token + proactive/reactive-refresh dispatch as
    mcp_http_server.py's _call_tool_for_current_caller, for the bearer-session
    Ops UI: call_tool above only reaches support_ops/crm_ops (no credentials
    of their own); this is the UI's way to call a connected CRM's own write
    tool (e.g. salesforce_update_contact), which needs a real, fresh token."""
    user = _user(authorization)
    adapter = adapters.ADAPTERS.get(provider)
    if not adapter:
        raise HTTPException(404, f"unknown provider: {provider}")
    spec = next((s for s in adapter.tools() if s.name == name), None)
    if not spec:
        raise HTTPException(404, f"unknown tool: {name}")

    gated = _confirm_gate(spec, provider, name, body, user.user_id)
    if gated is not None:
        return gated

    conn, err = crm_contacts.get_fresh_conn(user.user_id, provider)
    if not conn:
        raise HTTPException(409, err or f"{provider} is not connected")

    try:
        result = adapter.call(name, body.args, conn)
    except Exception as exc:
        if isinstance(exc, TypeError) or not oauth_refresh.looks_like_auth_error(exc):
            raise HTTPException(400 if isinstance(exc, TypeError) else 502, str(exc))
        try:
            conn = oauth_refresh.refresh_and_persist(adapter, conn, provider)
        except Exception:
            connections.mark_needs_reauth(user.user_id, provider)
            raise HTTPException(409, f"{provider} session expired and could not be refreshed — reconnect it from Connected accounts.")
        try:
            result = adapter.call(name, body.args, conn)
        except TypeError as retry_exc:
            raise HTTPException(400, str(retry_exc))
        except Exception as retry_exc:
            raise HTTPException(502, str(retry_exc))

    connections.touch_last_used(user.user_id, provider)
    return result


class DigestSettings(BaseModel):
    project_gid: str | None = None


@router.put("/ops/digest-settings")
def digest_settings(body: DigestSettings, authorization: str | None = Header(None)):
    return support_ops.set_digest_project(_user(authorization).user_id, body.project_gid or None)


@router.get("/cron/digest")
def cron_digest(authorization: str | None = Header(None)):
    """Vercel cron target. Vercel sends `Authorization: Bearer $CRON_SECRET`."""
    secret = os.environ.get("CRON_SECRET")
    if not secret or not hmac.compare_digest(authorization or "", f"Bearer {secret}"):
        raise HTTPException(401, "bad cron secret")
    return {"users": {uid: support_ops.run_digest(uid) for uid in support_ops.digest_user_ids()}}


# ── Connected accounts (JSON twin of connected_accounts.py / provider_detail.py)

def _tools(provider: str):
    adapter = adapters.ADAPTERS.get(provider)
    return adapter.tools() if adapter else []


@router.get("/accounts")
def accounts(authorization: str | None = Header(None)):
    user = _user(authorization)
    conns = {c["provider"]: c for c in connections.list_connections_for_user(user.user_id)}
    rows = []
    for p in accts._ALL_PROVIDER_KEYS:
        row = {"provider": p, "name": accts.PROVIDER_DISPLAY_NAMES[p], "favicon": accts.favicon_url(p),
               "tool_count": len(_tools(p))}
        if p in conns:
            row.update(state="connected", status=conns[p]["status"], last_used_at=conns[p].get("last_used_at"))
        elif p == "freshdesk":
            row["state"] = "api_key"
        elif accts._oauth_app_configured(p):
            row["state"] = "available"
        else:
            cfg = connect_oauth.PROVIDER_OAUTH_CONFIG[p]
            row.update(state="not_configured", env=[cfg["client_id_env"], cfg["client_secret_env"]])
        rows.append(row)
    return {"providers": rows}


class FreshdeskKey(BaseModel):
    domain: str
    api_key: str


@router.post("/accounts/freshdesk")
def connect_freshdesk(body: FreshdeskKey, authorization: str | None = Header(None)):
    user = _user(authorization)
    domain = validate_freshdesk_domain(body.domain)
    if not domain:
        raise HTTPException(400, "That doesn't look like a valid Freshdesk domain (expected something.freshdesk.com).")
    creds = {"domain": domain, "api_key": body.api_key}
    try:
        accts._freshdesk_adapter.verify(adapters.Connection(
            user_id=user.user_id, provider="freshdesk", auth_type="api_key", credentials=creds))
    except Exception:
        raise HTTPException(400, "That domain and API key combination was rejected by Freshdesk.")
    connections.create_connection(user_id=user.user_id, provider="freshdesk", auth_type="api_key", credentials=creds)
    return {"ok": True}


@router.post("/accounts/{provider}/connect")
def connect_start(provider: str, authorization: str | None = Header(None)):
    """Returns the provider's authorize URL; the UI navigates the browser to it."""
    user = _user(authorization)
    config = connect_oauth.PROVIDER_OAUTH_CONFIG.get(provider)
    if not config:
        raise HTTPException(404, f"'{provider}' has no OAuth connect flow")
    client_id = os.environ.get(config["client_id_env"])
    if not client_id:
        raise HTTPException(503, f"{provider} OAuth app is not registered ({config['client_id_env']} is unset)")
    return {"url": connect_oauth.authorize_url(provider, config, client_id, user.user_id, ui=True)}


@router.delete("/accounts/{provider}")
def disconnect(provider: str, authorization: str | None = Header(None)):
    connections.disconnect(_user(authorization).user_id, provider)
    return {"ok": True}


@router.get("/accounts/{provider}")
def account_detail(provider: str, authorization: str | None = Header(None)):
    user = _user(authorization)
    conn = connections.get_connection(user.user_id, provider) if provider in adapters.ADAPTERS else None
    if provider not in accts.PROVIDER_DISPLAY_NAMES or not conn:  # tools are shown only for connected providers
        raise HTTPException(404, "not connected")
    return {
        "provider": provider, "name": accts.PROVIDER_DISPLAY_NAMES[provider], "favicon": accts.favicon_url(provider),
        "last_used_at": conn.get("last_used_at"), "server_url": BASE_URL, "instructions": adapters.MCP_INSTRUCTIONS,
        "tools": [{"name": t.name, "description": t.description, "requires_confirm": t.requires_confirm, "schema": t.input_schema}
                  for t in _tools(provider)],
    }


@router.get("/crm-ops/duplicates")
def crm_ops_duplicates(authorization: str | None = Header(None)):
    return crm_ops.find_duplicates(_user(authorization).user_id)


@router.get("/crm-ops/quality")
def crm_ops_quality(authorization: str | None = Header(None)):
    return crm_ops.data_quality_audit(_user(authorization).user_id)


@router.get("/crm-ops/360")
def crm_ops_360(email: str, authorization: str | None = Header(None)):
    return crm_ops.customer_360(_user(authorization).user_id, email)


@router.get("/crm-ops/search")
def crm_ops_search(q: str, authorization: str | None = Header(None)):
    return crm_ops.customer_search(_user(authorization).user_id, q)


@router.get("/crm-ops/duplicates-fuzzy")
def crm_ops_duplicates_fuzzy(authorization: str | None = Header(None)):
    return crm_ops.find_duplicates_fuzzy(_user(authorization).user_id)


@router.get("/crm-ops/conflicts")
def crm_ops_conflicts(authorization: str | None = Header(None)):
    return crm_ops.field_conflicts_with_suggestion(_user(authorization).user_id)


@router.get("/crm-ops/stale")
def crm_ops_stale(days: int = 90, authorization: str | None = Header(None)):
    return crm_ops.stale_contact_finder(_user(authorization).user_id, days)
