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
    import connections
    import support_ops
except ImportError:
    from src import adapters
    from src import auth
    from src import confirm_tokens
    from src import connections
    from src import support_ops

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
