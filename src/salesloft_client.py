"""Minimal SalesLoft REST client (API v2). Same shape as the other provider
clients. Note (plan flagged): SalesLoft's native send-email endpoint wasn't
confirmed against primary docs during scoping — this client has no send_email
function; verify against developers.salesloft.com before adding one.
"""
import time

import httpx

BASE_URL = "https://api.salesloft.com/v2"


class SalesLoftError(Exception):
    pass


def _request(method: str, access_token: str, path: str, **kwargs) -> httpx.Response:
    headers = {"Authorization": f"Bearer {access_token}"}
    resp = httpx.request(method, f"{BASE_URL}{path}", headers=headers, timeout=30, **kwargs)
    if resp.status_code == 429:
        retry_after = int(resp.headers.get("Retry-After", "5"))
        time.sleep(retry_after)
        resp = httpx.request(method, f"{BASE_URL}{path}", headers=headers, timeout=30, **kwargs)

    if resp.status_code >= 400:
        raise SalesLoftError(f"{method} {path} -> {resp.status_code}: {resp.text}")
    return resp


def list_resource(access_token: str, resource: str, params: dict | None = None) -> list[dict]:
    return _request("GET", access_token, f"/{resource}.json", params=params or {}).json().get("data", [])


def get_resource(access_token: str, resource: str, resource_id: str) -> dict:
    return _request("GET", access_token, f"/{resource}/{resource_id}.json").json().get("data", {})


def create_resource(access_token: str, resource: str, data: dict) -> dict:
    return _request("POST", access_token, f"/{resource}.json", json={resource.rstrip("s"): data}).json().get("data", {})


def update_resource(access_token: str, resource: str, resource_id: str, data: dict) -> dict:
    return _request("PUT", access_token, f"/{resource}/{resource_id}.json", json={resource.rstrip("s"): data}).json().get("data", {})


def delete_resource(access_token: str, resource: str, resource_id: str) -> None:
    _request("DELETE", access_token, f"/{resource}/{resource_id}.json")


def enroll_in_cadence(access_token: str, person_id: str, cadence_id: str) -> dict:
    return _request("POST", access_token, "/cadence_memberships.json", json={"cadence_membership": {"person_id": person_id, "cadence_id": cadence_id}}).json().get("data", {})


def pause_membership(access_token: str, membership_id: str) -> dict:
    return _request("PUT", access_token, f"/cadence_memberships/{membership_id}.json", json={"cadence_membership": {"paused": True}}).json().get("data", {})


def resume_membership(access_token: str, membership_id: str) -> dict:
    return _request("PUT", access_token, f"/cadence_memberships/{membership_id}.json", json={"cadence_membership": {"paused": False}}).json().get("data", {})


def remove_membership(access_token: str, membership_id: str) -> None:
    _request("DELETE", access_token, f"/cadence_memberships/{membership_id}.json")
