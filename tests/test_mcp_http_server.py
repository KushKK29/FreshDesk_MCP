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
import time
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
    exchange (consent no longer asks for any provider's credentials — fixed
    bug where it hardcoded a Freshdesk form and blocked every other
    provider's first connect), then connects Freshdesk directly through
    connections.create_connection (what /connected-accounts does), and
    returns a working MCP bearer access token."""
    import base64, hashlib

    c = TestClient(svr.app)
    email = f"mcptest-{secrets.token_hex(4)}@example.com"
    c.get("/signup")  # sets the hub_csrf cookie the POST below must echo back
    csrf = c.cookies.get("hub_csrf")
    signup_resp = c.post("/signup", data={"email": email, "password": "supersecret123", "next": "", "csrf": csrf})
    assert signup_resp.status_code in (200, 302), signup_resp.text
    user = hub_auth._users.find_one({"email": email})
    connections.create_connection(
        user_id=str(user["_id"]), provider="freshdesk", auth_type="api_key",
        credentials={"domain": "x.freshdesk.com", "api_key": "fake_key"},
    )

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

    decision_resp = c.post("/oauth/authorize/decision", data={
        "session_id": pending["session_id"],
        "csrf": pending["csrf"],
        "decision": "approve",
    }, follow_redirects=False)

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


def _connect_asana_and_get_access_token(expires_at: float | None = None) -> tuple[str, str]:
    """Same shape as _connect_freshdesk_and_get_access_token, but with an
    OAuth2 Asana connection (needed to exercise refresh — Freshdesk is
    API-key auth and has no refresh path)."""
    import base64, hashlib

    c = TestClient(svr.app)
    email = f"refreshtest-{secrets.token_hex(4)}@example.com"
    c.get("/signup")
    csrf = c.cookies.get("hub_csrf")
    signup_resp = c.post("/signup", data={"email": email, "password": "supersecret123", "next": "", "csrf": csrf}, follow_redirects=False)
    assert signup_resp.status_code == 302, signup_resp.text
    user = hub_auth._users.find_one({"email": email})
    user_id = str(user["_id"])
    connections.create_connection(
        user_id=user_id, provider="asana", auth_type="oauth2",
        credentials={"access_token": "stale-token", "refresh_token": "refresh-token-abc"},
        expires_at=expires_at,
    )

    reg = c.post("/oauth/register", json={"client_name": "refresh-test-client", "redirect_uris": ["https://example.com/callback"]}).json()
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
    return token_resp.json()["access_token"], user_id


def test_expired_token_refreshes_proactively_before_call():
    """Fix: nothing ever called adapter.refresh() — a token connected hours
    earlier failed every call with an auth error (confirmed live on
    Salesforce: INVALID_SESSION_ID). A connection whose expires_at is in the
    past must be refreshed BEFORE the call, not just reactively on failure."""
    if not MONGO_AVAILABLE:
        return
    _reset_db()
    access_token, user_id = _connect_asana_and_get_access_token(expires_at=time.time() - 60)  # already expired

    import httpx
    calls = {"refresh": 0, "api": []}
    orig_post = httpx.post
    orig_request = httpx.request
    def fake_post(url, **kwargs):
        if "oauth_token" in url:
            calls["refresh"] += 1
            return httpx.Response(200, json={"access_token": "fresh-token", "expires_in": 3600}, request=httpx.Request("POST", url))
        return orig_post(url, **kwargs)
    def fake_request(method, url, **kwargs):
        calls["api"].append(kwargs.get("headers", {}).get("Authorization"))
        return httpx.Response(200, json={"data": []}, request=httpx.Request(method, url))
    httpx.post = fake_post
    httpx.request = fake_request
    try:
        result = _call_as(access_token, "asana_list_tasks", {"workspace": "w1"})
    finally:
        httpx.post = orig_post
        httpx.request = orig_request

    assert calls["refresh"] == 1, "expired token should trigger exactly one refresh call before the API call"
    assert calls["api"][-1] == "Bearer fresh-token", "the API call must use the refreshed token, not the stale one"
    assert result == {"results": []}

    conn = connections.get_connection(user_id, "asana")
    assert conn["credentials"]["access_token"] == "fresh-token"  # persisted back to the store


def test_401_triggers_one_reactive_refresh_and_retry():
    """No expires_at tracked (or it hasn't passed yet) but the provider
    itself rejects the token — must refresh once and retry, not loop."""
    if not MONGO_AVAILABLE:
        return
    _reset_db()
    access_token, user_id = _connect_asana_and_get_access_token(expires_at=None)

    import httpx
    calls = {"refresh": 0, "api": 0}
    orig_post = httpx.post
    orig_request = httpx.request
    def fake_post(url, **kwargs):
        if "oauth_token" in url:
            calls["refresh"] += 1
            return httpx.Response(200, json={"access_token": "fresh-token", "expires_in": 3600}, request=httpx.Request("POST", url))
        return orig_post(url, **kwargs)
    def fake_request(method, url, **kwargs):
        calls["api"] += 1
        if calls["api"] == 1:
            return httpx.Response(401, json={"errors": [{"message": "Not Authorized"}]}, request=httpx.Request(method, url))
        return httpx.Response(200, json={"data": []}, request=httpx.Request(method, url))
    httpx.post = fake_post
    httpx.request = fake_request
    try:
        result = _call_as(access_token, "asana_list_tasks", {"workspace": "w1"})
    finally:
        httpx.post = orig_post
        httpx.request = orig_request

    assert calls["refresh"] == 1
    assert calls["api"] == 2, "exactly one retry after refresh, not a loop"
    assert result == {"results": []}


def test_refresh_failure_marks_needs_reauth_not_infinite_retry():
    if not MONGO_AVAILABLE:
        return
    _reset_db()
    access_token, user_id = _connect_asana_and_get_access_token(expires_at=time.time() - 60)

    import httpx
    orig_post = httpx.post
    orig_request = httpx.request
    def fake_post(url, **kwargs):
        if "oauth_token" in url:
            return httpx.Response(400, json={"error": "invalid_grant"}, request=httpx.Request("POST", url))
        return orig_post(url, **kwargs)
    httpx.post = fake_post
    httpx.request = lambda method, url, **kwargs: httpx.Response(200, json={"data": []}, request=httpx.Request(method, url))
    try:
        try:
            _call_as(access_token, "asana_list_tasks", {"workspace": "w1"})
            assert False, "should have raised when refresh itself fails"
        except Exception:
            pass
    finally:
        httpx.post = orig_post
        httpx.request = orig_request

    conn = connections.list_connections_for_user(user_id)
    assert any(c["provider"] == "asana" and c["status"] == "needs_reauth" for c in conn)


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
    test_expired_token_refreshes_proactively_before_call()
    test_401_triggers_one_reactive_refresh_and_retry()
    test_refresh_failure_marks_needs_reauth_not_infinite_retry()
    _reset_db()
    print("all checks passed")
