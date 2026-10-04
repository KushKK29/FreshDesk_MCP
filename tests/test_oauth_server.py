"""Unit tests for oauth_server.py's pure logic (PKCE, token hashing, discovery
shape) plus a FastAPI TestClient smoke test of client registration.

Uses a real local MongoDB on a throwaway test database (mongodb://localhost:27017,
db name test_freshdesk_connector_oauth) rather than mongomock — this repo
already depends on a running Mongo for oauth_server.py itself, so no new
test-only dependency. Requires `mongod` running locally; skips cleanly if not.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

os.environ.setdefault("FRESHDESK_DOMAIN", "test.freshdesk.com")
os.environ.setdefault("FRESHDESK_API_KEY", "fake_key")
os.environ["MONGO_URI"] = "mongodb://localhost:27017"
os.environ["OAUTH_BASE_URL"] = "http://localhost:8004"

try:
    from pymongo import MongoClient
    MongoClient(os.environ["MONGO_URI"], serverSelectionTimeoutMS=1000).admin.command("ping")
    MONGO_AVAILABLE = True
except Exception:
    MONGO_AVAILABLE = False

if MONGO_AVAILABLE:
    import oauth_server as svr
    # Redirect this module's Mongo handles to an isolated test database.
    svr._db = svr._mongo["test_freshdesk_connector_oauth"]
    svr._clients = svr._db["oauth_clients"]
    svr._grants = svr._db["oauth_grants"]

    from fastapi.testclient import TestClient
    client = TestClient(svr.app)


def _reset_db():
    svr._clients.delete_many({})
    svr._grants.delete_many({})


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
    _reset_db()
    print("all checks passed")
