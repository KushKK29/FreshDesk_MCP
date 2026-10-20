"""Unit tests for ops_api.call_crm_tool: the bearer-session Ops UI's path to
call a connected CRM's own write tool (e.g. salesforce_update_contact), not
just support_ops/crm_ops. Imports via oauth_server (the real app entrypoint)
to avoid this codebase's known circular-import order if ops_api were
imported standalone — see test_oauth_server.py for the same pattern.
Requires `mongod` running locally; skips cleanly if not.
"""
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

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
    import oauth_server as svr  # noqa: E402 — real app entrypoint, see module docstring
    ops_api = svr.ops_api
    adapters = ops_api.adapters
    from fastapi import HTTPException

    def _fake_user(authorization):
        return MagicMock(user_id="u1")

    def _fake_spec(name, requires_confirm=False):
        return adapters.ToolSpec(name=name, description="", input_schema={"type": "object", "properties": {}}, requires_confirm=requires_confirm)

    def test_unknown_provider_is_404(monkeypatch):
        monkeypatch.setattr(ops_api, "_user", _fake_user)
        monkeypatch.setattr(adapters, "ADAPTERS", {})
        try:
            ops_api.call_crm_tool("bogus", "whatever", ops_api.ToolCall(args={}), "Bearer x")
            assert False, "expected HTTPException"
        except HTTPException as exc:
            assert exc.status_code == 404

    def test_unknown_tool_is_404(monkeypatch):
        fake_adapter = MagicMock(tools=lambda: [_fake_spec("salesforce_update_contact")])
        monkeypatch.setattr(ops_api, "_user", _fake_user)
        monkeypatch.setattr(adapters, "ADAPTERS", {"salesforce": fake_adapter})
        try:
            ops_api.call_crm_tool("salesforce", "nope", ops_api.ToolCall(args={}), "Bearer x")
            assert False, "expected HTTPException"
        except HTTPException as exc:
            assert exc.status_code == 404

    def test_not_connected_is_409(monkeypatch):
        fake_adapter = MagicMock(tools=lambda: [_fake_spec("salesforce_update_contact")])
        monkeypatch.setattr(ops_api, "_user", _fake_user)
        monkeypatch.setattr(adapters, "ADAPTERS", {"salesforce": fake_adapter})
        monkeypatch.setattr(ops_api.crm_contacts, "get_fresh_conn", lambda uid, provider: (None, "not connected"))
        try:
            ops_api.call_crm_tool("salesforce", "salesforce_update_contact", ops_api.ToolCall(args={"id": "1"}), "Bearer x")
            assert False, "expected HTTPException"
        except HTTPException as exc:
            assert exc.status_code == 409

    def test_success_path_calls_adapter_with_fresh_conn(monkeypatch):
        """This is the exact scenario reported broken: a per-provider write
        tool (salesforce_update_contact) called from the Ops UI's Sync names
        button via the previous call_tool endpoint, which only ever looked up
        tools under the support_ops adapter and always 404'd."""
        fake_conn = MagicMock()
        fake_adapter = MagicMock(tools=lambda: [_fake_spec("salesforce_update_contact")])
        fake_adapter.call.return_value = {"ok": True}

        monkeypatch.setattr(ops_api, "_user", _fake_user)
        monkeypatch.setattr(adapters, "ADAPTERS", {"salesforce": fake_adapter})
        monkeypatch.setattr(ops_api.crm_contacts, "get_fresh_conn", lambda uid, provider: (fake_conn, None))
        monkeypatch.setattr(ops_api.connections, "touch_last_used", lambda uid, provider: None)

        result = ops_api.call_crm_tool(
            "salesforce", "salesforce_update_contact",
            ops_api.ToolCall(args={"id": "003", "data": {"FirstName": "A", "LastName": "B"}}),
            "Bearer x",
        )
        assert result == {"ok": True}
        fake_adapter.call.assert_called_once_with(
            "salesforce_update_contact", {"id": "003", "data": {"FirstName": "A", "LastName": "B"}}, fake_conn,
        )

    def test_requires_confirm_previews_before_calling_adapter(monkeypatch):
        fake_adapter = MagicMock(tools=lambda: [_fake_spec("salesforce_delete_contact", requires_confirm=True)])
        monkeypatch.setattr(ops_api, "_user", _fake_user)
        monkeypatch.setattr(adapters, "ADAPTERS", {"salesforce": fake_adapter})
        out = ops_api.call_crm_tool("salesforce", "salesforce_delete_contact", ops_api.ToolCall(args={"id": "1"}), "Bearer x")
        assert "confirm_token" in out
        fake_adapter.call.assert_not_called()

    def test_auth_error_triggers_refresh_and_retry(monkeypatch):
        fake_conn = MagicMock()
        refreshed_conn = MagicMock()
        fake_adapter = MagicMock(tools=lambda: [_fake_spec("salesforce_update_contact")])
        fake_adapter.call.side_effect = [Exception("500: INVALID_SESSION_ID"), {"ok": True}]

        monkeypatch.setattr(ops_api, "_user", _fake_user)
        monkeypatch.setattr(adapters, "ADAPTERS", {"salesforce": fake_adapter})
        monkeypatch.setattr(ops_api.crm_contacts, "get_fresh_conn", lambda uid, provider: (fake_conn, None))
        monkeypatch.setattr(ops_api.connections, "touch_last_used", lambda uid, provider: None)
        monkeypatch.setattr(ops_api.oauth_refresh, "refresh_and_persist", lambda adapter, conn, provider: refreshed_conn)

        result = ops_api.call_crm_tool(
            "salesforce", "salesforce_update_contact", ops_api.ToolCall(args={"id": "1"}), "Bearer x",
        )
        assert result == {"ok": True}
        assert fake_adapter.call.call_count == 2
        # second call used the refreshed connection, not the stale one
        assert fake_adapter.call.call_args_list[1].args[2] is refreshed_conn


if __name__ == "__main__":
    if not MONGO_AVAILABLE:
        print("mongod not reachable, skipping")
    else:
        import types
        _noop_mp = types.SimpleNamespace(setattr=setattr)
        for name, fn in list(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn(_noop_mp)
        print("all checks passed")
