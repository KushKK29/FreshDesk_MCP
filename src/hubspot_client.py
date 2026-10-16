"""Minimal HubSpot REST client: same shape as freshdesk_client.py/
asana_client.py (one retry on 429 honoring Retry-After, flat functions,
explicit-credential calls — multi-tenant from day one).
"""
import time

import httpx

BASE_URL = "https://api.hubapi.com"


class HubSpotError(Exception):
    pass


def _request(method: str, access_token: str, path: str, **kwargs) -> httpx.Response:
    headers = {"Authorization": f"Bearer {access_token}"}
    resp = httpx.request(method, f"{BASE_URL}{path}", headers=headers, timeout=30, **kwargs)
    if resp.status_code == 429:
        retry_after = int(resp.headers.get("Retry-After", "5"))
        time.sleep(retry_after)
        resp = httpx.request(method, f"{BASE_URL}{path}", headers=headers, timeout=30, **kwargs)

    if resp.status_code >= 400:
        raise HubSpotError(f"{method} {path} -> {resp.status_code}: {resp.text}")
    return resp


def list_objects(access_token: str, object_type: str, limit: int = 100) -> list[dict]:
    return _request("GET", access_token, f"/crm/v3/objects/{object_type}", params={"limit": limit}).json().get("results", [])


def get_object(access_token: str, object_type: str, object_id: str) -> dict:
    return _request("GET", access_token, f"/crm/v3/objects/{object_type}/{object_id}").json()


def create_object(access_token: str, object_type: str, properties: dict) -> dict:
    return _request("POST", access_token, f"/crm/v3/objects/{object_type}", json={"properties": properties}).json()


def update_object(access_token: str, object_type: str, object_id: str, properties: dict) -> dict:
    return _request("PATCH", access_token, f"/crm/v3/objects/{object_type}/{object_id}", json={"properties": properties}).json()


def archive_object(access_token: str, object_type: str, object_id: str) -> None:
    _request("DELETE", access_token, f"/crm/v3/objects/{object_type}/{object_id}")


def search_objects(access_token: str, object_type: str, query: dict) -> list[dict]:
    return _request("POST", access_token, f"/crm/v3/objects/{object_type}/search", json=query).json().get("results", [])


def associate_objects(access_token: str, from_type: str, from_id: str, to_type: str, to_id: str) -> dict:
    return _request("PUT", access_token, f"/crm/v3/objects/{from_type}/{from_id}/associations/default/{to_type}/{to_id}").json()


def get_associations(access_token: str, from_type: str, from_id: str, to_type: str) -> list[dict]:
    return _request("GET", access_token, f"/crm/v4/objects/{from_type}/{from_id}/associations/{to_type}").json().get("results", [])


def enroll_in_workflow(access_token: str, workflow_id: str, contact_email: str) -> None:
    _request("POST", access_token, f"/automation/v2/workflows/{workflow_id}/enrollments/contacts/{contact_email}")


def batch_create(access_token: str, object_type: str, inputs: list[dict]) -> dict:
    return _request("POST", access_token, f"/crm/v3/objects/{object_type}/batch/create", json={"inputs": inputs}).json()


def batch_update(access_token: str, object_type: str, inputs: list[dict]) -> dict:
    return _request("POST", access_token, f"/crm/v3/objects/{object_type}/batch/update", json={"inputs": inputs}).json()


def batch_archive(access_token: str, object_type: str, ids: list[str]) -> None:
    _request("POST", access_token, f"/crm/v3/objects/{object_type}/batch/archive", json={"inputs": [{"id": i} for i in ids]})
