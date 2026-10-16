"""Minimal Outreach REST client (API v2, JSON:API). Same shape as the other
provider clients: one retry on 429, flat functions, explicit credentials.
"""
import time

import httpx

BASE_URL = "https://api.outreach.io/api/v2"


class OutreachError(Exception):
    pass


def _request(method: str, access_token: str, path: str, **kwargs) -> httpx.Response:
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/vnd.api+json"}
    resp = httpx.request(method, f"{BASE_URL}{path}", headers=headers, timeout=30, **kwargs)
    if resp.status_code == 429:
        retry_after = int(resp.headers.get("Retry-After", "5"))
        time.sleep(retry_after)
        resp = httpx.request(method, f"{BASE_URL}{path}", headers=headers, timeout=30, **kwargs)

    if resp.status_code >= 400:
        raise OutreachError(f"{method} {path} -> {resp.status_code}: {resp.text}")
    return resp


def _jsonapi_payload(resource_type: str, attributes: dict, relationships: dict | None = None) -> dict:
    data = {"type": resource_type, "attributes": attributes}
    if relationships:
        data["relationships"] = relationships
    return {"data": data}


def list_resource(access_token: str, resource: str) -> list[dict]:
    return _request("GET", access_token, f"/{resource}").json().get("data", [])


def get_resource(access_token: str, resource: str, resource_id: str) -> dict:
    return _request("GET", access_token, f"/{resource}/{resource_id}").json().get("data", {})


def create_resource(access_token: str, resource: str, resource_type: str, attributes: dict) -> dict:
    return _request("POST", access_token, f"/{resource}", json=_jsonapi_payload(resource_type, attributes)).json().get("data", {})


def update_resource(access_token: str, resource: str, resource_type: str, resource_id: str, attributes: dict) -> dict:
    payload = _jsonapi_payload(resource_type, attributes)
    payload["data"]["id"] = resource_id
    return _request("PATCH", access_token, f"/{resource}/{resource_id}", json=payload).json().get("data", {})


def delete_resource(access_token: str, resource: str, resource_id: str) -> None:
    _request("DELETE", access_token, f"/{resource}/{resource_id}")


def enroll_in_sequence(access_token: str, prospect_id: str, sequence_id: str, mailbox_id: str) -> dict:
    relationships = {
        "prospect": {"data": {"type": "prospect", "id": prospect_id}},
        "sequence": {"data": {"type": "sequence", "id": sequence_id}},
        "mailbox": {"data": {"type": "mailbox", "id": mailbox_id}},
    }
    return _request("POST", access_token, "/sequenceStates", json=_jsonapi_payload("sequenceState", {}, relationships)).json().get("data", {})


def _sequence_state_action(access_token: str, sequence_state_id: str, action: str) -> dict:
    payload = {"data": {"type": "sequenceState", "id": sequence_state_id, "attributes": {}, "meta": {action: True}}}
    return _request("PATCH", access_token, f"/sequenceStates/{sequence_state_id}", json=payload).json().get("data", {})


def pause_sequence(access_token: str, sequence_state_id: str) -> dict:
    return _sequence_state_action(access_token, sequence_state_id, "pause")


def resume_sequence(access_token: str, sequence_state_id: str) -> dict:
    return _sequence_state_action(access_token, sequence_state_id, "resume")


def finish_sequence(access_token: str, sequence_state_id: str) -> dict:
    return _sequence_state_action(access_token, sequence_state_id, "finish")


def log_call(access_token: str, prospect_id: str, direction: str, outcome: str, duration: int) -> dict:
    attributes = {"direction": direction, "outcome": outcome, "duration": duration}
    relationships = {"prospect": {"data": {"type": "prospect", "id": prospect_id}}}
    return _request("POST", access_token, "/calls", json=_jsonapi_payload("call", attributes, relationships)).json().get("data", {})


def request_compliance_deletion(access_token: str, email: str) -> dict:
    return _request("POST", access_token, "/complianceRequests", json=_jsonapi_payload("complianceRequest", {"email": email, "requestType": "deletion"})).json().get("data", {})
