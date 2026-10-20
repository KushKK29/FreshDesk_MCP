"""Shared coverage for the 429-retry and >=400-error branches that all six
*_client.py provider HTTP wrappers implement identically (hubspot_client,
salesforce_client, asana_client, outreach_client, salesloft_client,
zoho_client — freshdesk_client.py already has this in test_freshdesk_client.py,
plus its own extra rate-limit-header backoff). Each module's _request() is a
direct copy-paste of the same shape, so one bug there breaks all six
identically — this was previously 0% covered across all of them.
"""
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import hubspot_client as hs  # noqa: E402
import salesforce_client as sf  # noqa: E402
import asana_client as az  # noqa: E402
import outreach_client as oc  # noqa: E402
import salesloft_client as sl  # noqa: E402
import zoho_client as zh  # noqa: E402


def _fake_resp(status_code, headers=None, body=None):
    resp = mock.Mock()
    resp.status_code = status_code
    resp.headers = headers or {}
    resp.text = "error body"
    resp.json.return_value = body if body is not None else {}
    return resp


# (module, error_class, call() -> result, extra fixed positional args before
# access_token in _request's own signature — salesforce/zoho take one more)
CASES = [
    (hs, hs.HubSpotError, lambda: hs.list_objects("tok", "contacts")),
    (sf, sf.SalesforceError, lambda: sf._request("GET", "https://instance.my.salesforce.com", "tok", "/sobjects")),
    (az, az.AsanaError, lambda: az._request("GET", "tok", "/tasks")),
    (oc, oc.OutreachError, lambda: oc._request("GET", "tok", "/prospects")),
    (sl, sl.SalesLoftError, lambda: sl._request("GET", "tok", "/people")),
    (zh, zh.ZohoError, lambda: zh._request("GET", "www.zohoapis.com", "tok", "/Leads")),
]


@pytest.mark.parametrize("module,error_cls,call", CASES, ids=[c[0].__name__ for c in CASES])
def test_retries_once_on_429_honoring_retry_after(module, error_cls, call):
    calls = []

    def fake_request(method, url, **kwargs):
        if len(calls) == 0:
            calls.append(1)
            return _fake_resp(429, headers={"Retry-After": "0"})
        calls.append(1)
        return _fake_resp(200, body={"results": []})

    with mock.patch("httpx.request", side_effect=fake_request), mock.patch("time.sleep") as sleep:
        call()
    assert len(calls) == 2
    sleep.assert_called_once_with(0)


@pytest.mark.parametrize("module,error_cls,call", CASES, ids=[c[0].__name__ for c in CASES])
def test_raises_providers_own_error_on_4xx_after_no_retry(module, error_cls, call):
    with mock.patch("httpx.request", return_value=_fake_resp(404)):
        with pytest.raises(error_cls):
            call()


@pytest.mark.parametrize("module,error_cls,call", CASES, ids=[c[0].__name__ for c in CASES])
def test_raises_providers_own_error_on_5xx(module, error_cls, call):
    with mock.patch("httpx.request", return_value=_fake_resp(500)):
        with pytest.raises(error_cls):
            call()


@pytest.mark.parametrize("module,error_cls,call", CASES, ids=[c[0].__name__ for c in CASES])
def test_still_raises_if_second_attempt_after_429_also_errors(module, error_cls, call):
    """The retry is exactly one attempt, not a loop — a second 429 (or any
    other >=400) after the retry must surface as an error, not retry forever
    or silently swallow it."""
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            return _fake_resp(429, headers={"Retry-After": "0"})
        return _fake_resp(429, headers={"Retry-After": "0"})

    with mock.patch("httpx.request", side_effect=fake_request), mock.patch("time.sleep"):
        with pytest.raises(error_cls):
            call()
    assert len(calls) == 2, "must not retry more than once"


if __name__ == "__main__":
    for module, error_cls, call in CASES:
        test_retries_once_on_429_honoring_retry_after(module, error_cls, call)
        test_raises_providers_own_error_on_4xx_after_no_retry(module, error_cls, call)
        test_raises_providers_own_error_on_5xx(module, error_cls, call)
        test_still_raises_if_second_attempt_after_429_also_errors(module, error_cls, call)
    print("all checks passed")
