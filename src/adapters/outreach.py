"""Outreach adapter: Prospect/Account/Sequence/SequenceStep/Template CRUD
plus enroll_in_sequence, pause/resume/finish_sequence, log_call. Mailbox is
read-only (referenced for enrollment, not created/edited via this connector).
request_compliance_deletion (GDPR) is PC-gated (delete).
"""
try:
    import outreach_client as oc
    from adapters import AuthMode, Connection, ToolSpec, register
except ImportError:
    from src import outreach_client as oc
    from src.adapters import AuthMode, Connection, ToolSpec, register

_RESOURCES = {
    "prospect": ("prospects", "prospect"),
    "account": ("accounts", "account"),
    "sequence": ("sequences", "sequence"),
    "sequence_step": ("sequenceSteps", "sequenceStep"),
    "template": ("templates", "template"),
}


def _object_schema() -> dict:
    return {"type": "object", "additionalProperties": True}


def _list_tool_name(obj: str) -> str:
    return f"outreach_list_{obj}s"


def _crud_tools() -> list[ToolSpec]:
    tools = []
    for obj, (path, _type) in _RESOURCES.items():
        label = obj.replace("_", " ")
        tools.append(ToolSpec(
            name=_list_tool_name(obj),
            description=f"List Outreach {label}s.",
            input_schema={"type": "object", "properties": {}},
        ))
        tools.append(ToolSpec(
            name=f"outreach_get_{obj}",
            description=f"Get a single Outreach {label} by id.",
            input_schema={"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
        ))
        tools.append(ToolSpec(
            name=f"outreach_create_{obj}",
            description=f"Create an Outreach {label}.",
            input_schema={"type": "object", "properties": {"attributes": _object_schema()}, "required": ["attributes"]},
            read_only_hint=False, idempotent_hint=False,
        ))
        tools.append(ToolSpec(
            name=f"outreach_update_{obj}",
            description=f"Update an Outreach {label} by id.",
            input_schema={"type": "object", "properties": {"id": {"type": "string"}, "attributes": _object_schema()}, "required": ["id", "attributes"]},
            read_only_hint=False,
        ))
        tools.append(ToolSpec(
            name=f"outreach_delete_{obj}",
            description=f"Delete an Outreach {label} by id.",
            input_schema={"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
            read_only_hint=False, destructive_hint=True,
        ))
    tools.append(ToolSpec(
        name="outreach_list_mailboxes",
        description="List Outreach mailboxes available for sequence enrollment (read-only).",
        input_schema={"type": "object", "properties": {}},
    ))
    return tools


class OutreachAdapter:
    key = "outreach"
    display_name = "Outreach"
    auth_modes = {AuthMode.OAUTH2}
    scopes = ["prospects.all", "sequences.all", "sequenceStates.all", "accounts.all", "calls.all"]

    def tools(self) -> list[ToolSpec]:
        specs = [
            ToolSpec(
                name="outreach_enroll_in_sequence",
                description="Enroll a prospect into an Outreach sequence via a given mailbox.",
                input_schema={"type": "object", "properties": {
                    "prospect_id": {"type": "string"}, "sequence_id": {"type": "string"}, "mailbox_id": {"type": "string"},
                }, "required": ["prospect_id", "sequence_id", "mailbox_id"]},
                read_only_hint=False, idempotent_hint=False,
            ),
            ToolSpec(
                name="outreach_pause_sequence",
                description="Pause a prospect's sequence enrollment.",
                input_schema={"type": "object", "properties": {"sequence_state_id": {"type": "string"}}, "required": ["sequence_state_id"]},
                read_only_hint=False,
            ),
            ToolSpec(
                name="outreach_resume_sequence",
                description="Resume a paused sequence enrollment.",
                input_schema={"type": "object", "properties": {"sequence_state_id": {"type": "string"}}, "required": ["sequence_state_id"]},
                read_only_hint=False,
            ),
            ToolSpec(
                name="outreach_finish_sequence",
                description="Mark a sequence enrollment as finished.",
                input_schema={"type": "object", "properties": {"sequence_state_id": {"type": "string"}}, "required": ["sequence_state_id"]},
                read_only_hint=False,
            ),
            ToolSpec(
                name="outreach_log_call",
                description="Log a call against an Outreach prospect.",
                input_schema={"type": "object", "properties": {
                    "prospect_id": {"type": "string"}, "direction": {"type": "string"}, "outcome": {"type": "string"}, "duration": {"type": "integer"},
                }, "required": ["prospect_id", "direction", "outcome", "duration"]},
                read_only_hint=False, idempotent_hint=False,
            ),
            ToolSpec(
                name="outreach_request_compliance_deletion",
                description="Request GDPR-style deletion of a prospect by email. Destructive — requires confirmation.",
                input_schema={"type": "object", "properties": {"email": {"type": "string"}}, "required": ["email"]},
                read_only_hint=False, destructive_hint=True, requires_confirm=True,
            ),
        ]
        specs.extend(_crud_tools())
        return specs

    def call(self, tool: str, args: dict, conn: Connection) -> dict:
        token = conn.credentials["access_token"]

        if tool == "outreach_list_mailboxes":
            return {"results": oc.list_resource(token, "mailboxes")}
        if tool == "outreach_enroll_in_sequence":
            return oc.enroll_in_sequence(token, args["prospect_id"], args["sequence_id"], args["mailbox_id"])
        if tool == "outreach_pause_sequence":
            return oc.pause_sequence(token, args["sequence_state_id"])
        if tool == "outreach_resume_sequence":
            return oc.resume_sequence(token, args["sequence_state_id"])
        if tool == "outreach_finish_sequence":
            return oc.finish_sequence(token, args["sequence_state_id"])
        if tool == "outreach_log_call":
            return oc.log_call(token, args["prospect_id"], args["direction"], args["outcome"], args["duration"])
        if tool == "outreach_request_compliance_deletion":
            return oc.request_compliance_deletion(token, args["email"])

        for obj, (path, resource_type) in _RESOURCES.items():
            if tool == _list_tool_name(obj):
                return {"results": oc.list_resource(token, path)}
            if tool == f"outreach_get_{obj}":
                return oc.get_resource(token, path, args["id"])
            if tool == f"outreach_create_{obj}":
                return oc.create_resource(token, path, resource_type, args["attributes"])
            if tool == f"outreach_update_{obj}":
                return oc.update_resource(token, path, resource_type, args["id"], args["attributes"])
            if tool == f"outreach_delete_{obj}":
                oc.delete_resource(token, path, args["id"])
                return {"status": "deleted"}

        raise ValueError(f"unknown tool: {tool}")

    def refresh(self, conn: Connection) -> Connection:
        import time
        import httpx
        import os
        resp = httpx.post("https://api.outreach.io/oauth/token", data={
            "grant_type": "refresh_token",
            "client_id": os.environ.get("OUTREACH_CLIENT_ID"),
            "client_secret": os.environ.get("OUTREACH_CLIENT_SECRET"),
            "refresh_token": conn.credentials["refresh_token"],
        }, timeout=15)
        if resp.status_code >= 400:
            raise oc.OutreachError(f"refresh failed: {resp.status_code}: {resp.text}")
        body = resp.json()
        conn.credentials["access_token"] = body["access_token"]
        conn.expires_at = time.time() + body["expires_in"]
        return conn

    def verify(self, conn: Connection) -> None:
        oc.list_resource(conn.credentials["access_token"], "mailboxes")


register(OutreachAdapter())
