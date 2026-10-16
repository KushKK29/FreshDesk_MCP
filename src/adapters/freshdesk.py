"""Freshdesk adapter: wraps freshdesk_client.py's functions behind the
ProviderAdapter contract. The original 3 tools (list_tickets/get_ticket/
search_tickets) keep identical names and behavior (M0b exit criteria);
everything else is new full-parity coverage (connector-hub plan).

auth_modes={API_KEY} only — Freshdesk has no OAuth app for this use yet
(PRD §7.2 table), API-key connect is the only mode until that changes.
"""
import re

try:
    import freshdesk_client as fd
    from adapters import AuthMode, Connection, ToolSpec, register
except ImportError:
    from src import freshdesk_client as fd
    from src.adapters import AuthMode, Connection, ToolSpec, register

# Freshdesk domain is attacker-controlled input used to build an outbound
# request URL (SSRF surface) — validated once here, reused by every connect
# path (oauth_server.authorize_decision, connected_accounts.connect_freshdesk)
# instead of duplicating the pattern per caller.
_FRESHDESK_DOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}\.freshdesk\.com$")


def validate_freshdesk_domain(domain: str) -> str | None:
    cleaned = domain.strip().rstrip(".").lower()
    return cleaned if _FRESHDESK_DOMAIN_RE.fullmatch(cleaned) else None

_CRUD_OBJECTS = {
    "contact": "contacts",
    "company": "companies",
    "time_entry": "time_entries",
    "canned_response": "canned_responses",
    "solution": "solutions/categories",
}

def _object_schema() -> dict:
    return {"type": "object", "additionalProperties": True}


def _list_tool_name(obj: str) -> str:
    return f"freshdesk_list_{obj[:-1]}ies" if obj.endswith("y") else f"freshdesk_list_{obj}s"


def _crud_tools() -> list[ToolSpec]:
    tools = []
    for obj in _CRUD_OBJECTS:
        label = obj.replace("_", " ")
        tools.append(ToolSpec(
            name=_list_tool_name(obj),
            description=f"List Freshdesk {label}s, paginated.",
            input_schema={"type": "object", "properties": {"page": {"type": "integer", "default": 1}}},
        ))
        tools.append(ToolSpec(
            name=f"freshdesk_get_{obj}",
            description=f"Get a single Freshdesk {label} by id.",
            input_schema={"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]},
        ))
        tools.append(ToolSpec(
            name=f"freshdesk_create_{obj}",
            description=f"Create a Freshdesk {label}.",
            input_schema={"type": "object", "properties": {"data": _object_schema()}, "required": ["data"]},
            read_only_hint=False, idempotent_hint=False,
        ))
        tools.append(ToolSpec(
            name=f"freshdesk_update_{obj}",
            description=f"Update a Freshdesk {label} by id.",
            input_schema={"type": "object", "properties": {"id": {"type": "integer"}, "data": _object_schema()}, "required": ["id", "data"]},
            read_only_hint=False,
        ))
        tools.append(ToolSpec(
            name=f"freshdesk_delete_{obj}",
            description=f"Delete a Freshdesk {label} by id. Destructive — requires confirmation.",
            input_schema={"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]},
            read_only_hint=False, destructive_hint=True, requires_confirm=True,
        ))
    tools.append(ToolSpec(
        name="freshdesk_create_ticket",
        description="Create a Freshdesk ticket.",
        input_schema={"type": "object", "properties": {"data": _object_schema()}, "required": ["data"]},
        read_only_hint=False, idempotent_hint=False,
    ))
    tools.append(ToolSpec(
        name="freshdesk_update_ticket",
        description="Update a Freshdesk ticket by id.",
        input_schema={"type": "object", "properties": {"id": {"type": "integer"}, "data": _object_schema()}, "required": ["id", "data"]},
        read_only_hint=False,
    ))
    tools.append(ToolSpec(
        name="freshdesk_delete_ticket",
        description="Delete a Freshdesk ticket by id. Destructive — requires confirmation.",
        input_schema={"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]},
        read_only_hint=False, destructive_hint=True, requires_confirm=True,
    ))
    return tools


class FreshdeskAdapter:
    key = "freshdesk"
    display_name = "Freshdesk"
    auth_modes = {AuthMode.API_KEY}
    scopes: list[str] = []

    def tools(self) -> list[ToolSpec]:
        specs = [
            ToolSpec(
                name="freshdesk_list_tickets",
                description="List tickets for the connected Freshdesk account, newest first, paginated. "
                "Optionally filter by status or by an updated_since timestamp (ISO 8601).",
                input_schema={"type": "object", "properties": {
                    "status": {"type": "string"}, "updated_since": {"type": "string"}, "page": {"type": "integer", "default": 1},
                }},
            ),
            ToolSpec(
                name="freshdesk_get_ticket",
                description="Fetch the full details of a single Freshdesk ticket by its numeric id.",
                input_schema={"type": "object", "properties": {
                    "ticket_id": {"type": "integer"}, "include": {"type": "string"},
                }, "required": ["ticket_id"]},
            ),
            ToolSpec(
                name="freshdesk_search_tickets",
                description="Search Freshdesk tickets matching a query string, e.g. 'status:2 AND priority:3'.",
                input_schema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
            ),
            ToolSpec(
                name="freshdesk_reply_ticket",
                description="Reply to a Freshdesk ticket as the connected agent.",
                input_schema={"type": "object", "properties": {
                    "ticket_id": {"type": "integer"}, "body": {"type": "string"},
                }, "required": ["ticket_id", "body"]},
                read_only_hint=False,
            ),
            ToolSpec(
                name="freshdesk_add_note_ticket",
                description="Add a note (public or private) to a Freshdesk ticket.",
                input_schema={"type": "object", "properties": {
                    "ticket_id": {"type": "integer"}, "body": {"type": "string"}, "private": {"type": "boolean", "default": True},
                }, "required": ["ticket_id", "body"]},
                read_only_hint=False,
            ),
            ToolSpec(
                name="freshdesk_merge_contacts",
                description="Merge secondary Freshdesk contacts into a primary contact.",
                input_schema={"type": "object", "properties": {
                    "primary_contact_id": {"type": "integer"}, "secondary_contact_ids": {"type": "array", "items": {"type": "integer"}},
                }, "required": ["primary_contact_id", "secondary_contact_ids"]},
                read_only_hint=False, idempotent_hint=False,
            ),
            ToolSpec(
                name="freshdesk_bulk_update_tickets",
                description="Bulk-update properties on multiple Freshdesk tickets at once. Requires confirmation.",
                input_schema={"type": "object", "properties": {
                    "ticket_ids": {"type": "array", "items": {"type": "integer"}}, "properties": _object_schema(),
                }, "required": ["ticket_ids", "properties"]},
                read_only_hint=False, destructive_hint=True, requires_confirm=True,
            ),
            ToolSpec(
                name="freshdesk_forward_ticket",
                description="Forward a Freshdesk ticket by email to external recipients. Sends email — requires confirmation.",
                input_schema={"type": "object", "properties": {
                    "ticket_id": {"type": "integer"}, "to_emails": {"type": "array", "items": {"type": "string"}}, "body": {"type": "string"},
                }, "required": ["ticket_id", "to_emails", "body"]},
                read_only_hint=False, destructive_hint=True, requires_confirm=True,
            ),
        ]
        specs.extend(_crud_tools())
        return specs

    def call(self, tool: str, args: dict, conn: Connection) -> dict:
        domain = conn.credentials["domain"]
        api_key = conn.credentials["api_key"]

        if tool == "freshdesk_list_tickets":
            return {"results": fd.list_tickets_for(domain, api_key, args.get("status"), args.get("updated_since"), args.get("page", 1))}
        if tool == "freshdesk_get_ticket":
            return fd.get_ticket_for(domain, api_key, args["ticket_id"], args.get("include"))
        if tool == "freshdesk_search_tickets":
            return {"results": fd.search_tickets_for(domain, api_key, args["query"])}
        if tool == "freshdesk_reply_ticket":
            return fd.reply_ticket(domain, api_key, args["ticket_id"], args["body"])
        if tool == "freshdesk_add_note_ticket":
            return fd.add_note_ticket(domain, api_key, args["ticket_id"], args["body"], args.get("private", True))
        if tool == "freshdesk_merge_contacts":
            fd.merge_contacts(domain, api_key, args["primary_contact_id"], args["secondary_contact_ids"])
            return {"status": "merged"}
        if tool == "freshdesk_bulk_update_tickets":
            return fd.bulk_update_tickets(domain, api_key, args["ticket_ids"], args["properties"])
        if tool == "freshdesk_forward_ticket":
            return fd.forward_ticket(domain, api_key, args["ticket_id"], args["to_emails"], args["body"])

        if tool == "freshdesk_create_ticket":
            return fd.create_resource(domain, api_key, "tickets", args["data"])
        if tool == "freshdesk_update_ticket":
            return fd.update_resource(domain, api_key, "tickets", args["id"], args["data"])
        if tool == "freshdesk_delete_ticket":
            fd.delete_resource(domain, api_key, "tickets", args["id"])
            return {"status": "deleted"}

        for obj, path in _CRUD_OBJECTS.items():
            if tool == _list_tool_name(obj):
                return {"results": fd.list_resource(domain, api_key, path, args.get("page", 1))}
            if tool == f"freshdesk_get_{obj}":
                return fd.get_resource(domain, api_key, path, args["id"])
            if tool == f"freshdesk_create_{obj}":
                return fd.create_resource(domain, api_key, path, args["data"])
            if tool == f"freshdesk_update_{obj}":
                return fd.update_resource(domain, api_key, path, args["id"], args["data"])
            if tool == f"freshdesk_delete_{obj}":
                fd.delete_resource(domain, api_key, path, args["id"])
                return {"status": "deleted"}

        raise ValueError(f"unknown tool: {tool}")

    def refresh(self, conn: Connection) -> Connection:
        raise NotImplementedError("Freshdesk uses API-key auth — no token refresh")

    def verify(self, conn: Connection) -> None:
        # Same check oauth_server.py did inline before the connections-store rewiring.
        fd.list_tickets_for(conn.credentials["domain"], conn.credentials["api_key"], page=1)


register(FreshdeskAdapter())
