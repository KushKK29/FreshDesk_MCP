"""Unit tests for auth.py: hub signup/login, password hashing, session
cookies. Same shape as test_oauth_server.py — a real local MongoDB on a
throwaway test database, skips cleanly if mongod isn't running.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

os.environ["MONGO_URI"] = "mongodb://localhost:27017"

try:
    from pymongo import MongoClient
    MongoClient(os.environ["MONGO_URI"], serverSelectionTimeoutMS=1000).admin.command("ping")
    MONGO_AVAILABLE = True
except Exception:
    MONGO_AVAILABLE = False

if MONGO_AVAILABLE:
    import db as _dbmod
    import auth as a
    a._db = _dbmod._mongo["test_freshdesk_connector_auth"]
    a._users = a._db["users"]
    a._login_sessions = a._db["login_sessions"]
    a._users.create_index("email", unique=True)
    a._login_sessions.create_index("expires_at", expireAfterSeconds=0)


def _reset_db():
    a._users.delete_many({})
    a._login_sessions.delete_many({})


def test_signup_then_login_round_trip():
    if not MONGO_AVAILABLE:
        return
    _reset_db()
    user = a.signup("test@example.com", "supersecret123")
    assert user.email == "test@example.com"
    logged_in = a.verify_password("test@example.com", "supersecret123")
    assert logged_in.user_id == user.user_id


def test_signup_rejects_short_password():
    if not MONGO_AVAILABLE:
        return
    _reset_db()
    try:
        a.signup("short@example.com", "abc")
        assert False, "should have raised AuthError"
    except a.AuthError:
        pass


def test_signup_rejects_duplicate_email():
    if not MONGO_AVAILABLE:
        return
    _reset_db()
    a.signup("dup@example.com", "supersecret123")
    try:
        a.signup("dup@example.com", "anotherpassword")
        assert False, "should have raised AuthError"
    except a.AuthError:
        pass


def test_login_rejects_wrong_password():
    if not MONGO_AVAILABLE:
        return
    _reset_db()
    a.signup("wrongpw@example.com", "supersecret123")
    try:
        a.verify_password("wrongpw@example.com", "notthepassword")
        assert False, "should have raised AuthError"
    except a.AuthError:
        pass


def test_password_never_stored_in_plaintext():
    if not MONGO_AVAILABLE:
        return
    _reset_db()
    a.signup("plain@example.com", "supersecret123")
    doc = a._users.find_one({"email": "plain@example.com"})
    assert "supersecret123" not in doc["password_hash"]
    assert doc["password_hash"].startswith("$2b$")  # bcrypt's own format prefix


def test_session_round_trip():
    if not MONGO_AVAILABLE:
        return
    _reset_db()
    user = a.signup("session@example.com", "supersecret123")
    token = a.create_session(user.user_id)
    resolved = a.resolve_session(token)
    assert resolved is not None
    assert resolved.user_id == user.user_id

    doc = a._login_sessions.find_one({})
    assert token not in str(doc)  # only the hash is stored, never the raw cookie value


def test_resolve_session_rejects_unknown_token():
    if not MONGO_AVAILABLE:
        return
    _reset_db()
    assert a.resolve_session("not-a-real-token") is None
    assert a.resolve_session(None) is None


def test_destroy_session_logs_out():
    if not MONGO_AVAILABLE:
        return
    _reset_db()
    user = a.signup("logout@example.com", "supersecret123")
    token = a.create_session(user.user_id)
    assert a.resolve_session(token) is not None
    a.destroy_session(token)
    assert a.resolve_session(token) is None


if __name__ == "__main__":
    if not MONGO_AVAILABLE:
        print("SKIPPED: no local MongoDB reachable at mongodb://localhost:27017")
        sys.exit(0)
    test_signup_then_login_round_trip()
    test_signup_rejects_short_password()
    test_signup_rejects_duplicate_email()
    test_login_rejects_wrong_password()
    test_password_never_stored_in_plaintext()
    test_session_round_trip()
    test_resolve_session_rejects_unknown_token()
    test_destroy_session_logs_out()
    _reset_db()
    print("all checks passed")
