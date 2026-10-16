"""Preview-then-confirm mechanism (PRD §7.4, session scope decision: gates
only destructive/send/bulk actions, marked requires_confirm on their
ToolSpec). First call with no confirm_token returns a preview and a
single-use token bound to (user_id, provider, tool, hash of arguments);
the SAME call must be repeated with that token to actually execute. Binding
to the argument hash stops a confirm_token minted for one call being replayed
against a different, more dangerous set of arguments.

Same hash-not-plaintext, TTL-indexed Mongo collection pattern as every other
token/session in this codebase (auth.py sessions, oauth_server.py grants).
"""
import hashlib
import json
import os
import secrets
import time

from pymongo import MongoClient

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
CONFIRM_TOKEN_TTL_SECONDS = 300

_mongo = MongoClient(MONGO_URI)
_db = _mongo["freshdesk_connector"]
_confirm_tokens = _db["confirm_tokens"]
_confirm_tokens.create_index("expires_at", expireAfterSeconds=0)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _args_hash(args: dict) -> str:
    return _sha256(json.dumps(args, sort_keys=True))


def issue(user_id: str, provider: str, tool: str, args: dict) -> str:
    token = secrets.token_urlsafe(24)
    _confirm_tokens.insert_one({
        "token_hash": _sha256(token),
        "user_id": user_id,
        "provider": provider,
        "tool": tool,
        "args_hash": _args_hash(args),
        "expires_at": time.time() + CONFIRM_TOKEN_TTL_SECONDS,
    })
    return token


def consume(token: str, user_id: str, provider: str, tool: str, args: dict) -> bool:
    """Single-use: a matching record is deleted atomically on first use, so
    a confirm_token can't be replayed even if it hasn't expired yet."""
    result = _confirm_tokens.find_one_and_delete({
        "token_hash": _sha256(token),
        "user_id": user_id,
        "provider": provider,
        "tool": tool,
        "args_hash": _args_hash(args),
        "expires_at": {"$gte": time.time()},
    })
    return result is not None
