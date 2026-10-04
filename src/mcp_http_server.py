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
do the real work (PKCE, Freshdesk credential verification, Mongo-backed
grants). This file wires the MCP SDK's TokenVerifier protocol to look up
those same grants, and exposes list_tickets/get_ticket/search_tickets as
real MCP tools via FastMCP.
"""
import os
import time

from fastapi import FastAPI
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

try:
    # Flat import — works when run as `uvicorn mcp_http_server:app --app-dir src` (local dev).
    import freshdesk_client as fd
    from oauth_server import BASE_URL, _grants, _sha256, app as oauth_app
except ImportError:
    # Package-relative import — works when imported as `src.mcp_http_server:app` (Vercel).
    from src import freshdesk_client as fd
    from src.oauth_server import BASE_URL, _grants, _sha256, app as oauth_app


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
    name="Freshdesk Connector",
    instructions="Read-only connector for Freshdesk support tickets. List recent tickets, "
    "fetch a single ticket's full details by id, or search tickets by status/priority/type "
    "or by a free-text subject/description match. Cannot create, update, or delete tickets, "
    "and cannot access anything outside the connected Freshdesk account's tickets.",
    website_url="https://www.freshworks.com/freshdesk/",
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
        allowed_hosts=[os.environ.get("OAUTH_BASE_URL", BASE_URL).split("://")[-1]],
        allowed_origins=[os.environ.get("OAUTH_BASE_URL", BASE_URL)],
    ),
)


def _current_access_token() -> str:
    """Pulled from the MCP request context the SDK sets up per call, after
    TokenVerifier has already approved it — so a missing/expired token never
    reaches here, the SDK's auth middleware rejects it first."""
    from mcp.server.fastmcp.server import Context
    ctx = mcp.get_context()
    # FastMCP stores the verified AccessToken on the request state during the
    # auth middleware; retrieve the raw bearer token from there.
    return ctx.request_context.request.headers.get("authorization", "").removeprefix("Bearer ")


@mcp.tool()
def list_tickets(status: str | None = None, updated_since: str | None = None, page: int = 1) -> list[dict]:
    """List tickets for the connected Freshdesk account, newest first, paginated.
    Optionally filter by status or by an updated_since timestamp (ISO 8601)."""
    token = _current_access_token()
    grant = _grant_for_token(token)
    return fd.list_tickets_for(grant["freshdesk_domain"], grant["freshdesk_api_key"], status, updated_since, page)


@mcp.tool()
def get_ticket(ticket_id: int, include: str | None = None) -> dict:
    """Fetch the full details of a single Freshdesk ticket by its numeric id.
    Optionally include related data such as conversations or the requester's info."""
    token = _current_access_token()
    grant = _grant_for_token(token)
    return fd.get_ticket_for(grant["freshdesk_domain"], grant["freshdesk_api_key"], ticket_id, include)


@mcp.tool()
def search_tickets(query: str) -> list[dict]:
    """Search Freshdesk tickets matching a query string, e.g. 'status:2 AND priority:3'."""
    token = _current_access_token()
    grant = _grant_for_token(token)
    return fd.search_tickets_for(grant["freshdesk_domain"], grant["freshdesk_api_key"], query)


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

from contextlib import asynccontextmanager


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    async with mcp.session_manager.run():
        yield


app: FastAPI = oauth_app
app.router.lifespan_context = _lifespan
app.mount("/", _mcp_asgi_app)
