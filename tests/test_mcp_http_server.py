"""Integration test for mcp_http_server.py's per-caller dynamic tool list
(connector-hub plan Task 6): connects Freshdesk through the real OAuth flow,
then calls MCP tools/list and tools/call over raw JSON-RPC and checks the
Freshdesk tools appear and work — proving the adapter-registry rewiring
didn't change externally-visible behavior (M0b exit criteria).

Same shape as test_oauth_server.py: real local MongoDB, isolated test db,
skips cleanly if mongod isn't running.
"""
import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

os.environ.setdefault("FRESHDESK_DOMAIN", "test.freshdesk.com")
os.environ.setdefault("FRESHDESK_API_KEY", "fake_key")
os.environ["MONGO_URI"] = "mongodb://localhost:27017"
os.environ["OAUTH_BASE_URL"] = "http://localhost:8004"
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
    import confirm_tokens
    import mcp_http_server as mhs

    svr._db = _dbmod._mongo["test_freshdesk_connector_mcp"]
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
    confirm_tokens._confirm_tokens = svr._db["confirm_tokens"]
    confirm_tokens._confirm_tokens.create_index("expires_at", expireAfterSeconds=0)
    mhs.confirm_tokens = confirm_tokens
    # mhs imported oauth_server's _grants before the redirect above happened
    # at module-import time elsewhere in the suite — re-point it explicitly
    # so this test file is self-contained regardless of import order.
    mhs._grants = svr._grants

    from fastapi.testclient import TestClient
    client = TestClient(svr.app)


def _reset_db():
    svr._clients.delete_many({})
    svr._grants.delete_many({})
    svr._pending_authorizations.delete_many({})
    hub_auth._users.delete_many({})
    hub_auth._login_sessions.delete_many({})
    connections._connections.delete_many({})
    confirm_tokens._confirm_tokens.delete_many({})


def _connect_freshdesk_and_get_access_token() -> str:
    """Runs the real signup -> register -> authorize -> consent -> token
    exchange, with Freshdesk's verify() call mocked (no real Freshdesk
    account needed), and returns a working MCP bearer access token."""
    import httpx
    import base64, hashlib

    c = TestClient(svr.app)
    email = f"mcptest-{secrets.token_hex(4)}@example.com"
    c.get("/signup")  # sets the hub_csrf cookie the POST below must echo back
    csrf = c.cookies.get("hub_csrf")
    signup_resp = c.post("/signup", data={"email": email, "password": "supersecret123", "next": "", "csrf": csrf})
    assert signup_resp.status_code in (200, 302), signup_resp.text

    reg = c.post(
        "/oauth/register",
        json={"client_name": "mcp-test-client", "redirect_uris": ["https://example.com/callback"]},
    ).json()

    verifier = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()

    c.get("/oauth/authorize", params={
        "client_id": reg["client_id"],
        "redirect_uri": "https://example.com/callback",
        "response_type": "code",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    pending = svr._pending_authorizations.find_one({})

    class _FakeResp:
        status_code = 200
        headers = {}
        def json(self):
            return []
    orig_request = httpx.request
    httpx.request = lambda *a, **k: _FakeResp()
    try:
        decision_resp = c.post("/oauth/authorize/decision", data={
            "session_id": pending["session_id"],
            "csrf": pending["csrf"],
            "decision": "approve",
            "freshdesk_domain": "x.freshdesk.com",
            "freshdesk_api_key": "fake_key",
        }, follow_redirects=False)
    finally:
        httpx.request = orig_request

    redirect_location = decision_resp.headers["location"]
    code = redirect_location.split("code=")[1].split("&")[0]

    token_resp = c.post("/oauth/token", data={
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": verifier,
        "redirect_uri": "https://example.com/callback",
        "client_id": reg["client_id"],
    })
    return token_resp.json()["access_token"]


def test_tools_list_includes_freshdesk_tools_after_connect():
    if not MONGO_AVAILABLE:
        return
    _reset_db()
    access_token = _connect_freshdesk_and_get_access_token()

    grant = svr._grants.find_one({"access_token_hash": svr._sha256(access_token)})
    import asyncio

    class _FakeRequestCtx:
        class request:
            headers = {"authorization": f"Bearer {access_token}"}

    class _FakeContext:
        request_context = _FakeRequestCtx()

    orig_get_context = mhs.mcp.get_context
    mhs.mcp.get_context = lambda: _FakeContext()
    try:
        tools = asyncio.run(mhs._list_tools_for_current_caller())
    finally:
        mhs.mcp.get_context = orig_get_context

    names = {t.name for t in tools}
    assert "freshdesk_list_tickets" in names
    assert "freshdesk_get_ticket" in names
    assert "freshdesk_search_tickets" in names
    assert "freshdesk_create_ticket" in names
    assert "freshdesk_delete_ticket" in names
    delete_tool = next(t for t in tools if t.name == "freshdesk_delete_ticket")
    assert delete_tool.annotations.destructiveHint is True
    assert delete_tool.annotations.readOnlyHint is False


def test_tools_list_empty_for_unauthenticated_caller():
    if not MONGO_AVAILABLE:
        return

    class _FakeRequestCtx:
        class request:
            headers = {"authorization": "Bearer not-a-real-token"}

    class _FakeContext:
        request_context = _FakeRequestCtx()

    import asyncio
    orig_get_context = mhs.mcp.get_context
    mhs.mcp.get_context = lambda: _FakeContext()
    try:
        tools = asyncio.run(mhs._list_tools_for_current_caller())
    finally:
        mhs.mcp.get_context = orig_get_context
    assert tools == []


def test_call_tool_list_tickets_matches_rest_endpoint_behavior():
    """The MCP tool call and the plain REST /tickets endpoint (oauth_server.py)
    must return the same data for the same connection — proves the adapter
    dispatch didn't change behavior (M0b exit criteria)."""
    if not MONGO_AVAILABLE:
        return
    _reset_db()
    access_token = _connect_freshdesk_and_get_access_token()

    import httpx
    class _FakeTicketsResp:
        status_code = 200
        headers = {}
        def json(self):
            return [{"id": 1, "subject": "test ticket"}]
    orig_request = httpx.request
    httpx.request = lambda *a, **k: _FakeTicketsResp()
    try:
        rest_resp = client.get("/tickets", headers={"Authorization": f"Bearer {access_token}"})

        class _FakeRequestCtx:
            class request:
                headers = {"authorization": f"Bearer {access_token}"}
        class _FakeContext:
            request_context = _FakeRequestCtx()
        import asyncio
        orig_get_context = mhs.mcp.get_context
        mhs.mcp.get_context = lambda: _FakeContext()
        try:
            mcp_result = asyncio.run(mhs._call_tool_for_current_caller("freshdesk_list_tickets", {}))
        finally:
            mhs.mcp.get_context = orig_get_context
    finally:
        httpx.request = orig_request

    assert rest_resp.status_code == 200
    assert rest_resp.json() == [{"id": 1, "subject": "test ticket"}]
    assert mcp_result == {"results": [{"id": 1, "subject": "test ticket"}]}


def _call_as(access_token: str, tool: str, args: dict):
    import asyncio
    class _FakeRequestCtx:
        class request:
            headers = {"authorization": f"Bearer {access_token}"}
    class _FakeContext:
        request_context = _FakeRequestCtx()
    orig_get_context = mhs.mcp.get_context
    mhs.mcp.get_context = lambda: _FakeContext()
    try:
        return asyncio.run(mhs._call_tool_for_current_caller(tool, args))
    finally:
        mhs.mcp.get_context = orig_get_context


def test_requires_confirm_tool_previews_on_first_call_and_executes_on_second():
    """Security fix: fail-open requires_confirm gate. A destructive tool must
    never execute on its first call — it returns a preview + confirm_token,
    and only executes once that exact token is supplied back with the same
    arguments."""
    if not MONGO_AVAILABLE:
        return
    _reset_db()
    access_token = _connect_freshdesk_and_get_access_token()

    delete_called = {"n": 0}
    import httpx
    orig_request = httpx.request
    def fake_request(method, url, **kwargs):
        if method == "DELETE":
            delete_called["n"] += 1
        return httpx.Response(200, json={}, request=httpx.Request(method, url))
    httpx.request = fake_request
    try:
        first = _call_as(access_token, "freshdesk_delete_ticket", {"id": 1})
        assert "confirm_token" in first
        assert first["preview"] == {"id": 1}
        assert delete_called["n"] == 0, "a destructive tool must not execute on its first call"

        # A stale/invalid token must not work.
        rejected = _call_as(access_token, "freshdesk_delete_ticket", {"id": 1, "confirm_token": "not-the-real-token"})
        assert "confirm_token" in rejected  # still just a (new) preview, not executed
        assert delete_called["n"] == 0

        second = _call_as(access_token, "freshdesk_delete_ticket", {"id": 1, "confirm_token": first["confirm_token"]})
        assert second == {"status": "deleted"}
        assert delete_called["n"] == 1

        # The token is single-use — replaying it must not execute again.
        replay = _call_as(access_token, "freshdesk_delete_ticket", {"id": 1, "confirm_token": first["confirm_token"]})
        assert "confirm_token" in replay
        assert delete_called["n"] == 1
    finally:
        httpx.request = orig_request


def test_requires_confirm_token_bound_to_arguments():
    """A confirm_token issued for one set of arguments must not authorize a
    DIFFERENT set of arguments on the same tool — stops a token meant for
    'delete ticket 1' being reused to delete ticket 2."""
    if not MONGO_AVAILABLE:
        return
    _reset_db()
    access_token = _connect_freshdesk_and_get_access_token()

    import httpx
    orig_request = httpx.request
    httpx.request = lambda method, url, **kwargs: httpx.Response(200, json={}, request=httpx.Request(method, url))
    try:
        first = _call_as(access_token, "freshdesk_delete_ticket", {"id": 1})
        mismatched = _call_as(access_token, "freshdesk_delete_ticket", {"id": 2, "confirm_token": first["confirm_token"]})
        assert "confirm_token" in mismatched  # rejected as a new preview, not executed against ticket 2
    finally:
        httpx.request = orig_request


def test_non_gated_tool_executes_without_confirm_token():
    """Plain read/create/update tools must never require the confirm round
    trip — only tools whose ToolSpec sets requires_confirm."""
    if not MONGO_AVAILABLE:
        return
    _reset_db()
    access_token = _connect_freshdesk_and_get_access_token()

    import httpx
    orig_request = httpx.request
    httpx.request = lambda method, url, **kwargs: httpx.Response(200, json=[{"id": 1}], request=httpx.Request(method, url))
    try:
        result = _call_as(access_token, "freshdesk_list_tickets", {})
    finally:
        httpx.request = orig_request
    assert "confirm_token" not in result


if __name__ == "__main__":
    if not MONGO_AVAILABLE:
        print("SKIPPED: no local MongoDB reachable at mongodb://localhost:27017")
        sys.exit(0)
    test_tools_list_includes_freshdesk_tools_after_connect()
    test_tools_list_empty_for_unauthenticated_caller()
    test_call_tool_list_tickets_matches_rest_endpoint_behavior()
    test_requires_confirm_tool_previews_on_first_call_and_executes_on_second()
    test_requires_confirm_token_bound_to_arguments()
    test_non_gated_tool_executes_without_confirm_token()
    _reset_db()
    print("all checks passed")
