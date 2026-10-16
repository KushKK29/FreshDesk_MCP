"""Asana adapter: Task/Subtask/Project/Section/Tag/Story/Attachment/
Portfolio/CustomField CRUD plus the special actions in the connector-hub
plan's tool surface table. auth_modes includes both OAUTH2 and API_KEY since
a personal access token also works for internal testing (PRD M1 note) and
shouldn't be blocked on the Asana app's own registration/approval.

No preview+confirm gate on any Asana tool — Asana's API has no destructive
bulk/send action (deletes are single-object, scoped to what the user already
has access to), so nothing here meets the PC bar (delete/send/bulk) the
session's scope decision set.
"""
try:
    import asana_client as az
    from adapters import AuthMode, Connection, ToolSpec, register
except ImportError:
    from src import asana_client as az
    from src.adapters import AuthMode, Connection, ToolSpec, register

_CRUD_OBJECTS = {
    "task": "tasks",
    "project": "projects",
    "section": "sections",
    "tag": "tags",
    "portfolio": "portfolios",
    "custom_field": "custom_fields",
}


def _object_schema() -> dict:
    return {"type": "object", "additionalProperties": True}


def _list_tool_name(obj: str) -> str:
    return f"asana_list_{obj}s"


def _crud_tools() -> list[ToolSpec]:
    tools = []
    for obj in _CRUD_OBJECTS:
        label = obj.replace("_", " ")
        tools.append(ToolSpec(
            name=_list_tool_name(obj),
            description=f"List Asana {label}s.",
            input_schema={"type": "object", "properties": {"workspace": {"type": "string"}, "project": {"type": "string"}}},
        ))
        tools.append(ToolSpec(
            name=f"asana_get_{obj}",
            description=f"Get a single Asana {label} by gid.",
            input_schema={"type": "object", "properties": {"gid": {"type": "string"}}, "required": ["gid"]},
        ))
        tools.append(ToolSpec(
            name=f"asana_create_{obj}",
            description=f"Create an Asana {label}.",
            input_schema={"type": "object", "properties": {"data": _object_schema()}, "required": ["data"]},
            read_only_hint=False, idempotent_hint=False,
        ))
        tools.append(ToolSpec(
            name=f"asana_update_{obj}",
            description=f"Update an Asana {label} by gid.",
            input_schema={"type": "object", "properties": {"gid": {"type": "string"}, "data": _object_schema()}, "required": ["gid", "data"]},
            read_only_hint=False,
        ))
        tools.append(ToolSpec(
            name=f"asana_delete_{obj}",
            description=f"Delete an Asana {label} by gid.",
            input_schema={"type": "object", "properties": {"gid": {"type": "string"}}, "required": ["gid"]},
            read_only_hint=False, destructive_hint=True,
        ))
    return tools


class AsanaAdapter:
    key = "asana"
    display_name = "Asana"
    auth_modes = {AuthMode.OAUTH2, AuthMode.API_KEY}
    scopes = ["default"]

    def tools(self) -> list[ToolSpec]:
        specs = [
            ToolSpec(
                name="asana_list_subtasks",
                description="List subtasks of an Asana task.",
                input_schema={"type": "object", "properties": {"task_gid": {"type": "string"}}, "required": ["task_gid"]},
            ),
            ToolSpec(
                name="asana_search_tasks",
                description="Search Asana tasks in a workspace by text, assignee, project, completion, etc.",
                input_schema={"type": "object", "properties": {
                    "workspace_gid": {"type": "string"}, "text": {"type": "string"}, "completed": {"type": "boolean"},
                }, "required": ["workspace_gid"]},
            ),
            ToolSpec(
                name="asana_list_attachments",
                description="List attachments on an Asana task.",
                input_schema={"type": "object", "properties": {"task_gid": {"type": "string"}}, "required": ["task_gid"]},
            ),
            ToolSpec(
                name="asana_complete_task",
                description="Mark an Asana task complete.",
                input_schema={"type": "object", "properties": {"task_gid": {"type": "string"}}, "required": ["task_gid"]},
                read_only_hint=False,
            ),
            ToolSpec(
                name="asana_set_parent_task",
                description="Set the parent task of an Asana task (make it a subtask).",
                input_schema={"type": "object", "properties": {"task_gid": {"type": "string"}, "parent_gid": {"type": "string"}}, "required": ["task_gid", "parent_gid"]},
                read_only_hint=False,
            ),
            ToolSpec(
                name="asana_add_followers",
                description="Add followers to an Asana task.",
                input_schema={"type": "object", "properties": {"task_gid": {"type": "string"}, "follower_gids": {"type": "array", "items": {"type": "string"}}}, "required": ["task_gid", "follower_gids"]},
                read_only_hint=False,
            ),
            ToolSpec(
                name="asana_remove_followers",
                description="Remove followers from an Asana task.",
                input_schema={"type": "object", "properties": {"task_gid": {"type": "string"}, "follower_gids": {"type": "array", "items": {"type": "string"}}}, "required": ["task_gid", "follower_gids"]},
                read_only_hint=False,
            ),
            ToolSpec(
                name="asana_add_tag",
                description="Add a tag to an Asana task.",
                input_schema={"type": "object", "properties": {"task_gid": {"type": "string"}, "tag_gid": {"type": "string"}}, "required": ["task_gid", "tag_gid"]},
                read_only_hint=False,
            ),
            ToolSpec(
                name="asana_remove_tag",
                description="Remove a tag from an Asana task.",
                input_schema={"type": "object", "properties": {"task_gid": {"type": "string"}, "tag_gid": {"type": "string"}}, "required": ["task_gid", "tag_gid"]},
                read_only_hint=False,
            ),
            ToolSpec(
                name="asana_add_dependencies",
                description="Add dependency tasks to an Asana task.",
                input_schema={"type": "object", "properties": {"task_gid": {"type": "string"}, "dependency_gids": {"type": "array", "items": {"type": "string"}}}, "required": ["task_gid", "dependency_gids"]},
                read_only_hint=False,
            ),
            ToolSpec(
                name="asana_duplicate_project",
                description="Duplicate an Asana project under a new name.",
                input_schema={"type": "object", "properties": {"project_gid": {"type": "string"}, "name": {"type": "string"}}, "required": ["project_gid", "name"]},
                read_only_hint=False, idempotent_hint=False,
            ),
            ToolSpec(
                name="asana_add_comment",
                description="Add a comment (story) to an Asana task.",
                input_schema={"type": "object", "properties": {"task_gid": {"type": "string"}, "text": {"type": "string"}}, "required": ["task_gid", "text"]},
                read_only_hint=False, idempotent_hint=False,
            ),
        ]
        specs.extend(_crud_tools())
        return specs

    def call(self, tool: str, args: dict, conn: Connection) -> dict:
        token = conn.credentials["access_token"]

        if tool == "asana_list_subtasks":
            return {"results": az.list_resource(token, f"tasks/{args['task_gid']}/subtasks")}
        if tool == "asana_search_tasks":
            params = {k: v for k, v in args.items() if k != "workspace_gid"}
            return {"results": az.search_tasks(token, args["workspace_gid"], params)}
        if tool == "asana_list_attachments":
            return {"results": az.list_resource(token, "attachments", {"parent": args["task_gid"]})}
        if tool == "asana_complete_task":
            return az.complete_task(token, args["task_gid"])
        if tool == "asana_set_parent_task":
            return az.set_parent_task(token, args["task_gid"], args["parent_gid"])
        if tool == "asana_add_followers":
            return az.add_followers(token, args["task_gid"], args["follower_gids"])
        if tool == "asana_remove_followers":
            return az.remove_followers(token, args["task_gid"], args["follower_gids"])
        if tool == "asana_add_tag":
            return az.add_tag(token, args["task_gid"], args["tag_gid"])
        if tool == "asana_remove_tag":
            return az.remove_tag(token, args["task_gid"], args["tag_gid"])
        if tool == "asana_add_dependencies":
            return az.add_dependencies(token, args["task_gid"], args["dependency_gids"])
        if tool == "asana_duplicate_project":
            return az.duplicate_project(token, args["project_gid"], args["name"])
        if tool == "asana_add_comment":
            return az.add_comment(token, args["task_gid"], args["text"])

        for obj, path in _CRUD_OBJECTS.items():
            if tool == _list_tool_name(obj):
                params = {k: v for k, v in args.items() if v is not None}
                return {"results": az.list_resource(token, path, params)}
            if tool == f"asana_get_{obj}":
                return az.get_resource(token, path, args["gid"])
            if tool == f"asana_create_{obj}":
                return az.create_resource(token, path, args["data"])
            if tool == f"asana_update_{obj}":
                return az.update_resource(token, path, args["gid"], args["data"])
            if tool == f"asana_delete_{obj}":
                az.delete_resource(token, path, args["gid"])
                return {"status": "deleted"}

        raise ValueError(f"unknown tool: {tool}")

    def refresh(self, conn: Connection) -> Connection:
        import time
        import httpx
        import os
        resp = httpx.post("https://app.asana.com/-/oauth_token", data={
            "grant_type": "refresh_token",
            "client_id": os.environ.get("ASANA_CLIENT_ID"),
            "client_secret": os.environ.get("ASANA_CLIENT_SECRET"),
            "refresh_token": conn.credentials["refresh_token"],
        }, timeout=15)
        if resp.status_code >= 400:
            raise az.AsanaError(f"refresh failed: {resp.status_code}: {resp.text}")
        body = resp.json()
        conn.credentials["access_token"] = body["access_token"]
        conn.expires_at = time.time() + body["expires_in"]
        return conn

    def verify(self, conn: Connection) -> None:
        az.list_resource(conn.credentials["access_token"], "users/me")


register(AsanaAdapter())
