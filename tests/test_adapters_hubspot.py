"""Mocked-HTTP unit tests for the HubSpot adapter (M2 Task 12, PRD N5)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import httpx

import adapters
from adapters.hubspot import HubSpotAdapter

adapter = HubSpotAdapter()


def _conn() -> adapters.Connection:
    return adapters.Connection(user_id="u1", provider="hubspot", auth_type="oauth2", credentials={"access_token": "fake-token"})


def _patch_httpx_request(json_body: dict, status_code: int = 200):
    orig = httpx.request
    def fake_request(method, url, **kwargs):
        return httpx.Response(status_code, json=json_body, request=httpx.Request(method, url))
    httpx.request = fake_request
    return orig


def test_tools_are_unique_and_namespaced():
    names = [t.name for t in adapter.tools()]
    assert len(names) == len(set(names))
    assert all(n.startswith("hubspot_") for n in names)


def test_list_contacts():
    orig = _patch_httpx_request({"results": [{"id": "1", "properties": {"email": "a@b.com"}}]})
    try:
        result = adapter.call("hubspot_list_contacts", {}, _conn())
    finally:
        httpx.request = orig
    assert result == {"results": [{"id": "1", "properties": {"email": "a@b.com"}}]}


def test_create_deal_not_read_only():
    spec = next(t for t in adapter.tools() if t.name == "hubspot_create_deal")
    assert spec.read_only_hint is False
    orig = _patch_httpx_request({"id": "99", "properties": {"dealname": "Big Deal"}})
    try:
        result = adapter.call("hubspot_create_deal", {"properties": {"dealname": "Big Deal"}}, _conn())
    finally:
        httpx.request = orig
    assert result["id"] == "99"


def test_search_deals():
    orig = _patch_httpx_request({"results": [{"id": "5"}]})
    try:
        result = adapter.call("hubspot_search_deals", {"query": {"filterGroups": []}}, _conn())
    finally:
        httpx.request = orig
    assert result == {"results": [{"id": "5"}]}


def test_associate_objects():
    orig = _patch_httpx_request({"status": "ok"})
    try:
        result = adapter.call("hubspot_associate_objects", {"from_type": "contacts", "from_id": "1", "to_type": "deals", "to_id": "2"}, _conn())
    finally:
        httpx.request = orig
    assert result == {"status": "ok"}


def test_batch_create_requires_confirm_flag_set():
    spec = next(t for t in adapter.tools() if t.name == "hubspot_batch_create")
    assert spec.requires_confirm is True
    assert spec.destructive_hint is True


def test_delete_contact_is_destructive_not_confirm_gated():
    """Single-object delete isn't in the PC category (only bulk/send/true-delete
    of bulk scope per session decision) — HubSpot's single delete here is
    marked destructive_hint for client-side caution but doesn't require the
    confirm_token round trip; only batch_* actions do."""
    spec = next(t for t in adapter.tools() if t.name == "hubspot_delete_contact")
    assert spec.destructive_hint is True
    assert spec.requires_confirm is False


def test_unknown_tool_raises():
    try:
        adapter.call("hubspot_nonexistent", {}, _conn())
        assert False, "should have raised"
    except ValueError:
        pass


if __name__ == "__main__":
    test_tools_are_unique_and_namespaced()
    test_list_contacts()
    test_create_deal_not_read_only()
    test_search_deals()
    test_associate_objects()
    test_batch_create_requires_confirm_flag_set()
    test_delete_contact_is_destructive_not_confirm_gated()
    test_unknown_tool_raises()
    print("all checks passed")
