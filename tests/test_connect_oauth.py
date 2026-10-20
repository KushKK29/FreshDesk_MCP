"""Mocked-HTTP integration test for connect_oauth.py's generic upstream-OAuth
flow (M1 Task 8): /connect/{provider}/start redirects to the provider's
authorize URL with state+PKCE, /callback exchanges the code and writes a
connection. Uses Asana as the first registered provider.

Same shape as the other integration tests: real local MongoDB, isolated test
db, skips cleanly if mongod isn't running.
"""
import os
import secrets
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

os.environ.setdefault("FRESHDESK_DOMAIN", "test.freshdesk.com")
os.environ.setdefault("FRESHDESK_API_KEY", "fake_key")
os.environ["MONGO_URI"] = "mongodb://localhost:27017"
os.environ["OAUTH_BASE_URL"] = "http://localhost:8004"
os.environ.setdefault("VAULT_MASTER_KEY", "QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE=")
os.environ["ASANA_CLIENT_ID"] = "test-asana-client-id"
os.environ["ASANA_CLIENT_SECRET"] = "test-asana-client-secret"

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
    import connect_oauth

    def _rebind_globals():
        # See test_mcp_http_server.py's _rebind_globals for why this is a
        # function called from _reset_db(), not top-level code: pytest
        # imports every test file during collection before running any
        # test body, and every Mongo-backed test file here repoints these
        # same shared module globals — whichever file imports last wins at
        # import time, silently pointing every earlier file at ITS database.
        svr._db = _dbmod._mongo["test_freshdesk_connector_connect_oauth"]
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
        connect_oauth._pending_connects = svr._db["pending_oauth_connects"]
        connect_oauth._pending_connects.create_index("created_at", expireAfterSeconds=connect_oauth.PENDING_CONNECT_TTL_SECONDS)

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
    connect_oauth._pending_connects.delete_many({})


def _signed_up_client():
    c = TestClient(svr.app)
    c.get("/signup")
    csrf = c.cookies.get("hub_csrf")
    email = f"connectoauth-{secrets.token_hex(4)}@example.com"
    resp = c.post("/signup", data={"email": email, "password": "supersecret123", "next": "", "csrf": csrf}, follow_redirects=False)
    assert resp.status_code == 302, resp.text
    user = hub_auth._users.find_one({"email": email})
    return c, str(user["_id"])


def test_start_redirects_with_state_and_pkce():
    if not MONGO_AVAILABLE:
        return
    _reset_db()
    c, user_id = _signed_up_client()

    resp = c.get("/connect/asana/start", follow_redirects=False)
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith("https://app.asana.com/-/oauth_authorize")
    qs = parse_qs(urlparse(location).query)
    assert qs["client_id"] == ["test-asana-client-id"]
    assert "state" in qs
    assert "code_challenge" in qs
    assert qs["code_challenge_method"] == ["S256"]

    pending = connect_oauth._pending_connects.find_one({"state": qs["state"][0]})
    assert pending is not None
    assert pending["hub_user_id"] == user_id
    assert pending["provider"] == "asana"


def test_start_requires_login():
    if not MONGO_AVAILABLE:
        return
    c = TestClient(svr.app)
    resp = c.get("/connect/asana/start", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("/login")


def test_start_unknown_provider_404s():
    if not MONGO_AVAILABLE:
        return
    _reset_db()
    c, _ = _signed_up_client()
    resp = c.get("/connect/not_a_real_provider/start")
    assert resp.status_code == 404


def test_unknown_provider_reflected_in_error_page_is_escaped():
    """Security fix: reflected XSS. An attacker-controlled provider path
    segment must never reach the error page unescaped."""
    if not MONGO_AVAILABLE:
        return
    _reset_db()
    c, _ = _signed_up_client()
    payload = "<script>alert(1)</script>"
    start_resp = c.get(f"/connect/{payload}/start")
    assert start_resp.status_code == 404
    assert "<script>" not in start_resp.text
    assert "&lt;script&gt;" in start_resp.text or payload not in start_resp.text

    callback_resp = c.get(f"/connect/{payload}/callback", params={"code": "x", "state": "y"})
    assert callback_resp.status_code == 404
    assert "<script>" not in callback_resp.text


def test_callback_exchanges_code_and_creates_connection():
    if not MONGO_AVAILABLE:
        return
    _reset_db()
    c, user_id = _signed_up_client()

    start_resp = c.get("/connect/asana/start", follow_redirects=False)
    location = start_resp.headers["location"]
    state = parse_qs(urlparse(location).query)["state"][0]

    import httpx
    class _FakeTokenResp:
        status_code = 200
        def json(self):
            return {"access_token": "at_fake", "refresh_token": "rt_fake", "expires_in": 3600}
    class _FakeVerifyResp:
        status_code = 200
        headers = {}
        def json(self):
            return {"data": {"gid": "me", "name": "Test User"}}

    orig_post = httpx.post
    orig_request = httpx.request
    httpx.post = lambda *a, **k: _FakeTokenResp()
    httpx.request = lambda *a, **k: _FakeVerifyResp()
    try:
        callback_resp = c.get("/connect/asana/callback", params={"code": "fake-code", "state": state}, follow_redirects=False)
    finally:
        httpx.post = orig_post
        httpx.request = orig_request

    assert callback_resp.status_code == 302
    assert callback_resp.headers["location"] == "/connected-accounts"

    conn = connections.get_connection(user_id, "asana")
    assert conn is not None
    assert conn["credentials"]["access_token"] == "at_fake"
    assert conn["credentials"]["refresh_token"] == "rt_fake"
    assert conn["auth_type"] == "oauth2"

    # state is single-use — the pending record must be gone
    assert connect_oauth._pending_connects.find_one({"state": state}) is None


def test_callback_rejects_different_user_than_pending_record():
    """Security fix: account-linking CSRF. An attacker starts their own OAuth
    flow (valid state tied to THEIR hub_user_id), then tricks a victim's
    browser into completing that callback URL. The callback must refuse to
    proceed unless the session completing it is the SAME hub session that
    started it — not just hold a matching state."""
    if not MONGO_AVAILABLE:
        return
    _reset_db()
    attacker_client, attacker_user_id = _signed_up_client()
    victim_client, victim_user_id = _signed_up_client()
    assert attacker_user_id != victim_user_id

    start_resp = attacker_client.get("/connect/asana/start", follow_redirects=False)
    location = start_resp.headers["location"]
    state = parse_qs(urlparse(location).query)["state"][0]

    # Victim's browser (different session) completes the attacker's callback URL.
    callback_resp = victim_client.get("/connect/asana/callback", params={"code": "fake-code", "state": state})
    assert callback_resp.status_code == 403

    # Neither account should end up with an Asana connection from this.
    assert connections.get_connection(attacker_user_id, "asana") is None
    assert connections.get_connection(victim_user_id, "asana") is None


def test_callback_rejects_anonymous_caller():
    if not MONGO_AVAILABLE:
        return
    _reset_db()
    c, user_id = _signed_up_client()
    start_resp = c.get("/connect/asana/start", follow_redirects=False)
    state = parse_qs(urlparse(start_resp.headers["location"]).query)["state"][0]

    anon_client = TestClient(svr.app)  # no login at all
    resp = anon_client.get("/connect/asana/callback", params={"code": "x", "state": state})
    assert resp.status_code == 403
    assert connections.get_connection(user_id, "asana") is None


def test_callback_rejects_unknown_state():
    if not MONGO_AVAILABLE:
        return
    _reset_db()
    c, _ = _signed_up_client()
    resp = c.get("/connect/asana/callback", params={"code": "x", "state": "not-a-real-state"})
    assert resp.status_code == 400


if __name__ == "__main__":
    if not MONGO_AVAILABLE:
        print("SKIPPED: no local MongoDB reachable at mongodb://localhost:27017")
        sys.exit(0)
    test_start_redirects_with_state_and_pkce()
    test_start_requires_login()
    test_start_unknown_provider_404s()
    test_unknown_provider_reflected_in_error_page_is_escaped()
    test_callback_exchanges_code_and_creates_connection()
    test_callback_rejects_different_user_than_pending_record()
    test_callback_rejects_anonymous_caller()
    test_callback_rejects_unknown_state()
    _reset_db()
    print("all checks passed")
