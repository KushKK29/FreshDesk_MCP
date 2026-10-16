"""Unit tests for login_pages.py: XSS escaping on the next param, open
redirect prevention, and login/signup CSRF protection. Same shape as
test_oauth_server.py — a real local MongoDB on a throwaway test database,
skips cleanly if mongod isn't running.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

os.environ["MONGO_URI"] = "mongodb://localhost:27017"
os.environ["OAUTH_BASE_URL"] = "http://localhost:8004"

try:
    from pymongo import MongoClient
    MongoClient(os.environ["MONGO_URI"], serverSelectionTimeoutMS=1000).admin.command("ping")
    MONGO_AVAILABLE = True
except Exception:
    MONGO_AVAILABLE = False

if MONGO_AVAILABLE:
    import db as _dbmod
    import oauth_server as svr  # imported first: its own bottom-of-file `import login_pages` needs
                                 # _PAGE_STYLES/_error_page to already exist in this module's namespace —
                                 # importing login_pages directly here first would recreate that circular import.
    import auth as a
    import login_pages as lp
    a._db = _dbmod._mongo["test_freshdesk_connector_login_pages"]
    a._users = a._db["users"]
    a._login_sessions = a._db["login_sessions"]
    a._users.create_index("email", unique=True)
    a._login_sessions.create_index("expires_at", expireAfterSeconds=0)

    from fastapi.testclient import TestClient
    svr._db = a._db
    svr._clients = svr._db["oauth_clients"]
    svr._grants = svr._db["oauth_grants"]
    svr._pending_authorizations = svr._db["pending_authorizations"]
    client = TestClient(svr.app)


def _reset_db():
    a._users.delete_many({})
    a._login_sessions.delete_many({})


def test_login_page_escapes_malicious_next_param():
    if not MONGO_AVAILABLE:
        return
    resp = client.get("/login", params={"next": '"><script>alert(1)</script>'})
    assert "<script>alert(1)</script>" not in resp.text


def test_login_rejects_absolute_url_next_param_open_redirect():
    if not MONGO_AVAILABLE:
        return
    _reset_db()
    client.get("/signup")
    csrf = client.cookies.get("hub_csrf")
    resp = client.post("/signup", data={
        "email": "openredirect@example.com", "password": "supersecret123",
        "next": "https://evil.example", "csrf": csrf,
    }, follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/"  # unsafe absolute target rejected, falls back to "/"


def test_login_rejects_schemeless_double_slash_open_redirect():
    if not MONGO_AVAILABLE:
        return
    _reset_db()
    client.get("/signup")
    csrf = client.cookies.get("hub_csrf")
    resp = client.post("/signup", data={
        "email": "openredirect2@example.com", "password": "supersecret123",
        "next": "//evil.example", "csrf": csrf,
    }, follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/"


def test_login_rejects_backslash_open_redirect():
    """Security fix: some browsers normalize a leading "/\\" to "//",
    turning what looks like a same-origin path into a protocol-relative
    absolute URL. urlparse alone doesn't see the backslash as a netloc
    separator, so this must be rejected before urlparse runs.

    Each payload uses its own fresh TestClient — the CSRF cookie rotates on
    every GET /signup, so reusing one client+csrf across multiple POSTs in a
    loop would 400 on cookie mismatch before ever reaching _safe_next."""
    if not MONGO_AVAILABLE:
        return
    _reset_db()
    for i, payload in enumerate(["/\\evil.example", "/\\/evil.example", "/\\\\evil.example"]):
        c = TestClient(svr.app)
        c.get("/signup")
        csrf = c.cookies.get("hub_csrf")
        resp = c.post("/signup", data={
            "email": f"backslash{i}@example.com", "password": "supersecret123",
            "next": payload, "csrf": csrf,
        }, follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/", f"payload {payload!r} was not rejected: {resp.headers['location']!r}"


def test_login_rejects_control_char_in_next_param():
    if not MONGO_AVAILABLE:
        return
    _reset_db()
    client.get("/signup")
    csrf = client.cookies.get("hub_csrf")
    resp = client.post("/signup", data={
        "email": "controlchar@example.com", "password": "supersecret123",
        "next": "/\x00evil", "csrf": csrf,
    }, follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/"


def test_login_accepts_safe_relative_next_param():
    if not MONGO_AVAILABLE:
        return
    _reset_db()
    client.get("/signup")
    csrf = client.cookies.get("hub_csrf")
    resp = client.post("/signup", data={
        "email": "saferedirect@example.com", "password": "supersecret123",
        "next": "/oauth/authorize?client_id=abc", "csrf": csrf,
    }, follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/oauth/authorize?client_id=abc"


def test_signup_rejects_missing_csrf():
    if not MONGO_AVAILABLE:
        return
    _reset_db()
    fresh = TestClient(svr.app)  # never visited GET /signup, so no csrf cookie at all
    resp = fresh.post("/signup", data={"email": "nocsrf@example.com", "password": "supersecret123", "next": "", "csrf": "guessed"})
    assert resp.status_code == 400
    assert a._users.find_one({"email": "nocsrf@example.com"}) is None


def test_signup_rejects_mismatched_csrf():
    if not MONGO_AVAILABLE:
        return
    _reset_db()
    client.get("/signup")  # sets a real csrf cookie
    resp = client.post("/signup", data={"email": "badcsrf@example.com", "password": "supersecret123", "next": "", "csrf": "not-the-cookie-value"})
    assert resp.status_code == 400
    assert a._users.find_one({"email": "badcsrf@example.com"}) is None


if __name__ == "__main__":
    if not MONGO_AVAILABLE:
        print("SKIPPED: no local MongoDB reachable at mongodb://localhost:27017")
        sys.exit(0)
    test_login_page_escapes_malicious_next_param()
    test_login_rejects_absolute_url_next_param_open_redirect()
    test_login_rejects_schemeless_double_slash_open_redirect()
    test_login_rejects_backslash_open_redirect()
    test_login_rejects_control_char_in_next_param()
    test_login_accepts_safe_relative_next_param()
    test_signup_rejects_missing_csrf()
    test_signup_rejects_mismatched_csrf()
    _reset_db()
    print("all checks passed")
