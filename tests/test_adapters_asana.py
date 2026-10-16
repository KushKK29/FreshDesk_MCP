"""Mocked-HTTP unit tests for the Asana adapter (M1 Task 10, PRD N5). No
Mongo needed — the adapter's call() takes a Connection object directly, no
database round trip.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import httpx

import adapters
from adapters.asana import AsanaAdapter

adapter = AsanaAdapter()


def _conn() -> adapters.Connection:
    return adapters.Connection(user_id="u1", provider="asana", auth_type="oauth2", credentials={"access_token": "fake-token"})


def _mock_transport(json_body: dict, status_code: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=json_body)
    return httpx.MockTransport(handler)


def _patch_httpx_request(monkey_json: dict, status_code: int = 200):
    orig_request = httpx.request
    def fake_request(method, url, **kwargs):
        return httpx.Response(status_code, json=monkey_json, request=httpx.Request(method, url))
    httpx.request = fake_request
    return orig_request


def test_tools_are_unique_and_namespaced():
    names = [t.name for t in adapter.tools()]
    assert len(names) == len(set(names))
    assert all(n.startswith("asana_") for n in names)


def test_list_tasks_dispatches_to_generic_crud():
    orig = _patch_httpx_request({"data": [{"gid": "1", "name": "Task 1"}]})
    try:
        result = adapter.call("asana_list_tasks", {"workspace": "w1"}, _conn())
    finally:
        httpx.request = orig
    assert result == {"results": [{"gid": "1", "name": "Task 1"}]}


def test_get_task():
    orig = _patch_httpx_request({"data": {"gid": "1", "name": "Task 1"}})
    try:
        result = adapter.call("asana_get_task", {"gid": "1"}, _conn())
    finally:
        httpx.request = orig
    assert result == {"gid": "1", "name": "Task 1"}


def test_create_task_not_read_only():
    spec = next(t for t in adapter.tools() if t.name == "asana_create_task")
    assert spec.read_only_hint is False
    orig = _patch_httpx_request({"data": {"gid": "2", "name": "New task"}})
    try:
        result = adapter.call("asana_create_task", {"data": {"name": "New task"}}, _conn())
    finally:
        httpx.request = orig
    assert result["gid"] == "2"


def test_delete_task_is_destructive():
    spec = next(t for t in adapter.tools() if t.name == "asana_delete_task")
    assert spec.destructive_hint is True
    assert spec.read_only_hint is False
    orig = _patch_httpx_request({})
    try:
        result = adapter.call("asana_delete_task", {"gid": "1"}, _conn())
    finally:
        httpx.request = orig
    assert result == {"status": "deleted"}


def test_complete_task_special_action():
    orig = _patch_httpx_request({"data": {"gid": "1", "completed": True}})
    try:
        result = adapter.call("asana_complete_task", {"task_gid": "1"}, _conn())
    finally:
        httpx.request = orig
    assert result["completed"] is True


def test_add_comment_special_action():
    orig = _patch_httpx_request({"data": {"gid": "s1", "text": "hello"}})
    try:
        result = adapter.call("asana_add_comment", {"task_gid": "1", "text": "hello"}, _conn())
    finally:
        httpx.request = orig
    assert result["text"] == "hello"


def test_unknown_tool_raises():
    try:
        adapter.call("asana_nonexistent_tool", {}, _conn())
        assert False, "should have raised"
    except ValueError:
        pass


def test_no_tool_requires_confirm():
    """Session scope decision: Asana has no destructive bulk/send action, so
    nothing on this adapter should be PC-gated."""
    assert all(not t.requires_confirm for t in adapter.tools())


if __name__ == "__main__":
    test_tools_are_unique_and_namespaced()
    test_list_tasks_dispatches_to_generic_crud()
    test_get_task()
    test_create_task_not_read_only()
    test_delete_task_is_destructive()
    test_complete_task_special_action()
    test_add_comment_special_action()
    test_unknown_tool_raises()
    test_no_tool_requires_confirm()
    print("all checks passed")
