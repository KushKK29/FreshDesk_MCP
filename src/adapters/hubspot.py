"""HubSpot adapter: Contact/Company/Deal/Ticket/Lead/Quote/LineItem/Product
CRUD (HubSpot's generic objects API, one function per object via the same
path-parametrized approach as freshdesk.py/asana.py) plus Note/Call/Email/
Meeting/Task engagement CRUD, associations, per-type search, workflow
enrollment. batch_create/update/archive are the PC-gated bulk actions
(session scope decision: delete/send/bulk only).
"""
try:
    import hubspot_client as hs
    from adapters import AuthMode, Connection, ToolSpec, register
except ImportError:
    from src import hubspot_client as hs
    from src.adapters import AuthMode, Connection, ToolSpec, register

_OBJECT_TYPES = {
    "contact": "contacts",
    "company": "companies",
    "deal": "deals",
    "ticket": "tickets",
    "lead": "leads",
    "quote": "quotes",
    "line_item": "line_items",
    "product": "products",
    "note": "notes",
    "call": "calls",
    "email": "emails",
    "meeting": "meetings",
    "task": "tasks",
}


def _object_schema() -> dict:
    return {"type": "object", "additionalProperties": True}


def _list_tool_name(obj: str) -> str:
    return f"hubspot_list_{obj}s"


def _crud_and_search_tools() -> list[ToolSpec]:
    tools = []
    for obj in _OBJECT_TYPES:
        label = obj.replace("_", " ")
        tools.append(ToolSpec(
            name=_list_tool_name(obj),
            description=f"List HubSpot {label}s.",
            input_schema={"type": "object", "properties": {"limit": {"type": "integer", "default": 100}}},
        ))
        tools.append(ToolSpec(
            name=f"hubspot_get_{obj}",
            description=f"Get a single HubSpot {label} by id.",
            input_schema={"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
        ))
        tools.append(ToolSpec(
            name=f"hubspot_search_{obj}s",
            description=f"Search HubSpot {label}s with a filter query.",
            input_schema={"type": "object", "properties": {"query": _object_schema()}, "required": ["query"]},
        ))
        tools.append(ToolSpec(
            name=f"hubspot_create_{obj}",
            description=f"Create a HubSpot {label}.",
            input_schema={"type": "object", "properties": {"properties": _object_schema()}, "required": ["properties"]},
            read_only_hint=False, idempotent_hint=False,
        ))
        tools.append(ToolSpec(
            name=f"hubspot_update_{obj}",
            description=f"Update a HubSpot {label} by id.",
            input_schema={"type": "object", "properties": {"id": {"type": "string"}, "properties": _object_schema()}, "required": ["id", "properties"]},
            read_only_hint=False,
        ))
        tools.append(ToolSpec(
            name=f"hubspot_delete_{obj}",
            description=f"Archive (delete) a HubSpot {label} by id.",
            input_schema={"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
            read_only_hint=False, destructive_hint=True,
        ))
    return tools


class HubSpotAdapter:
    key = "hubspot"
    display_name = "HubSpot"
    auth_modes = {AuthMode.OAUTH2}
    scopes = ["crm.objects.contacts.read", "crm.objects.contacts.write", "crm.objects.deals.read", "crm.objects.deals.write"]

    def tools(self) -> list[ToolSpec]:
        specs = [
            ToolSpec(
                name="hubspot_associate_objects",
                description="Associate two HubSpot CRM objects (e.g. a contact with a deal) using the default association type.",
                input_schema={"type": "object", "properties": {
                    "from_type": {"type": "string"}, "from_id": {"type": "string"}, "to_type": {"type": "string"}, "to_id": {"type": "string"},
                }, "required": ["from_type", "from_id", "to_type", "to_id"]},
                read_only_hint=False,
            ),
            ToolSpec(
                name="hubspot_get_associations",
                description="List the objects of a given type associated with a HubSpot object.",
                input_schema={"type": "object", "properties": {
                    "from_type": {"type": "string"}, "from_id": {"type": "string"}, "to_type": {"type": "string"},
                }, "required": ["from_type", "from_id", "to_type"]},
            ),
            ToolSpec(
                name="hubspot_enroll_in_workflow",
                description="Enroll a contact (by email) in a HubSpot workflow.",
                input_schema={"type": "object", "properties": {"workflow_id": {"type": "string"}, "contact_email": {"type": "string"}}, "required": ["workflow_id", "contact_email"]},
                read_only_hint=False, idempotent_hint=False,
            ),
            ToolSpec(
                name="hubspot_batch_create",
                description="Batch-create multiple HubSpot objects of the same type in one call. Bulk action — requires confirmation.",
                input_schema={"type": "object", "properties": {"object_type": {"type": "string"}, "inputs": {"type": "array", "items": _object_schema()}}, "required": ["object_type", "inputs"]},
                read_only_hint=False, destructive_hint=True, idempotent_hint=False, requires_confirm=True,
            ),
            ToolSpec(
                name="hubspot_batch_update",
                description="Batch-update multiple HubSpot objects of the same type in one call. Bulk action — requires confirmation.",
                input_schema={"type": "object", "properties": {"object_type": {"type": "string"}, "inputs": {"type": "array", "items": _object_schema()}}, "required": ["object_type", "inputs"]},
                read_only_hint=False, destructive_hint=True, requires_confirm=True,
            ),
            ToolSpec(
                name="hubspot_batch_archive",
                description="Batch-archive (delete) multiple HubSpot objects of the same type in one call. Bulk action — requires confirmation.",
                input_schema={"type": "object", "properties": {"object_type": {"type": "string"}, "ids": {"type": "array", "items": {"type": "string"}}}, "required": ["object_type", "ids"]},
                read_only_hint=False, destructive_hint=True, requires_confirm=True,
            ),
        ]
        specs.extend(_crud_and_search_tools())
        return specs

    def call(self, tool: str, args: dict, conn: Connection) -> dict:
        token = conn.credentials["access_token"]

        if tool == "hubspot_associate_objects":
            return hs.associate_objects(token, args["from_type"], args["from_id"], args["to_type"], args["to_id"])
        if tool == "hubspot_get_associations":
            return {"results": hs.get_associations(token, args["from_type"], args["from_id"], args["to_type"])}
        if tool == "hubspot_enroll_in_workflow":
            hs.enroll_in_workflow(token, args["workflow_id"], args["contact_email"])
            return {"status": "enrolled"}
        if tool == "hubspot_batch_create":
            return hs.batch_create(token, args["object_type"], args["inputs"])
        if tool == "hubspot_batch_update":
            return hs.batch_update(token, args["object_type"], args["inputs"])
        if tool == "hubspot_batch_archive":
            hs.batch_archive(token, args["object_type"], args["ids"])
            return {"status": "archived"}

        for obj, obj_type in _OBJECT_TYPES.items():
            if tool == _list_tool_name(obj):
                return {"results": hs.list_objects(token, obj_type, args.get("limit", 100))}
            if tool == f"hubspot_get_{obj}":
                return hs.get_object(token, obj_type, args["id"])
            if tool == f"hubspot_search_{obj}s":
                return {"results": hs.search_objects(token, obj_type, args["query"])}
            if tool == f"hubspot_create_{obj}":
                return hs.create_object(token, obj_type, args["properties"])
            if tool == f"hubspot_update_{obj}":
                return hs.update_object(token, obj_type, args["id"], args["properties"])
            if tool == f"hubspot_delete_{obj}":
                hs.archive_object(token, obj_type, args["id"])
                return {"status": "deleted"}

        raise ValueError(f"unknown tool: {tool}")

    def refresh(self, conn: Connection) -> Connection:
        import time
        import httpx
        import os
        resp = httpx.post("https://api.hubapi.com/oauth/v1/token", data={
            "grant_type": "refresh_token",
            "client_id": os.environ.get("HUBSPOT_CLIENT_ID"),
            "client_secret": os.environ.get("HUBSPOT_CLIENT_SECRET"),
            "refresh_token": conn.credentials["refresh_token"],
        }, timeout=15)
        if resp.status_code >= 400:
            raise hs.HubSpotError(f"refresh failed: {resp.status_code}: {resp.text}")
        body = resp.json()
        conn.credentials["access_token"] = body["access_token"]
        # ponytail: defensive fallback, same reasoning as asana.py/salesforce.py —
        # don't crash/store None if a refresh response ever omits expires_in.
        conn.expires_at = time.time() + body.get("expires_in", 3600)
        return conn

    def verify(self, conn: Connection) -> None:
        hs.list_objects(conn.credentials["access_token"], "contacts", limit=1)


register(HubSpotAdapter())
