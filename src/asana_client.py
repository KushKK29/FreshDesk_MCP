"""Minimal Asana REST client: same shape as freshdesk_client.py (one retry
on 429 honoring Retry-After, flat functions, _for()-style explicit-credential
calls since this is multi-tenant from day one — no single-tenant env-var
convenience wrappers needed here, Asana has no single-tenant mode in this
connector).
"""
import time

import httpx

BASE_URL = "https://app.asana.com/api/1.0"


class AsanaError(Exception):
    pass


def _request(method: str, access_token: str, path: str, **kwargs) -> httpx.Response:
    headers = {"Authorization": f"Bearer {access_token}"}
    resp = httpx.request(method, f"{BASE_URL}{path}", headers=headers, timeout=30, **kwargs)
    if resp.status_code == 429:
        retry_after = int(resp.headers.get("Retry-After", "5"))
        time.sleep(retry_after)
        resp = httpx.request(method, f"{BASE_URL}{path}", headers=headers, timeout=30, **kwargs)

    if resp.status_code >= 400:
        raise AsanaError(f"{method} {path} -> {resp.status_code}: {resp.text}")
    return resp


def list_resource(access_token: str, resource: str, params: dict | None = None) -> list[dict]:
    return _request("GET", access_token, f"/{resource}", params=params or {}).json().get("data", [])


def get_resource(access_token: str, resource: str, gid: str, opt_fields: str | None = None) -> dict:
    params = {"opt_fields": opt_fields} if opt_fields else {}
    return _request("GET", access_token, f"/{resource}/{gid}", params=params).json().get("data", {})


def create_resource(access_token: str, resource: str, data: dict) -> dict:
    return _request("POST", access_token, f"/{resource}", json={"data": data}).json().get("data", {})


def update_resource(access_token: str, resource: str, gid: str, data: dict) -> dict:
    return _request("PUT", access_token, f"/{resource}/{gid}", json={"data": data}).json().get("data", {})


def delete_resource(access_token: str, resource: str, gid: str) -> None:
    _request("DELETE", access_token, f"/{resource}/{gid}")


def search_tasks(access_token: str, workspace_gid: str, params: dict) -> list[dict]:
    return _request("GET", access_token, f"/workspaces/{workspace_gid}/tasks/search", params=params).json().get("data", [])


def complete_task(access_token: str, task_gid: str) -> dict:
    return update_resource(access_token, "tasks", task_gid, {"completed": True})


def set_parent_task(access_token: str, task_gid: str, parent_gid: str) -> dict:
    return _request("POST", access_token, f"/tasks/{task_gid}/setParent", json={"data": {"parent": parent_gid}}).json().get("data", {})


def add_followers(access_token: str, task_gid: str, follower_gids: list[str]) -> dict:
    return _request("POST", access_token, f"/tasks/{task_gid}/addFollowers", json={"data": {"followers": follower_gids}}).json().get("data", {})


def remove_followers(access_token: str, task_gid: str, follower_gids: list[str]) -> dict:
    return _request("POST", access_token, f"/tasks/{task_gid}/removeFollowers", json={"data": {"followers": follower_gids}}).json().get("data", {})


def add_tag(access_token: str, task_gid: str, tag_gid: str) -> dict:
    return _request("POST", access_token, f"/tasks/{task_gid}/addTag", json={"data": {"tag": tag_gid}}).json().get("data", {})


def remove_tag(access_token: str, task_gid: str, tag_gid: str) -> dict:
    return _request("POST", access_token, f"/tasks/{task_gid}/removeTag", json={"data": {"tag": tag_gid}}).json().get("data", {})


def add_dependencies(access_token: str, task_gid: str, dependency_gids: list[str]) -> dict:
    return _request("POST", access_token, f"/tasks/{task_gid}/addDependencies", json={"data": {"dependencies": dependency_gids}}).json().get("data", {})


def duplicate_project(access_token: str, project_gid: str, name: str) -> dict:
    return _request("POST", access_token, f"/projects/{project_gid}/duplicate", json={"data": {"name": name}}).json().get("data", {})


def add_comment(access_token: str, task_gid: str, text: str) -> dict:
    return _request("POST", access_token, f"/tasks/{task_gid}/stories", json={"data": {"text": text}}).json().get("data", {})
