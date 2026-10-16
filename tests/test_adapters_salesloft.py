"""Mocked-HTTP unit tests for the SalesLoft adapter (M3 Task 15, PRD N5)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import httpx

import adapters
from adapters.salesloft import SalesLoftAdapter

adapter = SalesLoftAdapter()


def _conn() -> adapters.Connection:
    return adapters.Connection(user_id="u1", provider="salesloft", auth_type="oauth2", credentials={"access_token": "fake-token"})


def _patch_httpx_request(json_body: dict, status_code: int = 200):
    orig = httpx.request
    def fake_request(method, url, **kwargs):
        return httpx.Response(status_code, json=json_body, request=httpx.Request(method, url))
    httpx.request = fake_request
    return orig


def test_tools_are_unique_and_namespaced():
    names = [t.name for t in adapter.tools()]
    assert len(names) == len(set(names))
    assert all(n.startswith("salesloft_") for n in names)


def test_list_people():
    orig = _patch_httpx_request({"data": [{"id": 1, "name": "Jane"}]})
    try:
        result = adapter.call("salesloft_list_people", {}, _conn())
    finally:
        httpx.request = orig
    assert result == {"results": [{"id": 1, "name": "Jane"}]}


def test_enroll_in_cadence():
    orig = _patch_httpx_request({"data": {"id": 7}})
    try:
        result = adapter.call("salesloft_enroll_in_cadence", {"person_id": "1", "cadence_id": "2"}, _conn())
    finally:
        httpx.request = orig
    assert result["id"] == 7


def test_remove_membership_requires_confirm():
    spec = next(t for t in adapter.tools() if t.name == "salesloft_remove_membership")
    assert spec.requires_confirm is True


def test_pause_resume_not_confirm_gated():
    pause_spec = next(t for t in adapter.tools() if t.name == "salesloft_pause_membership")
    resume_spec = next(t for t in adapter.tools() if t.name == "salesloft_resume_membership")
    assert pause_spec.requires_confirm is False
    assert resume_spec.requires_confirm is False


def test_read_only_activity_resources_present():
    names = {t.name for t in adapter.tools()}
    assert "salesloft_list_cadences" in names
    assert "salesloft_list_people" in names  # not "salesloft_list_persons" — tool names follow the actual resource path, not obj+"s"
    assert "salesloft_list_activities_emails" in names


def test_no_send_email_tool_exists():
    """Plan flag: SalesLoft's native send-email endpoint wasn't confirmed
    against primary docs — this adapter deliberately has no send_email tool
    until that's verified directly."""
    names = {t.name for t in adapter.tools()}
    assert not any("send_email" in n for n in names)


if __name__ == "__main__":
    test_tools_are_unique_and_namespaced()
    test_list_people()
    test_enroll_in_cadence()
    test_remove_membership_requires_confirm()
    test_pause_resume_not_confirm_gated()
    test_read_only_activity_resources_present()
    test_no_send_email_tool_exists()
    print("all checks passed")
