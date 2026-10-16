"""Salesforce adapter: Lead/Contact/Account/Opportunity/Case/Campaign/Task/
Event/Note/EmailMessage/User CRUD plus query_soql (guarded by soql_guard.py),
search_sosl, convert_lead. merge_records, bulk_ingest, send_email are
PC-gated (delete/merge + bulk + send, per session scope decision).

instance_url is read only from the connection's credentials (set at OAuth
connect time from Salesforce's own token response) — never from user input,
SSRF guard per PRD §7.5.
"""
try:
    import salesforce_client as sf
    import soql_guard
    from adapters import AuthMode, Connection, ToolSpec, register
except ImportError:
    from src import salesforce_client as sf
    from src import soql_guard
    from src.adapters import AuthMode, Connection, ToolSpec, register

_SOBJECTS = {
    "lead": "Lead",
    "contact": "Contact",
    "account": "Account",
    "opportunity": "Opportunity",
    "case": "Case",
    "campaign": "Campaign",
    "task": "Task",
    "event": "Event",
    "note": "Note",
    "email_message": "EmailMessage",
    "user": "User",
}


def _object_schema() -> dict:
    return {"type": "object", "additionalProperties": True}


def _list_tool_name(obj: str) -> str:
    return f"salesforce_list_{obj}s"


def _crud_tools() -> list[ToolSpec]:
    tools = []
    for obj, sobject in _SOBJECTS.items():
        label = obj.replace("_", " ")
        tools.append(ToolSpec(
            name=_list_tool_name(obj),
            description=f"List Salesforce {sobject} ids (use salesforce_query_soql for field data).",
            input_schema={"type": "object", "properties": {}},
        ))
        tools.append(ToolSpec(
            name=f"salesforce_get_{obj}",
            description=f"Get a single Salesforce {sobject} record by id.",
            input_schema={"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
        ))
        tools.append(ToolSpec(
            name=f"salesforce_create_{obj}",
            description=f"Create a Salesforce {sobject} record.",
            input_schema={"type": "object", "properties": {"data": _object_schema()}, "required": ["data"]},
            read_only_hint=False, idempotent_hint=False,
        ))
        tools.append(ToolSpec(
            name=f"salesforce_update_{obj}",
            description=f"Update a Salesforce {sobject} record by id.",
            input_schema={"type": "object", "properties": {"id": {"type": "string"}, "data": _object_schema()}, "required": ["id", "data"]},
            read_only_hint=False,
        ))
        tools.append(ToolSpec(
            name=f"salesforce_delete_{obj}",
            description=f"Delete a Salesforce {sobject} record by id.",
            input_schema={"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
            read_only_hint=False, destructive_hint=True,
        ))
    return tools


class SalesforceAdapter:
    key = "salesforce"
    display_name = "Salesforce"
    auth_modes = {AuthMode.OAUTH2}
    scopes = ["api", "refresh_token"]

    def tools(self) -> list[ToolSpec]:
        specs = [
            ToolSpec(
                name="salesforce_query_soql",
                description="Run a read-only SOQL SELECT query against the connected Salesforce org. "
                "Only SELECT is allowed; DML and multi-statement queries are rejected. Results are capped "
                "by a LIMIT clause. The connected user's own Salesforce permissions still apply — this tool "
                "cannot see data outside their profile's access.",
                input_schema={"type": "object", "properties": {"soql": {"type": "string"}}, "required": ["soql"]},
            ),
            ToolSpec(
                name="salesforce_search_sosl",
                description="Run a SOSL search across multiple Salesforce object types.",
                input_schema={"type": "object", "properties": {"sosl": {"type": "string"}}, "required": ["sosl"]},
            ),
            ToolSpec(
                name="salesforce_convert_lead",
                description="Convert a Salesforce Lead into an Account/Contact/Opportunity.",
                input_schema={"type": "object", "properties": {"lead_id": {"type": "string"}}, "required": ["lead_id"]},
                read_only_hint=False, idempotent_hint=False,
            ),
            ToolSpec(
                name="salesforce_merge_records",
                description="Merge duplicate Salesforce records into one master record. Destructive — requires confirmation.",
                input_schema={"type": "object", "properties": {
                    "sobject": {"type": "string"}, "master_id": {"type": "string"}, "merge_ids": {"type": "array", "items": {"type": "string"}},
                }, "required": ["sobject", "master_id", "merge_ids"]},
                read_only_hint=False, destructive_hint=True, requires_confirm=True,
            ),
            ToolSpec(
                name="salesforce_bulk_ingest",
                description="Bulk insert/update/upsert/delete records via Salesforce Bulk API 2.0. Bulk action — requires confirmation.",
                input_schema={"type": "object", "properties": {
                    "sobject": {"type": "string"}, "operation": {"type": "string", "enum": ["insert", "update", "upsert", "delete"]}, "records": {"type": "array", "items": _object_schema()},
                }, "required": ["sobject", "operation", "records"]},
                read_only_hint=False, destructive_hint=True, requires_confirm=True,
            ),
            ToolSpec(
                name="salesforce_send_email",
                description="Send an email through Salesforce. Sends externally — requires confirmation.",
                input_schema={"type": "object", "properties": {
                    "to_address": {"type": "string"}, "subject": {"type": "string"}, "body": {"type": "string"},
                }, "required": ["to_address", "subject", "body"]},
                read_only_hint=False, destructive_hint=True, requires_confirm=True,
            ),
        ]
        specs.extend(_crud_tools())
        return specs

    def call(self, tool: str, args: dict, conn: Connection) -> dict:
        instance_url = conn.credentials["instance_url"]
        token = conn.credentials["access_token"]

        if tool == "salesforce_query_soql":
            safe_soql = soql_guard.validate_and_cap(args["soql"])
            return {"records": sf.query_soql(instance_url, token, safe_soql)}
        if tool == "salesforce_search_sosl":
            return {"records": sf.search_sosl(instance_url, token, args["sosl"])}
        if tool == "salesforce_convert_lead":
            return sf.convert_lead(instance_url, token, args["lead_id"])
        if tool == "salesforce_merge_records":
            return sf.merge_records(instance_url, token, args["sobject"], args["master_id"], args["merge_ids"])
        if tool == "salesforce_bulk_ingest":
            return sf.bulk_ingest(instance_url, token, args["sobject"], args["operation"], args["records"])
        if tool == "salesforce_send_email":
            return sf.send_email(instance_url, token, args["to_address"], args["subject"], args["body"])

        for obj, sobject in _SOBJECTS.items():
            if tool == _list_tool_name(obj):
                return {"ids": sf.list_objects(instance_url, token, sobject)}
            if tool == f"salesforce_get_{obj}":
                return sf.get_object(instance_url, token, sobject, args["id"])
            if tool == f"salesforce_create_{obj}":
                return sf.create_object(instance_url, token, sobject, args["data"])
            if tool == f"salesforce_update_{obj}":
                sf.update_object(instance_url, token, sobject, args["id"], args["data"])
                return {"status": "updated"}
            if tool == f"salesforce_delete_{obj}":
                sf.delete_object(instance_url, token, sobject, args["id"])
                return {"status": "deleted"}

        raise ValueError(f"unknown tool: {tool}")

    def refresh(self, conn: Connection) -> Connection:
        import time
        import httpx
        import os
        resp = httpx.post(f"{conn.credentials['instance_url']}/services/oauth2/token", data={
            "grant_type": "refresh_token",
            "client_id": os.environ.get("SALESFORCE_CLIENT_ID"),
            "client_secret": os.environ.get("SALESFORCE_CLIENT_SECRET"),
            "refresh_token": conn.credentials["refresh_token"],
        }, timeout=15)
        if resp.status_code >= 400:
            raise sf.SalesforceError(f"refresh failed: {resp.status_code}: {resp.text}")
        body = resp.json()
        conn.credentials["access_token"] = body["access_token"]
        conn.expires_at = time.time() + 7200  # Salesforce doesn't return expires_in on refresh; a conservative fixed window
        return conn

    def verify(self, conn: Connection) -> None:
        sf.query_soql(conn.credentials["instance_url"], conn.credentials["access_token"], "SELECT Id FROM Organization LIMIT 1")


register(SalesforceAdapter())
