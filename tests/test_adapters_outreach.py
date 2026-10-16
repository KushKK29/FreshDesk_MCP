"""Mocked-HTTP unit tests for the Outreach adapter (M3 Task 15, PRD N5)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import httpx

import adapters
from adapters.outreach import OutreachAdapter

adapter = OutreachAdapter()


def _conn() -> adapters.Connection:
    return adapters.Connection(user_id="u1", provider="outreach", auth_type="oauth2", credentials={"access_token": "fake-token"})


def _patch_httpx_request(json_body: dict, status_code: int = 200):
    orig = httpx.request
    def fake_request(method, url, **kwargs):
        return httpx.Response(status_code, json=json_body, request=httpx.Request(method, url))
    httpx.request = fake_request
    return orig


def test_tools_are_unique_and_namespaced():
    names = [t.name for t in adapter.tools()]
    assert len(names) == len(set(names))
    assert all(n.startswith("outreach_") for n in names)


def test_list_prospects():
    orig = _patch_httpx_request({"data": [{"id": "1", "attributes": {"name": "Jane"}}]})
    try:
        result = adapter.call("outreach_list_prospects", {}, _conn())
    finally:
        httpx.request = orig
    assert result == {"results": [{"id": "1", "attributes": {"name": "Jane"}}]}


def test_enroll_in_sequence():
    orig = _patch_httpx_request({"data": {"id": "99", "type": "sequenceState"}})
    try:
        result = adapter.call("outreach_enroll_in_sequence", {"prospect_id": "1", "sequence_id": "2", "mailbox_id": "3"}, _conn())
    finally:
        httpx.request = orig
    assert result["id"] == "99"


def test_request_compliance_deletion_requires_confirm():
    spec = next(t for t in adapter.tools() if t.name == "outreach_request_compliance_deletion")
    assert spec.requires_confirm is True
    assert spec.destructive_hint is True


def test_create_prospect_not_confirm_gated():
    spec = next(t for t in adapter.tools() if t.name == "outreach_create_prospect")
    assert spec.requires_confirm is False
    assert spec.read_only_hint is False


def test_mailboxes_are_read_only_tool():
    spec = next(t for t in adapter.tools() if t.name == "outreach_list_mailboxes")
    assert spec.read_only_hint is True


if __name__ == "__main__":
    test_tools_are_unique_and_namespaced()
    test_list_prospects()
    test_enroll_in_sequence()
    test_request_compliance_deletion_requires_confirm()
    test_create_prospect_not_confirm_gated()
    test_mailboxes_are_read_only_tool()
    print("all checks passed")
