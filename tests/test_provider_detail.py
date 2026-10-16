"""Integration test for provider_detail.py: the per-provider tool detail
page shows real tool names/descriptions pulled from the adapter, setup
instructions for ChatGPT/Claude, and requires login.

Same shape as test_connected_accounts.py: real local MongoDB, isolated test
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

    svr._db = _dbmod._mongo["test_freshdesk_connector_provider_detail"]
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


def _reset_db():
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
    email = f"provdetail-{secrets.token_hex(4)}@example.com"
    resp = c.post("/signup", data={"email": email, "password": "supersecret123", "next": "", "csrf": csrf}, follow_redirects=False)
    assert resp.status_code == 302, resp.text
    user = hub_auth._users.find_one({"email": email})
    return c, str(user["_id"])


def test_requires_login():
    if not MONGO_AVAILABLE:
        return
    c = TestClient(svr.app)
    resp = c.get("/connected-accounts/freshdesk", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("/login")


def test_unknown_provider_redirects_home():
    if not MONGO_AVAILABLE:
        return
    _reset_db()
    c, _ = _signed_up_client()
    resp = c.get("/connected-accounts/not_a_real_provider", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/connected-accounts"


def test_shows_real_tool_names_and_descriptions():
    """The page must reflect the adapter's actual ToolSpec list, not
    hand-maintained copy — proves it by checking real tool names/
    descriptions from the Freshdesk adapter appear verbatim."""
    if not MONGO_AVAILABLE:
        return
    _reset_db()
    c, _ = _signed_up_client()
    resp = c.get("/connected-accounts/freshdesk")
    assert resp.status_code == 200
    assert "freshdesk_list_tickets" in resp.text
    assert "freshdesk_delete_ticket" in resp.text
    assert "needs confirm" in resp.text  # destructive tool badge


def test_shows_setup_instructions_for_all_clients():
    if not MONGO_AVAILABLE:
        return
    _reset_db()
    c, _ = _signed_up_client()
    resp = c.get("/connected-accounts/freshdesk")
    assert resp.status_code == 200
    assert "ChatGPT" in resp.text
    assert "Claude" in resp.text
    assert "MCP-compatible agent" in resp.text
    assert "http://localhost:8004" in resp.text  # server URL shown


def test_shows_connected_status_after_connecting():
    if not MONGO_AVAILABLE:
        return
    _reset_db()
    c, user_id = _signed_up_client()
    connections.create_connection(
        user_id=user_id, provider="freshdesk", auth_type="api_key",
        credentials={"domain": "x.freshdesk.com", "api_key": "fake"},
    )
    resp = c.get("/connected-accounts/freshdesk")
    assert resp.status_code == 200
    assert "Connected" in resp.text


if __name__ == "__main__":
    if not MONGO_AVAILABLE:
        print("SKIPPED: no local MongoDB reachable at mongodb://localhost:27017")
        sys.exit(0)
    test_requires_login()
    test_unknown_provider_redirects_home()
    test_shows_real_tool_names_and_descriptions()
    test_shows_setup_instructions_for_all_clients()
    test_shows_connected_status_after_connecting()
    _reset_db()
    print("all checks passed")
