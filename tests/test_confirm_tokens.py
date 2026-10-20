"""Unit tests for confirm_tokens.py: issue()/consume() is the single-use
preview-then-confirm gate every requires_confirm=True write tool depends on
(destructive/send/bulk CRM actions). Previously had zero direct coverage —
only indirectly exercised through mocked adapters in other test files,
which never called consume() against a real Mongo record.

Same shape as the other integration tests: real local MongoDB, isolated
test db, skips cleanly if mongod isn't running.
"""
import os
import sys
import time
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
    import confirm_tokens as ct

    def _rebind_globals():
        # See test_mcp_http_server.py's _rebind_globals for why this is a
        # function called at the top of every test, not top-level code:
        # pytest imports every test file during collection before running
        # any test body, and confirm_tokens.py's module-level _db/
        # _confirm_tokens point at the real "freshdesk_connector" db by
        # default — redirect to an isolated test db, re-asserted before
        # each test regardless of what ran before it.
        ct._mongo = MongoClient(os.environ["MONGO_URI"])
        ct._db = ct._mongo["test_freshdesk_connector_confirm_tokens"]
        ct._confirm_tokens = ct._db["confirm_tokens"]
        ct._confirm_tokens.create_index("expires_at", expireAfterSeconds=0)
        ct._confirm_tokens.delete_many({})


def test_issue_then_consume_with_matching_args_succeeds():
    if not MONGO_AVAILABLE:
        return
    _rebind_globals()
    token = ct.issue("u1", "salesforce", "salesforce_update_contact", {"id": "003", "data": {"FirstName": "A"}})
    assert ct.consume(token, "u1", "salesforce", "salesforce_update_contact", {"id": "003", "data": {"FirstName": "A"}})


def test_consume_is_single_use():
    if not MONGO_AVAILABLE:
        return
    _rebind_globals()
    token = ct.issue("u1", "salesforce", "salesforce_update_contact", {"id": "1"})
    assert ct.consume(token, "u1", "salesforce", "salesforce_update_contact", {"id": "1"})
    assert not ct.consume(token, "u1", "salesforce", "salesforce_update_contact", {"id": "1"})


def test_consume_rejects_different_args_replay_protection():
    """The core replay-protection guarantee: a confirm_token minted for one
    set of arguments must not authorize a DIFFERENT, more dangerous call."""
    if not MONGO_AVAILABLE:
        return
    _rebind_globals()
    token = ct.issue("u1", "salesforce", "salesforce_update_contact", {"id": "1", "data": {"FirstName": "A"}})
    assert not ct.consume(token, "u1", "salesforce", "salesforce_update_contact", {"id": "1", "data": {"FirstName": "EVIL"}})
    # the token is still valid for its original args — a rejected mismatched
    # attempt must not itself burn the token (find_one_and_delete only
    # matches/deletes on an exact filter hit)
    assert ct.consume(token, "u1", "salesforce", "salesforce_update_contact", {"id": "1", "data": {"FirstName": "A"}})


def test_consume_rejects_wrong_user():
    if not MONGO_AVAILABLE:
        return
    _rebind_globals()
    token = ct.issue("u1", "salesforce", "salesforce_update_contact", {"id": "1"})
    assert not ct.consume(token, "attacker", "salesforce", "salesforce_update_contact", {"id": "1"})


def test_consume_rejects_wrong_provider_or_tool():
    if not MONGO_AVAILABLE:
        return
    _rebind_globals()
    token = ct.issue("u1", "salesforce", "salesforce_update_contact", {"id": "1"})
    assert not ct.consume(token, "u1", "hubspot", "salesforce_update_contact", {"id": "1"})
    assert not ct.consume(token, "u1", "salesforce", "salesforce_delete_contact", {"id": "1"})


def test_consume_rejects_unknown_token():
    if not MONGO_AVAILABLE:
        return
    _rebind_globals()
    assert not ct.consume("not-a-real-token", "u1", "salesforce", "salesforce_update_contact", {"id": "1"})


def test_consume_rejects_expired_token():
    if not MONGO_AVAILABLE:
        return
    _rebind_globals()
    token = ct.issue("u1", "salesforce", "salesforce_update_contact", {"id": "1"})
    # back-date the record past its TTL instead of sleeping 300s
    ct._confirm_tokens.update_one({"token_hash": ct._sha256(token)}, {"$set": {"expires_at": time.time() - 1}})
    assert not ct.consume(token, "u1", "salesforce", "salesforce_update_contact", {"id": "1"})


if __name__ == "__main__":
    if not MONGO_AVAILABLE:
        print("mongod not reachable, skipping")
    else:
        for name, fn in list(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        print("all checks passed")
