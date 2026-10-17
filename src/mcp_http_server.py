"""Real MCP server over HTTP (streamable-http transport), mounted alongside
our existing OAuth 2.1 authorization server.

Why this file exists, separate from oauth_server.py: ChatGPT's current
"Connect app" flow speaks the MCP protocol directly (JSON-RPC 2.0 at POST /,
discovery at /.well-known/oauth-protected-resource per RFC 9728) — NOT the
legacy ChatGPT-Plugins OpenAPI format. Confirmed from oauth_server.py's live
request log: ChatGPT hit POST /, /.well-known/oauth-protected-resource, and
/.well-known/openid-configuration — none of which the Plugins-format server
served, which is why tools showed up as empty even after OAuth succeeded.

oauth_server.py's register/authorize/token endpoints are unchanged and still
do the real work (PKCE, hub login, Mongo-backed grants). This file wires the
MCP SDK's TokenVerifier protocol to look up those same grants, and exposes
every registered provider adapter's tools dynamically — the tool list is no
longer 3 hardcoded Freshdesk functions (connector-hub plan Task 6): it's
built per-request from ADAPTERS, filtered to the calling hub user's active
connections (PRD F3), so adding a provider needs no change here.
"""
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import Tool as MCPTool

try:
    import adapters
    import connections
    import confirm_tokens
    import crm_contacts
    import crm_ops  # noqa: F401 — import-time side effect registers CrmOpsAdapter into adapters.ADAPTERS
    import oauth_refresh
    from oauth_server import BASE_URL, _grants, _sha256, app as oauth_app
except ImportError:
    from src import adapters
    from src import connections
    from src import confirm_tokens
    from src import crm_contacts
    from src import crm_ops  # noqa: F401
    from src import oauth_refresh
    from src.oauth_server import BASE_URL, _grants, _sha256, app as oauth_app

adapters.register_all_adapters()


class MongoGrantTokenVerifier(TokenVerifier):
    """Validates a bearer token against the same oauth_grants collection
    oauth_server.py's /oauth/token issues into — one source of truth for
    "is this token valid," whether a tool call comes in via MCP or (if ever
    re-added) a plain REST call."""

    async def verify_token(self, token: str) -> AccessToken | None:
        grant = _grants.find_one({
            "access_token_hash": _sha256(token),
            "access_expires_at": {"$gte": time.time()},
            "revoked_at": None,
        })
        if not grant:
            return None
        return AccessToken(
            token=token,
            client_id=grant["client_id"],
            scopes=["tickets:read"],
            expires_at=int(grant["access_expires_at"]),
        )


def _grant_for_token(token: str) -> dict | None:
    return _grants.find_one({
        "access_token_hash": _sha256(token),
        "access_expires_at": {"$gte": time.time()},
        "revoked_at": None,
    })


mcp = FastMCP(
    name="Connector Hub",
    instructions=adapters.MCP_INSTRUCTIONS,
    website_url=BASE_URL,
    token_verifier=MongoGrantTokenVerifier(),
    auth=AuthSettings(
        issuer_url=BASE_URL,
        resource_server_url=BASE_URL,
        required_scopes=["tickets:read"],
    ),
    stateless_http=True,  # each tool call looks up its own grant fresh; no server-side MCP session state needed
    streamable_http_path="/",  # ChatGPT's connector POSTs JSON-RPC to the server root, not /mcp
    transport_security=TransportSecuritySettings(
        # DNS-rebinding protection defaults to an EMPTY allowed_hosts list, which rejects every
        # Host header including our own public one — "Invalid Host header", 421 Misdirected
        # Request, observed live against ChatGPT. Must explicitly allow the deployed hostname.
        allowed_hosts=[BASE_URL.split("://")[-1]],
        allowed_origins=[BASE_URL],
    ),
)


def _current_access_token() -> str:
    """Pulled from the MCP request context the SDK sets up per call, after
    TokenVerifier has already approved it — so a missing/expired token never
    reaches here, the SDK's auth middleware rejects it first."""
    ctx = mcp.get_context()
    return ctx.request_context.request.headers.get("authorization", "").removeprefix("Bearer ")


def _active_adapter_tools_for_user(user_id: str) -> list[tuple[adapters.ToolSpec, str]]:
    """(ToolSpec, provider_key) pairs for every tool the user's active,
    enabled connections expose — PRD F3: tools/list reflects only active,
    enabled connections, not every registered provider."""
    out = []
    active_rows = [r for r in connections.list_connections_for_user(user_id) if r["status"] == "active"]
    for conn_row in active_rows:
        provider = conn_row["provider"]
        adapter = adapters.ADAPTERS.get(provider)
        if not adapter:
            continue
        enabled = conn_row.get("enabled_tools")  # None = all enabled (PRD F7)
        for spec in adapter.tools():
            if enabled is not None and spec.name not in enabled:
                continue
            out.append((spec, provider))

    # crm_ops has no OAuth connection of its own (it only reads connections
    # the user already made to real CRM providers) — exposed whenever at
    # least one of those is active, same PRD F3 gate real providers go
    # through above (not just "a row for it exists somewhere").
    if any(r["provider"] in crm_contacts.CRM_PROVIDERS for r in active_rows):
        crm_ops_adapter = adapters.ADAPTERS.get("crm_ops")
        if crm_ops_adapter:
            out.extend((spec, "crm_ops") for spec in crm_ops_adapter.tools())
    return out


def _tool_spec_to_mcp_tool(spec: adapters.ToolSpec) -> MCPTool:
    input_schema = spec.input_schema
    if spec.requires_confirm:
        # Added here, not on every adapter's own ToolSpec — the confirm_token
        # field is a cross-cutting dispatch-layer concern (confirm_tokens.py),
        # not something each adapter module should know the shape of.
        input_schema = dict(input_schema)
        input_schema["properties"] = {
            **input_schema.get("properties", {}),
            "confirm_token": {"type": "string", "description": "From a prior preview call to this same tool — required to execute, not to preview."},
        }
    return MCPTool(
        name=spec.name,
        description=spec.description + " Destructive/send/bulk action: first call previews and returns a confirm_token; call again with that token to execute." if spec.requires_confirm else spec.description,
        inputSchema=input_schema,
        annotations={
            "readOnlyHint": spec.read_only_hint,
            "destructiveHint": spec.destructive_hint,
            "idempotentHint": spec.idempotent_hint,
            "openWorldHint": spec.open_world_hint,
        },
    )


async def _list_tools_for_current_caller() -> list[MCPTool]:
    """Replaces FastMCP's static list_tools(): the SDK's method takes no
    request context, so tool visibility can't vary per caller through the
    normal @mcp.tool() path. Monkey-patched onto the instance below — PRD F3
    is a hard requirement (hide tools for providers the caller hasn't
    connected), not optional, so this is intentional, not a workaround for
    convenience."""
    token = _current_access_token()
    grant = _grant_for_token(token)
    if not grant:
        return []
    return [_tool_spec_to_mcp_tool(spec) for spec, _provider in _active_adapter_tools_for_user(grant["user_id"])]


async def _call_tool_for_current_caller(name: str, arguments: dict) -> dict:
    token = _current_access_token()
    grant = _grant_for_token(token)
    if not grant:
        raise ToolError("not authenticated")

    # confirm_token travels alongside the tool's real arguments but isn't
    # one of them — stripped before it reaches the adapter, and excluded
    # from the args hash the token is bound to (same args in, same hash,
    # regardless of whether a confirm_token happens to be present).
    call_args = {k: v for k, v in arguments.items() if k != "confirm_token"}
    supplied_token = arguments.get("confirm_token")

    for spec, provider in _active_adapter_tools_for_user(grant["user_id"]):
        if spec.name != name:
            continue

        if spec.requires_confirm:
            # Fail-closed gate: a destructive/send/bulk tool never executes
            # on its first call, no matter what arguments arrive. Without a
            # valid, matching confirm_token it returns a preview only.
            if not supplied_token or not confirm_tokens.consume(supplied_token, grant["user_id"], provider, name, call_args):
                new_token = confirm_tokens.issue(grant["user_id"], provider, name, call_args)
                return {"preview": call_args, "confirm_token": new_token, "message": f"This action ({name}) requires confirmation. Call it again with this confirm_token to execute."}

        if provider == "crm_ops":
            # No credentials of its own — crm_ops.call() reads conn.user_id
            # only, to re-fetch the other providers' already-stored connections.
            result = adapters.ADAPTERS["crm_ops"].call(name, call_args, adapters.Connection(user_id=grant["user_id"], provider="crm_ops", auth_type="none", credentials={}))
            return result

        conn_row = connections.get_connection(grant["user_id"], provider)
        if not conn_row:
            raise ToolError(f"{provider} is not connected or needs reauthorization")
        conn = adapters.Connection(
            user_id=grant["user_id"], provider=provider, auth_type=conn_row["auth_type"],
            credentials=conn_row["credentials"], scopes=conn_row.get("scopes", []), expires_at=conn_row.get("expires_at"),
        )
        adapter = adapters.ADAPTERS[provider]

        # Proactive refresh: OAuth access tokens are short-lived (Salesforce's
        # especially) and nothing else in this codebase ever refreshed them —
        # confirmed live: a token connected hours earlier failed every call
        # with INVALID_SESSION_ID. Refresh ahead of expiry when we know it,
        # and reactively below on an actual 401/expired-session error for
        # providers whose expiry we don't track.
        if conn.auth_type == "oauth2" and conn.expires_at and time.time() > conn.expires_at - 120:
            try:
                conn = oauth_refresh.refresh_and_persist(adapter, conn, provider)
            except Exception as exc:
                connections.mark_needs_reauth(grant["user_id"], provider)
                raise ToolError(f"{provider} session expired and could not be refreshed — reconnect it from Connected accounts.") from exc

        try:
            result = adapter.call(name, call_args, conn)
        except Exception as exc:
            if conn.auth_type != "oauth2" or not oauth_refresh.looks_like_auth_error(exc):
                raise
            # Reactive refresh: one retry, not a loop (same ceiling as the
            # rest of this codebase's HTTP clients) — if the refreshed token
            # still fails, the connection is marked needs_reauth and the
            # error surfaces to the caller instead of retrying forever.
            try:
                conn = oauth_refresh.refresh_and_persist(adapter, conn, provider)
            except Exception:
                connections.mark_needs_reauth(grant["user_id"], provider)
                raise ToolError(f"{provider} session expired and could not be refreshed — reconnect it from Connected accounts.") from exc
            result = adapter.call(name, call_args, conn)

        connections.touch_last_used(grant["user_id"], provider)
        return result

    raise ToolError(f"unknown or disabled tool: {name}")


# Reassigning mcp.list_tools/mcp.call_tool as plain attributes does NOT
# work: FastMCP.__init__ calls self._setup_handlers() once, which does
# self._mcp_server.list_tools()(self.list_tools) — that decorator captures
# the bound method reference at that moment and stores it in the low-level
# Server's request_handlers dict. Reassigning mcp.list_tools afterward never
# touches that dict, so every real tools/list call kept hitting the original
# (empty, no @mcp.tool()-registered functions) handler — confirmed live:
# ChatGPT and Claude Code both showed "connected, no tools" despite OAuth
# succeeding. Re-registering directly with the low-level server's own
# decorators is the only way to actually replace the handler.
mcp._mcp_server.list_tools()(_list_tools_for_current_caller)
mcp._mcp_server.call_tool(validate_input=False)(_call_tool_for_current_caller)


# Mount the MCP streamable-HTTP app at / on top of the existing OAuth routes.
# oauth_server.py's /oauth/* endpoints keep working unchanged; MCP's own
# protected-resource metadata and the JSON-RPC endpoint are added by mcp.streamable_http_app().
#
# The mounted sub-app's own `lifespan=lambda app: self.session_manager.run()`
# does NOT run automatically just because it's mounted — Starlette only
# invokes a sub-app's lifespan if the parent app's lifespan explicitly calls
# into it. Without this, every /  (JSON-RPC) request fails with
# "RuntimeError: Task group is not initialized. Make sure to use run()."
_mcp_asgi_app = mcp.streamable_http_app()


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    async with mcp.session_manager.run():
        yield


app: FastAPI = oauth_app
app.router.lifespan_context = _lifespan
app.mount("/", _mcp_asgi_app)
