"""Hub login — the connector's own account system, separate from any
Freshdesk (or future provider) credential.

Today's oauth_server.py treats the Freshdesk API key as the login itself:
whoever has a given key can connect "as" that identity. This module adds a
real hub account (email + password) in front of that, so a `connections`
record (future milestones) can key on a stable hub user_id instead of a
provider credential. See PRD Section 7.1 and PLAN.md's "no real user
accounts" limitation.

Two Mongo collections, same flat pymongo style as oauth_server.py's
_clients/_grants — no ORM/model layer for two collections:
- _users: one doc per hub account (email, bcrypt hash).
- _login_sessions: one doc per issued session cookie. Only the SHA-256 hash
  of the cookie value is stored, mirroring how oauth_server.py stores
  access/refresh token hashes, never the raw session cookie value.

Password hashing is bcrypt, not the MD5 used in Humantic's own
users.controller.js (unsalted, broken for this purpose) — a deliberate
deviation from that reference code, not an oversight.
"""
import hashlib
import secrets
import time
from dataclasses import dataclass

import bcrypt
from bson import ObjectId
from pymongo.errors import DuplicateKeyError

try:
    import db as _dbmod  # flat import, local dev (--app-dir src)
except ImportError:
    from src import db as _dbmod  # package-relative, Vercel

SESSION_TTL_SECONDS = 30 * 24 * 60 * 60  # 30 days, a browser login session — independent of the MCP access/refresh token lifetimes in oauth_server.py

_db = _dbmod.db  # shared connection/db (src/db.py) — same pool oauth_server.py uses, not a second one
_users = _db["users"]
_login_sessions = _db["login_sessions"]

_users.create_index("email", unique=True)
_login_sessions.create_index("expires_at", expireAfterSeconds=0)  # TTL: Mongo deletes once expires_at is in the past


class AuthError(Exception):
    pass


@dataclass
class HubUser:
    user_id: str
    email: str


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def signup(email: str, password: str) -> HubUser:
    email = email.strip().lower()
    if not email or "@" not in email:
        raise AuthError("invalid email")
    if len(password) < 8:
        raise AuthError("password must be at least 8 characters")

    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    try:
        result = _users.insert_one({
            "email": email,
            "password_hash": password_hash,
            "created_at": time.time(),
        })
    except DuplicateKeyError as exc:
        raise AuthError("an account with this email already exists") from exc
    return HubUser(user_id=str(result.inserted_id), email=email)


def verify_password(email: str, password: str) -> HubUser:
    email = email.strip().lower()
    doc = _users.find_one({"email": email})
    if not doc or not bcrypt.checkpw(password.encode(), doc["password_hash"].encode()):
        raise AuthError("invalid email or password")
    return HubUser(user_id=str(doc["_id"]), email=email)


def create_session(user_id: str) -> str:
    """Issues a new session cookie value for an already-authenticated user.
    Returns the raw token to set as the cookie; only its hash is stored."""
    token = secrets.token_urlsafe(32)
    _login_sessions.insert_one({
        "token_hash": _sha256(token),
        "user_id": user_id,
        "created_at": time.time(),
        "expires_at": time.time() + SESSION_TTL_SECONDS,
    })
    return token


def resolve_session(token: str | None) -> HubUser | None:
    """Looks up the hub user for a session cookie value. Returns None for a
    missing, unknown, or expired cookie — callers treat that as "not logged
    in," not an error, since an anonymous visitor is the expected case for
    most routes that call this."""
    if not token:
        return None
    doc = _login_sessions.find_one({"token_hash": _sha256(token), "expires_at": {"$gte": time.time()}})
    if not doc:
        return None
    user = _users.find_one({"_id": ObjectId(doc["user_id"])})
    if not user:
        return None
    return HubUser(user_id=str(user["_id"]), email=user["email"])


def destroy_session(token: str) -> None:
    _login_sessions.delete_one({"token_hash": _sha256(token)})
