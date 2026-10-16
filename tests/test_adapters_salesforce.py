"""Mocked-HTTP unit tests for the Salesforce adapter (M2 Task 13, PRD N5 and
§9 security review). The SOQL guard gets extra coverage here since it's the
adapter's real safety boundary beyond "trust the OAuth scope"."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import httpx

import adapters
import soql_guard
from adapters.salesforce import SalesforceAdapter

adapter = SalesforceAdapter()


def _conn() -> adapters.Connection:
    return adapters.Connection(
        user_id="u1", provider="salesforce", auth_type="oauth2",
        credentials={"access_token": "fake-token", "instance_url": "https://example.my.salesforce.com"},
    )


def _patch_httpx_request(json_body: dict, status_code: int = 200):
    orig = httpx.request
    def fake_request(method, url, **kwargs):
        return httpx.Response(status_code, json=json_body, request=httpx.Request(method, url))
    httpx.request = fake_request
    return orig


def test_tools_are_unique_and_namespaced():
    names = [t.name for t in adapter.tools()]
    assert len(names) == len(set(names))
    assert all(n.startswith("salesforce_") for n in names)


def test_query_soql_happy_path():
    orig = _patch_httpx_request({"records": [{"Id": "001", "Name": "Acme"}]})
    try:
        result = adapter.call("salesforce_query_soql", {"soql": "SELECT Id, Name FROM Account"}, _conn())
    finally:
        httpx.request = orig
    assert result == {"records": [{"Id": "001", "Name": "Acme"}]}


def test_query_soql_rejects_dml():
    for bad_soql in [
        "INSERT INTO Account (Name) VALUES ('x')",
        "UPDATE Account SET Name='hacked'",
        "DELETE FROM Account",
        "SELECT Id FROM Account; DELETE FROM Account",
    ]:
        try:
            adapter.call("salesforce_query_soql", {"soql": bad_soql}, _conn())
            assert False, f"should have rejected: {bad_soql}"
        except soql_guard.SoqlRejected:
            pass


def test_query_soql_caps_oversized_limit():
    captured_soql = {}
    orig = httpx.request
    def fake_request(method, url, **kwargs):
        captured_soql["q"] = kwargs["params"]["q"]
        return httpx.Response(200, json={"records": []}, request=httpx.Request(method, url))
    httpx.request = fake_request
    try:
        adapter.call("salesforce_query_soql", {"soql": "SELECT Id FROM Account LIMIT 999999"}, _conn())
    finally:
        httpx.request = orig
    assert "LIMIT 2000" in captured_soql["q"]


def test_convert_lead():
    orig = _patch_httpx_request({"success": True, "accountId": "001", "contactId": "003", "opportunityId": "006"})
    try:
        result = adapter.call("salesforce_convert_lead", {"lead_id": "00Q1"}, _conn())
    finally:
        httpx.request = orig
    assert result["success"] is True


def test_merge_records_requires_confirm_flag_set():
    spec = next(t for t in adapter.tools() if t.name == "salesforce_merge_records")
    assert spec.requires_confirm is True
    assert spec.destructive_hint is True


def test_bulk_ingest_requires_confirm_flag_set():
    spec = next(t for t in adapter.tools() if t.name == "salesforce_bulk_ingest")
    assert spec.requires_confirm is True


def test_send_email_requires_confirm_flag_set():
    spec = next(t for t in adapter.tools() if t.name == "salesforce_send_email")
    assert spec.requires_confirm is True


def test_create_lead_not_confirm_gated():
    """Plain create/update aren't in the PC category per the session's scope
    decision — only delete/merge/bulk/send are."""
    spec = next(t for t in adapter.tools() if t.name == "salesforce_create_lead")
    assert spec.requires_confirm is False
    assert spec.read_only_hint is False


def test_instance_url_comes_only_from_connection_credentials():
    """SSRF guard (PRD §7.5): the adapter must never accept an instance_url
    as a tool argument — it only reads conn.credentials["instance_url"]."""
    import inspect
    source = inspect.getsource(adapter.call)
    assert 'args["instance_url"]' not in source
    assert 'args.get("instance_url")' not in source


def test_unknown_tool_raises():
    try:
        adapter.call("salesforce_nonexistent", {}, _conn())
        assert False, "should have raised"
    except ValueError:
        pass


if __name__ == "__main__":
    test_tools_are_unique_and_namespaced()
    test_query_soql_happy_path()
    test_query_soql_rejects_dml()
    test_query_soql_caps_oversized_limit()
    test_convert_lead()
    test_merge_records_requires_confirm_flag_set()
    test_bulk_ingest_requires_confirm_flag_set()
    test_send_email_requires_confirm_flag_set()
    test_create_lead_not_confirm_gated()
    test_instance_url_comes_only_from_connection_credentials()
    test_unknown_tool_raises()
    print("all checks passed")
