"""SalesLoft adapter: Person/Account CRUD, Cadence/Step/Activity read,
enroll_in_cadence, pause/resume_membership. remove_membership is PC-gated
(delete).
"""
try:
    import salesloft_client as sl
    from adapters import AuthMode, Connection, ToolSpec, register
except ImportError:
    from src import salesloft_client as sl
    from src.adapters import AuthMode, Connection, ToolSpec, register

_CRUD_RESOURCES = {
    "person": "people",
    "account": "accounts",
}

_READ_ONLY_RESOURCES = {
    "cadence": "cadences",
    "step": "steps",
    "email_activity": "activities/emails",
    "call_activity": "activities/calls",
}


def _object_schema() -> dict:
    return {"type": "object", "additionalProperties": True}


class SalesLoftAdapter:
    key = "salesloft"
    display_name = "SalesLoft"
    auth_modes = {AuthMode.OAUTH2}
    scopes: list[str] = []

    def tools(self) -> list[ToolSpec]:
        specs = []
        for obj, path in _CRUD_RESOURCES.items():
            label = obj.replace("_", " ")
            specs.append(ToolSpec(name=f"salesloft_list_{path}", description=f"List SalesLoft {label}s.", input_schema={"type": "object", "properties": {}}))
            specs.append(ToolSpec(name=f"salesloft_get_{obj}", description=f"Get a SalesLoft {label} by id.", input_schema={"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}))
            specs.append(ToolSpec(
                name=f"salesloft_create_{obj}", description=f"Create a SalesLoft {label}.",
                input_schema={"type": "object", "properties": {"data": _object_schema()}, "required": ["data"]},
                read_only_hint=False, idempotent_hint=False,
            ))
            specs.append(ToolSpec(
                name=f"salesloft_update_{obj}", description=f"Update a SalesLoft {label} by id.",
                input_schema={"type": "object", "properties": {"id": {"type": "string"}, "data": _object_schema()}, "required": ["id", "data"]},
                read_only_hint=False,
            ))
            specs.append(ToolSpec(
                name=f"salesloft_delete_{obj}", description=f"Delete a SalesLoft {label} by id.",
                input_schema={"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
                read_only_hint=False, destructive_hint=True,
            ))
        for obj, path in _READ_ONLY_RESOURCES.items():
            label = obj.replace("_", " ")
            tool_suffix = path.replace("/", "_")
            specs.append(ToolSpec(name=f"salesloft_list_{tool_suffix}", description=f"List SalesLoft {label}s (read-only).", input_schema={"type": "object", "properties": {}}))

        specs.append(ToolSpec(
            name="salesloft_enroll_in_cadence",
            description="Enroll a person in a SalesLoft cadence.",
            input_schema={"type": "object", "properties": {"person_id": {"type": "string"}, "cadence_id": {"type": "string"}}, "required": ["person_id", "cadence_id"]},
            read_only_hint=False, idempotent_hint=False,
        ))
        specs.append(ToolSpec(
            name="salesloft_pause_membership",
            description="Pause a person's cadence membership.",
            input_schema={"type": "object", "properties": {"membership_id": {"type": "string"}}, "required": ["membership_id"]},
            read_only_hint=False,
        ))
        specs.append(ToolSpec(
            name="salesloft_resume_membership",
            description="Resume a paused cadence membership.",
            input_schema={"type": "object", "properties": {"membership_id": {"type": "string"}}, "required": ["membership_id"]},
            read_only_hint=False,
        ))
        specs.append(ToolSpec(
            name="salesloft_remove_membership",
            description="Remove a person's cadence membership entirely. Destructive — requires confirmation.",
            input_schema={"type": "object", "properties": {"membership_id": {"type": "string"}}, "required": ["membership_id"]},
            read_only_hint=False, destructive_hint=True, requires_confirm=True,
        ))
        return specs

    def call(self, tool: str, args: dict, conn: Connection) -> dict:
        token = conn.credentials["access_token"]

        if tool == "salesloft_enroll_in_cadence":
            return sl.enroll_in_cadence(token, args["person_id"], args["cadence_id"])
        if tool == "salesloft_pause_membership":
            return sl.pause_membership(token, args["membership_id"])
        if tool == "salesloft_resume_membership":
            return sl.resume_membership(token, args["membership_id"])
        if tool == "salesloft_remove_membership":
            sl.remove_membership(token, args["membership_id"])
            return {"status": "removed"}

        for obj, path in _READ_ONLY_RESOURCES.items():
            if tool == f"salesloft_list_{path.replace('/', '_')}":
                return {"results": sl.list_resource(token, path)}

        for obj, path in _CRUD_RESOURCES.items():
            if tool == f"salesloft_list_{path}":
                return {"results": sl.list_resource(token, path)}
            if tool == f"salesloft_get_{obj}":
                return sl.get_resource(token, path, args["id"])
            if tool == f"salesloft_create_{obj}":
                return sl.create_resource(token, path, args["data"])
            if tool == f"salesloft_update_{obj}":
                return sl.update_resource(token, path, args["id"], args["data"])
            if tool == f"salesloft_delete_{obj}":
                sl.delete_resource(token, path, args["id"])
                return {"status": "deleted"}

        raise ValueError(f"unknown tool: {tool}")

    def refresh(self, conn: Connection) -> Connection:
        import time
        import httpx
        import os
        resp = httpx.post("https://accounts.salesloft.com/oauth/token", data={
            "grant_type": "refresh_token",
            "client_id": os.environ.get("SALESLOFT_CLIENT_ID"),
            "client_secret": os.environ.get("SALESLOFT_CLIENT_SECRET"),
            "refresh_token": conn.credentials["refresh_token"],
        }, timeout=15)
        if resp.status_code >= 400:
            raise sl.SalesLoftError(f"refresh failed: {resp.status_code}: {resp.text}")
        body = resp.json()
        conn.credentials["access_token"] = body["access_token"]
        conn.expires_at = time.time() + body["expires_in"]
        return conn

    def verify(self, conn: Connection) -> None:
        sl.list_resource(conn.credentials["access_token"], "people", {"per_page": 1})


register(SalesLoftAdapter())
