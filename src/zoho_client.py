"""Minimal Zoho CRM REST client. Host is resolved from a fixed allowlist of
known data centers by the adapter (never from user input — SSRF guard,
PRD §7.5); this module just takes the already-resolved host.
"""
import time

import httpx

API_VERSION = "v8"


class ZohoError(Exception):
    pass


def _request(method: str, host: str, access_token: str, path: str, **kwargs) -> httpx.Response:
    headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}
    base = f"https://{host}/crm/{API_VERSION}"
    resp = httpx.request(method, f"{base}{path}", headers=headers, timeout=30, **kwargs)
    if resp.status_code == 429:
        retry_after = int(resp.headers.get("Retry-After", "5"))
        time.sleep(retry_after)
        resp = httpx.request(method, f"{base}{path}", headers=headers, timeout=30, **kwargs)

    if resp.status_code >= 400:
        raise ZohoError(f"{method} {path} -> {resp.status_code}: {resp.text}")
    return resp


def list_records(host: str, access_token: str, module: str) -> list[dict]:
    # Zoho's list endpoint 400s (MANDATORY_NOT_FOUND) without a `fields`
    # param. "id" is valid on every module, so it's always safe — but it
    # means callers only get back {id: ...} per record, not full field data.
    # ponytail: fine for verify() (only needs proof the token/module work),
    # NOT fine for a real "list leads" tool call — add per-module field lists
    # (or switch those tools to query_coql, which already takes an explicit
    # SELECT) once we can test against a live Zoho org and see what each
    # module's fields are actually called.
    return _request("GET", host, access_token, f"/{module}", params={"fields": "id"}).json().get("data", [])


def get_record(host: str, access_token: str, module: str, record_id: str) -> dict:
    data = _request("GET", host, access_token, f"/{module}/{record_id}").json().get("data", [])
    return data[0] if data else {}


def create_record(host: str, access_token: str, module: str, data: dict) -> dict:
    return _request("POST", host, access_token, f"/{module}", json={"data": [data]}).json()


def update_record(host: str, access_token: str, module: str, record_id: str, data: dict) -> dict:
    return _request("PUT", host, access_token, f"/{module}/{record_id}", json={"data": [data]}).json()


def delete_record(host: str, access_token: str, module: str, record_id: str) -> None:
    _request("DELETE", host, access_token, f"/{module}/{record_id}")


def upsert_record(host: str, access_token: str, module: str, data: dict) -> dict:
    return _request("POST", host, access_token, f"/{module}/upsert", json={"data": [data]}).json()


def query_coql(host: str, access_token: str, select_query: str) -> list[dict]:
    return _request("POST", host, access_token, "/coql", json={"select_query": select_query}).json().get("data", [])


def convert_lead(host: str, access_token: str, lead_id: str) -> dict:
    return _request("POST", host, access_token, f"/Leads/{lead_id}/actions/convert", json={"data": [{}]}).json()


def mass_convert_leads(host: str, access_token: str, lead_ids: list[str]) -> dict:
    return _request("POST", host, access_token, "/Leads/actions/mass_convert", json={"data": [{"id": i} for i in lead_ids]}).json()


def get_related_records(host: str, access_token: str, module: str, record_id: str, related_list: str) -> list[dict]:
    return _request("GET", host, access_token, f"/{module}/{record_id}/{related_list}").json().get("data", [])


def bulk_read(host: str, access_token: str, module: str) -> dict:
    return _request("POST", host, access_token, "/bulk/v8/read", json={"query": {"module": {"api_name": module}}}).json()


def bulk_write(host: str, access_token: str, module: str, operation: str, records: list[dict]) -> dict:
    return _request("POST", host, access_token, "/bulk/v8/write", json={"operation": operation, "resource": [{"module": {"api_name": module}}]}).json()
