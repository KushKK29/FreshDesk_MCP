"""Smallest check that fails if retry/pagination/auth logic breaks."""
import base64
import os
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

os.environ.setdefault("FRESHDESK_DOMAIN", "test.freshdesk.com")
os.environ.setdefault("FRESHDESK_API_KEY", "fake_key")

import freshdesk_client as fd


def test_auth_is_basic_key_and_x():
    captured = {}

    def fake_request(method, url, auth=None, **kwargs):
        captured["auth"] = auth
        resp = mock.Mock()
        resp.status_code = 200
        resp.headers = {}
        resp.json.return_value = []
        return resp

    with mock.patch("httpx.request", side_effect=fake_request):
        fd.list_tickets()
    assert captured["auth"] == ("fake_key", "X")


def test_retry_on_429_honors_retry_after():
    calls = []

    def fake_request(method, url, auth=None, **kwargs):
        resp = mock.Mock()
        if len(calls) == 0:
            resp.status_code = 429
            resp.headers = {"Retry-After": "0"}
        else:
            resp.status_code = 200
            resp.headers = {}
            resp.json.return_value = [{"id": 1}]
        calls.append(1)
        return resp

    with mock.patch("httpx.request", side_effect=fake_request), mock.patch("time.sleep"):
        result = fd.list_tickets()
    assert len(calls) == 2
    assert result == [{"id": 1}]


def test_pagination_param_forwarded():
    captured = {}

    def fake_request(method, url, auth=None, params=None, **kwargs):
        captured["params"] = params
        resp = mock.Mock()
        resp.status_code = 200
        resp.headers = {}
        resp.json.return_value = []
        return resp

    with mock.patch("httpx.request", side_effect=fake_request):
        fd.list_tickets(page=3)
    assert captured["params"]["page"] == 3


def test_rate_limit_header_as_float_string_does_not_crash():
    # Freshdesk sometimes sends "49.0" instead of "49" — real response observed in testing.
    def fake_request(method, url, auth=None, **kwargs):
        resp = mock.Mock()
        resp.status_code = 200
        resp.headers = {"X-RateLimit-Remaining": "49.0"}
        resp.json.return_value = []
        return resp

    with mock.patch("httpx.request", side_effect=fake_request):
        fd.list_tickets()  # must not raise


def test_looks_structured_detects_field_syntax():
    assert fd._looks_structured("status:2 AND priority:3") is True
    assert fd._looks_structured("authentication failure") is False


def test_structured_query_goes_to_search_endpoint():
    captured = {}

    def fake_request(method, url, auth=None, params=None, **kwargs):
        captured["url"] = url
        captured["params"] = params
        resp = mock.Mock()
        resp.status_code = 200
        resp.headers = {}
        resp.json.return_value = {"results": [{"id": 1, "subject": "match"}]}
        return resp

    with mock.patch("httpx.request", side_effect=fake_request):
        result = fd.search_tickets("status:2")
    assert "/search/tickets" in captured["url"]
    assert captured["params"]["query"] == '"status:2"'
    assert result == [{"id": 1, "subject": "match"}]


def test_free_text_query_filters_client_side_by_subject():
    # Freshdesk's search endpoint has no free-text/subject field (confirmed live: "Unexpected/
    # invalid field in request" for query=subject:'...') — ChatGPT sent natural language anyway,
    # so free text must fall back to paging list_tickets and matching locally.
    pages = {
        1: [
            {"id": 1, "subject": "Authentication failure", "description_text": ""},
            {"id": 2, "subject": "Billing question", "description_text": ""},
        ],
        2: [],
    }

    def fake_request(method, url, auth=None, params=None, **kwargs):
        resp = mock.Mock()
        resp.status_code = 200
        resp.headers = {}
        resp.json.return_value = pages.get(params["page"], [])
        return resp

    with mock.patch("httpx.request", side_effect=fake_request):
        result = fd.search_tickets("authentication failure")
    assert len(result) == 1
    assert result[0]["id"] == 1


def test_free_text_query_matches_description_too():
    pages = {1: [{"id": 5, "subject": "Something else", "description_text": "mentions AUTHENTICATION issue"}], 2: []}

    def fake_request(method, url, auth=None, params=None, **kwargs):
        resp = mock.Mock()
        resp.status_code = 200
        resp.headers = {}
        resp.json.return_value = pages.get(params["page"], [])
        return resp

    with mock.patch("httpx.request", side_effect=fake_request):
        result = fd.search_tickets("authentication")
    assert len(result) == 1
    assert result[0]["id"] == 5


if __name__ == "__main__":
    test_auth_is_basic_key_and_x()
    test_retry_on_429_honors_retry_after()
    test_pagination_param_forwarded()
    test_rate_limit_header_as_float_string_does_not_crash()
    test_looks_structured_detects_field_syntax()
    test_structured_query_goes_to_search_endpoint()
    test_free_text_query_filters_client_side_by_subject()
    test_free_text_query_matches_description_too()
    print("all checks passed")
