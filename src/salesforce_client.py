"""Minimal Salesforce REST client: same shape as the other provider clients.
instance_url comes from the connection's stored credentials (set from the
OAuth token response at connect time, never from user input — SSRF guard,
PRD §7.5), not computed here.
"""
import time

import httpx

API_VERSION = "v60.0"


class SalesforceError(Exception):
    pass


def _request(method: str, instance_url: str, access_token: str, path: str, **kwargs) -> httpx.Response:
    headers = {"Authorization": f"Bearer {access_token}"}
    base = f"{instance_url}/services/data/{API_VERSION}"
    resp = httpx.request(method, f"{base}{path}", headers=headers, timeout=30, **kwargs)
    if resp.status_code == 429:
        retry_after = int(resp.headers.get("Retry-After", "5"))
        time.sleep(retry_after)
        resp = httpx.request(method, f"{base}{path}", headers=headers, timeout=30, **kwargs)

    if resp.status_code >= 400:
        raise SalesforceError(f"{method} {path} -> {resp.status_code}: {resp.text}")
    return resp


def query_soql(instance_url: str, access_token: str, soql: str) -> list[dict]:
    return _request("GET", instance_url, access_token, "/query", params={"q": soql}).json().get("records", [])


def search_sosl(instance_url: str, access_token: str, sosl: str) -> list[dict]:
    return _request("GET", instance_url, access_token, "/search", params={"q": sosl}).json().get("searchRecords", [])


def list_objects(instance_url: str, access_token: str, sobject: str) -> list[dict]:
    return query_soql(instance_url, access_token, f"SELECT Id FROM {sobject}")


def get_object(instance_url: str, access_token: str, sobject: str, record_id: str) -> dict:
    return _request("GET", instance_url, access_token, f"/sobjects/{sobject}/{record_id}").json()


def create_object(instance_url: str, access_token: str, sobject: str, data: dict) -> dict:
    return _request("POST", instance_url, access_token, f"/sobjects/{sobject}", json=data).json()


def update_object(instance_url: str, access_token: str, sobject: str, record_id: str, data: dict) -> None:
    _request("PATCH", instance_url, access_token, f"/sobjects/{sobject}/{record_id}", json=data)


def delete_object(instance_url: str, access_token: str, sobject: str, record_id: str) -> None:
    _request("DELETE", instance_url, access_token, f"/sobjects/{sobject}/{record_id}")


def convert_lead(instance_url: str, access_token: str, lead_id: str) -> dict:
    return _request("POST", instance_url, access_token, f"/sobjects/Lead/{lead_id}/convert", json={}).json()


def merge_records(instance_url: str, access_token: str, sobject: str, master_id: str, merge_ids: list[str]) -> dict:
    return _request("POST", instance_url, access_token, f"/sobjects/{sobject}/{master_id}/merge", json={"mergeIds": merge_ids}).json()


def bulk_ingest(instance_url: str, access_token: str, sobject: str, operation: str, records: list[dict]) -> dict:
    """Bulk API 2.0 job creation — a full implementation polls the job and
    uploads CSV; this is the job-creation call, sufficient for the adapter
    contract's single call/response shape."""
    job = _request("POST", instance_url, access_token, "/jobs/ingest", json={"object": sobject, "operation": operation}).json()
    return job


def send_email(instance_url: str, access_token: str, to_address: str, subject: str, body: str) -> dict:
    payload = {"inputs": [{"emailSimple": {"textBody": body, "subject": subject, "emailAddresses": {"to": [to_address]}}}]}
    return _request("POST", instance_url, access_token, "/actions/standard/emailSimple", json=payload).json()
