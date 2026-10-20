"""Integration test for connected_accounts.py (M0b Task 7): connect Freshdesk
via the API-key form, confirm it's listed with status "active", disconnect,
confirm the connection and its encrypted credentials are gone.

Same shape as the other integration tests: real local MongoDB, isolated test
db, skips cleanly if mongod isn't running.
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

    def _rebind_globals():
        # See test_mcp_http_server.py's _rebind_globals for why this is a
        # function called from _reset_db(), not top-level code: pytest
        # imports every test file during collection before running any
        # test body, and every Mongo-backed test file here repoints these
        # same shared module globals — whichever file imports last wins at
        # import time, silently pointing every earlier file at ITS database.
        svr._db = _dbmod._mongo["test_freshdesk_connector_connected_accounts"]
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


def _signed_up_client():
    c = TestClient(svr.app)
    c.get("/signup")
    csrf = c.cookies.get("hub_csrf")
    email = f"connacct-{secrets.token_hex(4)}@example.com"
    resp = c.post("/signup", data={"email": email, "password": "supersecret123", "next": "", "csrf": csrf}, follow_redirects=False)
    assert resp.status_code == 302, resp.text
    user = hub_auth._users.find_one({"email": email})
    return c, str(user["_id"])


def test_connect_list_disconnect_round_trip():
    if not MONGO_AVAILABLE:
        return
    _reset_db()
    c, user_id = _signed_up_client()
    c.get("/connected-accounts")  # sets the hub_csrf cookie the POSTs below must echo back
    csrf = c.cookies.get("hub_csrf")

    import httpx
    class _FakeResp:
        status_code = 200
        headers = {}
        def json(self):
            return []
    orig_request = httpx.request
    httpx.request = lambda *a, **k: _FakeResp()
    try:
        connect_resp = c.post("/connected-accounts/connect-freshdesk", data={
            "freshdesk_domain": "x.freshdesk.com", "freshdesk_api_key": "my-secret-key", "csrf": csrf,
        }, follow_redirects=False)
    finally:
        httpx.request = orig_request
    assert connect_resp.status_code == 302, connect_resp.text

    rows = connections.list_connections_for_user(user_id)
    assert len(rows) == 1
    assert rows[0]["provider"] == "freshdesk"
    assert rows[0]["status"] == "active"

    page_resp = c.get("/connected-accounts")
    assert page_resp.status_code == 200
    assert "freshdesk" in page_resp.text
    assert "my-secret-key" not in page_resp.text  # never render a secret
    csrf = c.cookies.get("hub_csrf")  # that GET rotated the CSRF cookie — re-read it before the next POST

    disconnect_resp = c.post("/connected-accounts/disconnect", data={"provider": "freshdesk", "csrf": csrf}, follow_redirects=False)
    assert disconnect_resp.status_code == 302
    assert connections.get_connection(user_id, "freshdesk") is None


def test_connect_rejects_invalid_freshdesk_domain():
    """SSRF/validation guard: a non-Freshdesk domain must be rejected before
    any outbound request is attempted."""
    if not MONGO_AVAILABLE:
        return
    _reset_db()
    c, user_id = _signed_up_client()
    c.get("/connected-accounts")
    csrf = c.cookies.get("hub_csrf")

    called = {"n": 0}
    import httpx
    orig_request = httpx.request
    def fake_request(*a, **k):
        called["n"] += 1
        return httpx.Response(200, json=[])
    httpx.request = fake_request
    try:
        resp = c.post("/connected-accounts/connect-freshdesk", data={
            "freshdesk_domain": "evil.attacker.com", "freshdesk_api_key": "whatever", "csrf": csrf,
        })
    finally:
        httpx.request = orig_request
    assert resp.status_code == 400
    assert called["n"] == 0, "no outbound request should be attempted for an invalid domain"
    assert connections.get_connection(user_id, "freshdesk") is None


def test_connect_rejects_missing_or_wrong_csrf():
    if not MONGO_AVAILABLE:
        return
    _reset_db()
    c, user_id = _signed_up_client()
    c.get("/connected-accounts")

    resp = c.post("/connected-accounts/connect-freshdesk", data={
        "freshdesk_domain": "x.freshdesk.com", "freshdesk_api_key": "k", "csrf": "wrong-value",
    })
    assert resp.status_code == 400
    assert connections.get_connection(user_id, "freshdesk") is None


def test_connect_rejects_bad_credentials():
    if not MONGO_AVAILABLE:
        return
    _reset_db()
    c, user_id = _signed_up_client()
    c.get("/connected-accounts")
    csrf = c.cookies.get("hub_csrf")

    import httpx
    class _FakeUnauthorized:
        status_code = 401
        headers = {}
        def json(self):
            return {}
    orig_request = httpx.request
    httpx.request = lambda *a, **k: _FakeUnauthorized()
    try:
        resp = c.post("/connected-accounts/connect-freshdesk", data={
            "freshdesk_domain": "x.freshdesk.com", "freshdesk_api_key": "wrong-key", "csrf": csrf,
        })
    finally:
        httpx.request = orig_request
    assert resp.status_code == 401
    assert connections.get_connection(user_id, "freshdesk") is None


def test_connected_accounts_requires_login():
    if not MONGO_AVAILABLE:
        return
    c = TestClient(svr.app)
    resp = c.get("/connected-accounts", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("/login")


if __name__ == "__main__":
    if not MONGO_AVAILABLE:
        print("SKIPPED: no local MongoDB reachable at mongodb://localhost:27017")
        sys.exit(0)
    test_connect_list_disconnect_round_trip()
    test_connect_rejects_invalid_freshdesk_domain()
    test_connect_rejects_missing_or_wrong_csrf()
    test_connect_rejects_bad_credentials()
    test_connected_accounts_requires_login()
    _reset_db()
    print("all checks passed")
