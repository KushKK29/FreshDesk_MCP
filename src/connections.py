"""`connections` collection (PRD §7.2): one connected provider account per
hub user. Credentials are encrypted at the boundary here via vault.py — no
caller outside this module ever sees `credentials_enc` directly, same
"never touch the raw secret outside the module that owns it" discipline
auth.py already uses for session cookies.

Same flat pymongo style as auth.py/oauth_server.py — one collection, no ORM.
"""
import os
import time

from pymongo import MongoClient

try:
    import vault
except ImportError:
    from src import vault

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")

_mongo = MongoClient(MONGO_URI)
_db = _mongo["freshdesk_connector"]
_connections = _db["connections"]

_connections.create_index([("user_id", 1), ("provider", 1)], unique=True)
# One connection per (user_id, provider) in v1 (PRD §7.2 non-goal: no
# multi-account-per-provider) — a second create_connection for the same pair
# overwrites via upsert below, by design, not a bug.


def create_connection(
    user_id: str,
    provider: str,
    auth_type: str,
    credentials: dict,
    scopes: list[str] | None = None,
    expires_at: float | None = None,
    dc: str | None = None,
) -> str:
    """Creates or overwrites the (user_id, provider) connection. Returns the
    connection's string id."""
    doc = {
        "user_id": user_id,
        "provider": provider,
        "status": "active",
        "auth_type": auth_type,
        "credentials_enc": vault.encrypt(credentials),
        "scopes": scopes or [],
        "expires_at": expires_at,
        "dc": dc,
        "created_at": time.time(),
        "last_used_at": None,
        "enabled_tools": None,  # None = all tools enabled; a list restricts to those names (PRD F7)
    }
    _connections.update_one(
        {"user_id": user_id, "provider": provider},
        {"$set": doc},
        upsert=True,
    )
    row = _connections.find_one({"user_id": user_id, "provider": provider})
    return str(row["_id"])


def get_connection(user_id: str, provider: str) -> dict | None:
    """Returns the connection with credentials already decrypted under the
    `credentials` key, or None if not connected / not active."""
    row = _connections.find_one({"user_id": user_id, "provider": provider, "status": {"$ne": "revoked"}})
    if not row:
        return None
    row["credentials"] = vault.decrypt(row["credentials_enc"])
    del row["credentials_enc"]
    row["_id"] = str(row["_id"])
    return row


def list_connections_for_user(user_id: str) -> list[dict]:
    """Status/metadata only, no decrypted credentials — for the Connected
    accounts page (PRD F10), which must never render a secret."""
    rows = list(_connections.find({"user_id": user_id, "status": {"$ne": "revoked"}}))
    for row in rows:
        row["_id"] = str(row["_id"])
        del row["credentials_enc"]
    return rows


def touch_last_used(user_id: str, provider: str) -> None:
    _connections.update_one({"user_id": user_id, "provider": provider}, {"$set": {"last_used_at": time.time()}})


def mark_needs_reauth(user_id: str, provider: str) -> None:
    _connections.update_one({"user_id": user_id, "provider": provider}, {"$set": {"status": "needs_reauth"}})


def update_credentials(user_id: str, provider: str, credentials: dict, expires_at: float | None = None) -> None:
    """Used after a token refresh (Task 9) to store the new tokens."""
    _connections.update_one(
        {"user_id": user_id, "provider": provider},
        {"$set": {"credentials_enc": vault.encrypt(credentials), "expires_at": expires_at, "status": "active"}},
    )


def disconnect(user_id: str, provider: str) -> None:
    """Deletes the connection row and its encrypted credentials outright
    (not a soft revoke) — PRD F6: disconnect "deletes stored credentials"."""
    _connections.delete_one({"user_id": user_id, "provider": provider})
