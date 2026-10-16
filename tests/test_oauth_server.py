"""Unit tests for oauth_server.py's pure logic (PKCE, token hashing, discovery
shape) plus a FastAPI TestClient smoke test of client registration.

Uses a real local MongoDB on a throwaway test database (mongodb://localhost:27017,
db name test_freshdesk_connector_oauth) rather than mongomock — this repo
already depends on a running Mongo for oauth_server.py itself, so no new
test-only dependency. Requires `mongod` running locally; skips cleanly if not.
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
os.environ.setdefault("VAULT_MASTER_KEY", "QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE=")  # 32 raw bytes, base64 — test-only key

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
    # Redirect this module's Mongo handles to an isolated test database.
    svr._db = _dbmod._mongo["test_freshdesk_connector_oauth"]
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

    from fastapi.testclient import TestClient
    client = TestClient(svr.app)


def _reset_db():
    svr._clients.delete_many({})
    svr._grants.delete_many({})
    svr._pending_authorizations.delete_many({})
    hub_auth._users.delete_many({})
    hub_auth._login_sessions.delete_many({})
    connections._connections.delete_many({})


def _signed_in_client():
    """A fresh TestClient logged in as a newly signed-up hub user — most
    /oauth/authorize tests need a real hub login now that it's gated.
    Signup is CSRF-protected (double-submit cookie), so a GET /signup comes
    first to pick up the csrf cookie, whose value is then read straight
    from the client's cookie jar and sent back as the form field."""
    c = TestClient(svr.app)
    c.get("/signup")
    csrf = c.cookies.get("hub_csrf")
    c.post("/signup", data={"email": f"test-{secrets.token_hex(4)}@example.com", "password": "supersecret123", "next": "", "csrf": csrf})
    return c


def test_pkce_challenge_matches_known_vector():
    # RFC 7636 appendix B test vector
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    expected_challenge = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    assert svr._b64url_sha256(verifier) == expected_challenge


def test_sha256_is_deterministic_and_distinct():
    assert svr._sha256("abc") == svr._sha256("abc")
    assert svr._sha256("abc") != svr._sha256("abd")


def test_discovery_endpoint_shape():
    if not MONGO_AVAILABLE:
        return
    resp = client.get("/.well-known/oauth-authorization-server")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code_challenge_methods_supported"] == ["S256"]
    assert "authorization_code" in body["grant_types_supported"]
    assert "refresh_token" in body["grant_types_supported"]


def test_register_client_rejects_missing_redirect_uris():
    if not MONGO_AVAILABLE:
        return
    resp = client.post("/oauth/register", json={"client_name": "no-uris"})
    assert resp.status_code == 400


def test_register_client_succeeds_and_persists():
    if not MONGO_AVAILABLE:
        return
    _reset_db()
    resp = client.post(
        "/oauth/register",
        json={"client_name": "test-client", "redirect_uris": ["https://example.com/callback"]},
    )
    assert resp.status_code == 201
    client_id = resp.json()["client_id"]
    assert svr._clients.find_one({"client_id": client_id}) is not None


def test_authorize_rejects_unknown_client():
    if not MONGO_AVAILABLE:
        return
    resp = client.get("/oauth/authorize", params={
        "client_id": "nonexistent_client",
        "redirect_uri": "https://example.com/callback",
        "response_type": "code",
        "code_challenge": "x" * 43,
        "code_challenge_method": "S256",
    })
    assert resp.status_code == 400


def test_authorize_rejects_unregistered_redirect_uri():
    if not MONGO_AVAILABLE:
        return
    _reset_db()
    reg = client.post(
        "/oauth/register",
        json={"client_name": "redirect-test", "redirect_uris": ["https://example.com/callback"]},
    ).json()
    resp = client.get("/oauth/authorize", params={
        "client_id": reg["client_id"],
        "redirect_uri": "https://evil.example.com/callback",  # not in the registered list
        "response_type": "code",
        "code_challenge": "x" * 43,
        "code_challenge_method": "S256",
    })
    assert resp.status_code == 400


def test_token_endpoint_rejects_unknown_code():
    if not MONGO_AVAILABLE:
        return
    resp = client.post("/oauth/token", data={
        "grant_type": "authorization_code",
        "code": "nonexistent_code",
        "code_verifier": "x" * 43,
        "redirect_uri": "https://example.com/callback",
        "client_id": "whatever",
    })
    assert resp.status_code == 400


def test_tickets_endpoint_requires_bearer_token():
    if not MONGO_AVAILABLE:
        return
    resp = client.get("/tickets")
    assert resp.status_code == 401


def test_authorize_without_login_redirects_to_login():
    if not MONGO_AVAILABLE:
        return
    _reset_db()
    reg = client.post(
        "/oauth/register",
        json={"client_name": "login-gate-test", "redirect_uris": ["https://example.com/callback"]},
    ).json()
    anon = TestClient(svr.app)  # fresh client, no session cookie
    resp = anon.get("/oauth/authorize", params={
        "client_id": reg["client_id"],
        "redirect_uri": "https://example.com/callback",
        "response_type": "code",
        "code_challenge": "x" * 43,
        "code_challenge_method": "S256",
    }, follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("/login?")


def test_authorize_with_login_shows_consent_with_csrf():
    if not MONGO_AVAILABLE:
        return
    _reset_db()
    reg = client.post(
        "/oauth/register",
        json={"client_name": "consent-test", "redirect_uris": ["https://example.com/callback"]},
    ).json()
    c = _signed_in_client()
    resp = c.get("/oauth/authorize", params={
        "client_id": reg["client_id"],
        "redirect_uri": "https://example.com/callback",
        "response_type": "code",
        "code_challenge": "x" * 43,
        "code_challenge_method": "S256",
    })
    assert resp.status_code == 200
    assert 'name="csrf"' in resp.text
    assert svr._pending_authorizations.find_one({}) is not None


def test_consent_decision_rejects_bad_csrf():
    if not MONGO_AVAILABLE:
        return
    _reset_db()
    reg = client.post(
        "/oauth/register",
        json={"client_name": "csrf-test", "redirect_uris": ["https://example.com/callback"]},
    ).json()
    c = _signed_in_client()
    c.get("/oauth/authorize", params={
        "client_id": reg["client_id"],
        "redirect_uri": "https://example.com/callback",
        "response_type": "code",
        "code_challenge": "x" * 43,
        "code_challenge_method": "S256",
    })
    pending = svr._pending_authorizations.find_one({})
    resp = c.post("/oauth/authorize/decision", data={
        "session_id": pending["session_id"],
        "csrf": "not-the-real-nonce",
        "decision": "approve",
    })
    assert resp.status_code == 400


def test_grant_records_hub_user_id():
    if not MONGO_AVAILABLE:
        return
    _reset_db()
    reg = client.post(
        "/oauth/register",
        json={"client_name": "grant-user-id-test", "redirect_uris": ["https://example.com/callback"]},
    ).json()
    c = _signed_in_client()
    c.get("/oauth/authorize", params={
        "client_id": reg["client_id"],
        "redirect_uri": "https://example.com/callback",
        "response_type": "code",
        "code_challenge": "x" * 43,
        "code_challenge_method": "S256",
    })
    pending = svr._pending_authorizations.find_one({})

    resp = c.post("/oauth/authorize/decision", data={
        "session_id": pending["session_id"],
        "csrf": pending["csrf"],
        "decision": "approve",
    }, follow_redirects=False)
    assert resp.status_code == 302
    grant = svr._grants.find_one({})
    assert grant is not None
    assert grant.get("user_id") is not None


def test_consent_page_escapes_malicious_client_name():
    if not MONGO_AVAILABLE:
        return
    _reset_db()
    reg = client.post(
        "/oauth/register",
        json={"client_name": "<script>alert(1)</script>", "redirect_uris": ["https://example.com/callback"]},
    ).json()
    c = _signed_in_client()
    resp = c.get("/oauth/authorize", params={
        "client_id": reg["client_id"],
        "redirect_uri": "https://example.com/callback",
        "response_type": "code",
        "code_challenge": "x" * 43,
        "code_challenge_method": "S256",
    })
    assert "<script>alert(1)</script>" not in resp.text
    assert "&lt;script&gt;" in resp.text


def test_authorize_decision_requires_matching_hub_session():
    """The POST that issues a grant must itself require the SAME hub login
    the pending record was created under — not just a valid-looking
    session_id+csrf pair. Simulates a leaked session_id+csrf (e.g. from a
    Referer header) being replayed from a browser that never actually
    logged in as that hub user."""
    if not MONGO_AVAILABLE:
        return
    _reset_db()
    reg = client.post(
        "/oauth/register",
        json={"client_name": "session-check-test", "redirect_uris": ["https://example.com/callback"]},
    ).json()
    victim = _signed_in_client()
    victim.get("/oauth/authorize", params={
        "client_id": reg["client_id"],
        "redirect_uri": "https://example.com/callback",
        "response_type": "code",
        "code_challenge": "x" * 43,
        "code_challenge_method": "S256",
    })
    pending = svr._pending_authorizations.find_one({"client_id": reg["client_id"]})

    attacker = TestClient(svr.app)  # no login session at all
    resp = attacker.post("/oauth/authorize/decision", data={
        "session_id": pending["session_id"],
        "csrf": pending["csrf"],
        "decision": "approve",
    })
    assert resp.status_code == 400
    assert svr._grants.find_one({}) is None  # no grant should have been issued


def test_pending_authorization_survives_simulated_restart():
    """The old in-memory `_sessions` dict lost every pending consent flow on
    a restart (PLAN.md Limitations). Simulating that here means re-reading
    the pending-authorization record straight from Mongo rather than from
    any process-local state — proving the fix doesn't depend on the server
    process staying alive between the GET and the POST."""
    if not MONGO_AVAILABLE:
        return
    _reset_db()
    reg = client.post(
        "/oauth/register",
        json={"client_name": "restart-test", "redirect_uris": ["https://example.com/callback"]},
    ).json()
    c = _signed_in_client()
    c.get("/oauth/authorize", params={
        "client_id": reg["client_id"],
        "redirect_uri": "https://example.com/callback",
        "response_type": "code",
        "code_challenge": "x" * 43,
        "code_challenge_method": "S256",
    })
    # Read the pending record back exactly as a freshly-restarted process
    # would — purely from Mongo, no reference to any object the first
    # request created in memory.
    from_db = svr._pending_authorizations.find_one({"client_id": reg["client_id"]})
    assert from_db is not None
    assert from_db["csrf"]
    assert from_db["hub_user_id"]


def test_consent_no_longer_collects_any_provider_credentials():
    """Fix: the MCP consent page used to hardcode a Freshdesk domain/API-key
    form and REQUIRE it to approve any client — breaking first-time connect
    for every other provider (a ChatGPT user connecting Salesforce only was
    forced through a Freshdesk form with no way to skip it). The grant is
    now just "this MCP client may access the hub account"; which providers'
    tools it sees is decided later, per call, from /connected-accounts —
    not at consent time, and not tied to any one provider."""
    if not MONGO_AVAILABLE:
        return
    _reset_db()
    reg = client.post(
        "/oauth/register",
        json={"client_name": "no-provider-creds-test", "redirect_uris": ["https://example.com/callback"]},
    ).json()
    c = _signed_in_client()
    get_resp = c.get("/oauth/authorize", params={
        "client_id": reg["client_id"],
        "redirect_uri": "https://example.com/callback",
        "response_type": "code",
        "code_challenge": "x" * 43,
        "code_challenge_method": "S256",
    })
    # The consent page itself must not ask for any provider's credentials —
    # no domain/API-key inputs, no "Freshdesk" in the heading.
    assert "freshdesk_domain" not in get_resp.text
    assert "freshdesk_api_key" not in get_resp.text
    assert "wants to access your Freshdesk tickets" not in get_resp.text

    pending = svr._pending_authorizations.find_one({})

    # Approving with ONLY session_id/csrf/decision — no provider fields —
    # must succeed and issue a grant with no credential-shaped keys at all.
    resp = c.post("/oauth/authorize/decision", data={
        "session_id": pending["session_id"],
        "csrf": pending["csrf"],
        "decision": "approve",
    }, follow_redirects=False)
    assert resp.status_code == 302

    grant = svr._grants.find_one({})
    assert grant is not None
    assert "freshdesk_api_key" not in grant
    assert "freshdesk_domain" not in grant
    assert connections.get_connection(grant["user_id"], "freshdesk") is None  # consent alone connects nothing


if __name__ == "__main__":
    if not MONGO_AVAILABLE:
        print("SKIPPED: no local MongoDB reachable at mongodb://localhost:27017")
        sys.exit(0)
    test_pkce_challenge_matches_known_vector()
    test_sha256_is_deterministic_and_distinct()
    test_discovery_endpoint_shape()
    test_register_client_rejects_missing_redirect_uris()
    test_register_client_succeeds_and_persists()
    test_authorize_rejects_unknown_client()
    test_authorize_rejects_unregistered_redirect_uri()
    test_token_endpoint_rejects_unknown_code()
    test_tickets_endpoint_requires_bearer_token()
    test_authorize_without_login_redirects_to_login()
    test_authorize_with_login_shows_consent_with_csrf()
    test_consent_decision_rejects_bad_csrf()
    test_grant_records_hub_user_id()
    test_consent_page_escapes_malicious_client_name()
    test_authorize_decision_requires_matching_hub_session()
    test_pending_authorization_survives_simulated_restart()
    test_consent_no_longer_collects_any_provider_credentials()
    _reset_db()
    print("all checks passed")
