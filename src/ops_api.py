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
    import crm_ops
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
    from src import crm_ops
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


@router.post("/ops/tools/{name}")
def call_tool(name: str, body: ToolCall, authorization: str | None = Header(None)):
    user = _user(authorization)
    spec = next((s for s in adapters.ADAPTERS[_PROVIDER].tools() if s.name == name), None)
    if not spec:
        raise HTTPException(404, f"unknown tool: {name}")
    if spec.requires_confirm and not (
        body.confirm_token and confirm_tokens.consume(body.confirm_token, user.user_id, _PROVIDER, name, body.args)
    ):
        return {"preview": body.args, "confirm_token": confirm_tokens.issue(user.user_id, _PROVIDER, name, body.args)}
    conn = adapters.Connection(user_id=user.user_id, provider=_PROVIDER, auth_type="none", credentials={})
    try:
        return adapters.ADAPTERS[_PROVIDER].call(name, body.args, conn)
    except TypeError as exc:  # missing/unknown argument
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(502, str(exc))


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
