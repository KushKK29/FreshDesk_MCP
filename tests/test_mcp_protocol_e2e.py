"""End-to-end test through the REAL MCP wire protocol (not just calling
_list_tools_for_current_caller()/_call_tool_for_current_caller() as plain
functions, which is what the other mcp_http_server tests do).

This exists because of a real bug those direct-call tests couldn't catch:
FastMCP.__init__ registers its tools/list and tools/call handlers with the
low-level Server ONCE, at construction time, via
self._mcp_server.list_tools()(self.list_tools) — that decorator captures
the bound method reference right then. Reassigning mcp.list_tools =
some_function afterward (what mcp_http_server.py used to do) never reaches
the low-level Server's request_handlers dict, so every real tools/list call
from ChatGPT/Claude Code kept hitting the original (empty) handler — "OAuth
succeeded, connected, no tools" — even though direct calls to the helper
functions worked fine and every other test passed.

This test starts the real app with uvicorn on a real local port and drives
it with the actual `mcp` client SDK over streamable-http, exactly like a
real MCP client would, so a regression of that bug fails here even if every
other test stays green.
"""
import os
import secrets
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

os.environ.setdefault("FRESHDESK_DOMAIN", "test.freshdesk.com")
os.environ.setdefault("FRESHDESK_API_KEY", "fake_key")
os.environ["MONGO_URI"] = "mongodb://localhost:27017"
os.environ.setdefault("OAUTH_BASE_URL", "http://localhost:8099")
os.environ.setdefault("VAULT_MASTER_KEY", "QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE=")

try:
    from pymongo import MongoClient
    MongoClient(os.environ["MONGO_URI"], serverSelectionTimeoutMS=1000).admin.command("ping")
    MONGO_AVAILABLE = True
except Exception:
    MONGO_AVAILABLE = False

if MONGO_AVAILABLE:
    import db as _dbmod
    import oauth_server as svr
    import auth as hub_auth
    import connections
    import mcp_http_server as mhs

    # mhs.app's TransportSecuritySettings.allowed_hosts was already baked
    # in from svr.BASE_URL at mcp_http_server.py's import time — whichever
    # test file triggers that import first in the collection order "wins",
    # same class of bug as _rebind_globals below. os.environ["OAUTH_BASE_URL"]
    # set above only takes effect if this file is that first import, so the
    # real uvicorn server here must listen on whatever port actually got
    # baked in, not on a port this file assumed it controlled.
    TEST_PORT = int(svr.BASE_URL.rsplit(":", 1)[-1])

    def _rebind_globals():
        # See test_mcp_http_server.py's _rebind_globals for why this needs
        # to be a function called from _reset_db(), not top-level code:
        # pytest imports every test file during collection before running
        # any test body, and every Mongo-backed test file here repoints
        # these same shared module globals — whichever file imports last
        # wins at import time, silently pointing every earlier file at ITS
        # database (and, in this file's case, the real uvicorn server
        # below reads auth state through these same module handles too).
        svr._db = _dbmod._mongo["test_freshdesk_connector_protocol_e2e"]
        svr._clients = svr._db["oauth_clients"]
        svr._grants = svr._db["oauth_grants"]
        svr._pending_authorizations = svr._db["pending_authorizations"]
        hub_auth._db = svr._db
        hub_auth._users = hub_auth._db["users"]
        hub_auth._login_sessions = hub_auth._db["login_sessions"]
        hub_auth._users.create_index("email", unique=True)
        hub_auth._login_sessions.create_index("expires_at", expireAfterSeconds=0)
        connections._db = svr._db
        connections._connections = connections._db["connections"]
        connections._connections.create_index([("user_id", 1), ("provider", 1)], unique=True)
        mhs._grants = svr._grants

    _rebind_globals()

    from fastapi.testclient import TestClient


def _reset_db():
    _rebind_globals()
    svr._clients.delete_many({})
    svr._grants.delete_many({})
    svr._pending_authorizations.delete_many({})
    hub_auth._users.delete_many({})
    hub_auth._login_sessions.delete_many({})
    connections._connections.delete_many({})


def _get_access_token_with_freshdesk_connected() -> str:
    c = TestClient(svr.app)
    email = f"protoe2e-{secrets.token_hex(4)}@example.com"
    c.get("/signup")
    csrf = c.cookies.get("hub_csrf")
    signup_resp = c.post("/signup", data={"email": email, "password": "supersecret123", "next": "", "csrf": csrf}, follow_redirects=False)
    assert signup_resp.status_code == 302, signup_resp.text
    user = hub_auth._users.find_one({"email": email})
    connections.create_connection(
        user_id=str(user["_id"]), provider="freshdesk", auth_type="api_key",
        credentials={"domain": "x.freshdesk.com", "api_key": "fake_key"},
    )

    import base64, hashlib
    reg = c.post("/oauth/register", json={"client_name": "e2e-test-client", "redirect_uris": ["https://example.com/callback"]}).json()
    verifier = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    c.get("/oauth/authorize", params={
        "client_id": reg["client_id"], "redirect_uri": "https://example.com/callback",
        "response_type": "code", "code_challenge": challenge, "code_challenge_method": "S256",
    })
    pending = svr._pending_authorizations.find_one({})
    decision_resp = c.post("/oauth/authorize/decision", data={
        "session_id": pending["session_id"], "csrf": pending["csrf"], "decision": "approve",
    }, follow_redirects=False)
    code = decision_resp.headers["location"].split("code=")[1].split("&")[0]
    token_resp = c.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code, "code_verifier": verifier,
        "redirect_uri": "https://example.com/callback", "client_id": reg["client_id"],
    })
    return token_resp.json()["access_token"]


def _run_server_in_background() -> threading.Thread:
    import uvicorn
    config = uvicorn.Config(mhs.app, host="localhost", port=TEST_PORT, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(50):
        try:
            import httpx
            httpx.get(f"http://localhost:{TEST_PORT}/login", timeout=0.5)
            break
        except Exception:
            time.sleep(0.1)
    return thread


def test_real_mcp_client_sees_tools_after_connect():
    """The actual regression test: a real MCP client (the `mcp` SDK's own
    ClientSession, over real streamable-http) must see Freshdesk's tools
    after the user connected it — not just the direct-call helpers."""
    if not MONGO_AVAILABLE:
        return
    _reset_db()
    access_token = _get_access_token_with_freshdesk_connected()
    _run_server_in_background()

    import asyncio
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async def _list_tools_over_real_protocol():
        url = f"http://localhost:{TEST_PORT}/"
        headers = {"Authorization": f"Bearer {access_token}"}
        async with streamablehttp_client(url, headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                return result.tools

    tools = asyncio.run(_list_tools_over_real_protocol())
    names = {t.name for t in tools}
    assert "freshdesk_list_tickets" in names, f"expected freshdesk_list_tickets in real tools/list response, got: {sorted(names)}"
    assert len(tools) >= 30, f"expected Freshdesk's full tool set (~36), got only {len(tools)}"


if __name__ == "__main__":
    if not MONGO_AVAILABLE:
        print("SKIPPED: no local MongoDB reachable at mongodb://localhost:27017")
        sys.exit(0)
    test_real_mcp_client_sees_tools_after_connect()
    _reset_db()
    print("all checks passed")
