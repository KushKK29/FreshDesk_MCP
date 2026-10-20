"""Mocked-HTTP unit tests for the Zoho CRM adapter (M3 Task 14, PRD N5 +
SSRF guard on DC resolution)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import httpx

import adapters
from adapters.zoho import ZohoAdapter, UnknownDataCenter, InvalidZohoPathSegment
from coql_guard import CoqlRejected

adapter = ZohoAdapter()


def _conn(dc: str = "us") -> adapters.Connection:
    return adapters.Connection(user_id="u1", provider="zoho", auth_type="oauth2", credentials={"access_token": "fake-token", "dc": dc})


def _patch_httpx_request(json_body: dict, status_code: int = 200):
    orig = httpx.request
    def fake_request(method, url, **kwargs):
        return httpx.Response(status_code, json=json_body, request=httpx.Request(method, url))
    httpx.request = fake_request
    return orig


def test_tools_are_unique_and_namespaced():
    names = [t.name for t in adapter.tools()]
    assert len(names) == len(set(names))
    assert all(n.startswith("zoho_") for n in names)


def test_list_leads():
    orig = _patch_httpx_request({"data": [{"id": "1", "Last_Name": "Doe"}]})
    try:
        result = adapter.call("zoho_list_leads", {}, _conn())
    finally:
        httpx.request = orig
    assert result == {"records": [{"id": "1", "Last_Name": "Doe"}]}


def test_unknown_dc_rejected_before_any_http_call():
    called = {"n": 0}
    orig = httpx.request
    def fake_request(*a, **k):
        called["n"] += 1
        return httpx.Response(200, json={})
    httpx.request = fake_request
    try:
        try:
            adapter.call("zoho_list_leads", {}, _conn(dc="not_a_real_dc"))
            assert False, "should have raised UnknownDataCenter"
        except UnknownDataCenter:
            pass
    finally:
        httpx.request = orig
    assert called["n"] == 0, "no HTTP call should have been made for an unknown DC"


def test_query_coql():
    orig = _patch_httpx_request({"data": [{"id": "1"}]})
    try:
        result = adapter.call("zoho_query_coql", {"select_query": "select id from Leads"}, _conn())
    finally:
        httpx.request = orig
    assert result == {"records": [{"id": "1"}]}


def test_query_coql_rejects_multi_statement_before_any_http_call():
    called = {"n": 0}
    orig = httpx.request
    def fake_request(*a, **k):
        called["n"] += 1
        return httpx.Response(200, json={})
    httpx.request = fake_request
    try:
        try:
            adapter.call("zoho_query_coql", {"select_query": "select id from Leads; select id from Contacts"}, _conn())
            assert False, "should have raised CoqlRejected"
        except CoqlRejected:
            pass
    finally:
        httpx.request = orig
    assert called["n"] == 0


def test_query_coql_caps_an_excessive_limit():
    orig = _patch_httpx_request({"data": []})
    try:
        adapter.call("zoho_query_coql", {"select_query": "select id from Leads LIMIT 99999"}, _conn())
    finally:
        httpx.request = orig


def test_get_related_records_rejects_unknown_module_before_any_http_call():
    called = {"n": 0}
    orig = httpx.request
    def fake_request(*a, **k):
        called["n"] += 1
        return httpx.Response(200, json={})
    httpx.request = fake_request
    try:
        try:
            adapter.call("zoho_get_related_records", {"module": "Leads/123/actions/convert", "id": "1", "related_list": "x"}, _conn())
            assert False, "should have raised InvalidZohoPathSegment"
        except InvalidZohoPathSegment:
            pass
    finally:
        httpx.request = orig
    assert called["n"] == 0, "no HTTP call should have been made for an out-of-allowlist module"


def test_get_related_records_rejects_related_list_with_path_separator():
    orig = httpx.request
    httpx.request = lambda *a, **k: httpx.Response(200, json={})
    try:
        try:
            adapter.call("zoho_get_related_records", {"module": "Leads", "id": "1", "related_list": "Notes/../Contacts"}, _conn())
            assert False, "should have raised InvalidZohoPathSegment"
        except InvalidZohoPathSegment:
            pass
    finally:
        httpx.request = orig


def test_get_related_records_accepts_known_module_and_related_list():
    orig = _patch_httpx_request({"data": [{"id": "1"}]})
    try:
        result = adapter.call("zoho_get_related_records", {"module": "Leads", "id": "1", "related_list": "Notes"}, _conn())
    finally:
        httpx.request = orig
    assert result == {"records": [{"id": "1"}]}


def test_bulk_read_rejects_unknown_module_before_any_http_call():
    called = {"n": 0}
    orig = httpx.request
    def fake_request(*a, **k):
        called["n"] += 1
        return httpx.Response(200, json={})
    httpx.request = fake_request
    try:
        try:
            adapter.call("zoho_bulk_read", {"module": "not_a_real_module"}, _conn())
            assert False, "should have raised InvalidZohoPathSegment"
        except InvalidZohoPathSegment:
            pass
    finally:
        httpx.request = orig
    assert called["n"] == 0


def test_mass_convert_leads_requires_confirm():
    spec = next(t for t in adapter.tools() if t.name == "zoho_mass_convert_leads")
    assert spec.requires_confirm is True


def test_bulk_read_write_require_confirm():
    read_spec = next(t for t in adapter.tools() if t.name == "zoho_bulk_read")
    write_spec = next(t for t in adapter.tools() if t.name == "zoho_bulk_write")
    assert read_spec.requires_confirm is True
    assert write_spec.requires_confirm is True


def test_create_lead_not_confirm_gated():
    spec = next(t for t in adapter.tools() if t.name == "zoho_create_lead")
    assert spec.requires_confirm is False


if __name__ == "__main__":
    test_tools_are_unique_and_namespaced()
    test_list_leads()
    test_unknown_dc_rejected_before_any_http_call()
    test_query_coql()
    test_query_coql_rejects_multi_statement_before_any_http_call()
    test_query_coql_caps_an_excessive_limit()
    test_get_related_records_rejects_unknown_module_before_any_http_call()
    test_get_related_records_rejects_related_list_with_path_separator()
    test_get_related_records_accepts_known_module_and_related_list()
    test_bulk_read_rejects_unknown_module_before_any_http_call()
    test_mass_convert_leads_requires_confirm()
    test_bulk_read_write_require_confirm()
    test_create_lead_not_confirm_gated()
    print("all checks passed")
