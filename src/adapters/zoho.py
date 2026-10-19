"""Zoho CRM adapter: Lead/Contact/Account/Deal/Task/Event/Call/Campaign/
Product/Note/Attachment CRUD plus query_coql, upsert_record, convert_lead,
get_related_records. mass_convert_leads and bulk_read/bulk_write are
PC-gated (bulk, per session scope decision).

DC (data center) handling (PRD §7.2, §7.5 SSRF guard): the host is resolved
from a fixed allowlist of Zoho's known DCs, taken from the connection's
stored `dc` field (set at OAuth connect time from Zoho's own response) —
never interpolated from user input.
"""
try:
    import zoho_client as zh
    from adapters import AuthMode, Connection, ToolSpec, register
except ImportError:
    from src import zoho_client as zh
    from src.adapters import AuthMode, Connection, ToolSpec, register

_DC_HOSTS = {
    "us": "www.zohoapis.com",
    "eu": "www.zohoapis.eu",
    "in": "www.zohoapis.in",
    "au": "www.zohoapis.com.au",
    "jp": "www.zohoapis.jp",
    "uk": "www.zohoapis.uk",
}


class UnknownDataCenter(Exception):
    pass


def _resolve_host(dc: str) -> str:
    host = _DC_HOSTS.get(dc)
    if not host:
        raise UnknownDataCenter(f"unknown Zoho data center: {dc!r}")
    return host


_MODULES = {
    "lead": "Leads",
    "contact": "Contacts",
    "account": "Accounts",
    "deal": "Deals",
    "task": "Tasks",
    "event": "Events",
    "call": "Calls",
    "campaign": "Campaigns",
    "product": "Products",
    "note": "Notes",
    "attachment": "Attachments",
}


def _object_schema() -> dict:
    return {"type": "object", "additionalProperties": True}


def _list_tool_name(obj: str) -> str:
    return f"zoho_list_{obj}s"


def _crud_tools() -> list[ToolSpec]:
    tools = []
    for obj, module in _MODULES.items():
        label = obj.replace("_", " ")
        tools.append(ToolSpec(
            name=_list_tool_name(obj),
            description=f"List Zoho CRM {module}.",
            input_schema={"type": "object", "properties": {}},
        ))
        tools.append(ToolSpec(
            name=f"zoho_get_{obj}",
            description=f"Get a single Zoho CRM {label} by id.",
            input_schema={"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
        ))
        tools.append(ToolSpec(
            name=f"zoho_create_{obj}",
            description=f"Create a Zoho CRM {label}.",
            input_schema={"type": "object", "properties": {"data": _object_schema()}, "required": ["data"]},
            read_only_hint=False, idempotent_hint=False,
        ))
        tools.append(ToolSpec(
            name=f"zoho_update_{obj}",
            description=f"Update a Zoho CRM {label} by id.",
            input_schema={"type": "object", "properties": {"id": {"type": "string"}, "data": _object_schema()}, "required": ["id", "data"]},
            read_only_hint=False,
        ))
        tools.append(ToolSpec(
            name=f"zoho_delete_{obj}",
            description=f"Delete a Zoho CRM {label} by id.",
            input_schema={"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
            read_only_hint=False, destructive_hint=True,
        ))
    return tools


class ZohoAdapter:
    key = "zoho"
    display_name = "Zoho CRM"
    auth_modes = {AuthMode.OAUTH2}
    scopes = ["ZohoCRM.modules.ALL"]

    def tools(self) -> list[ToolSpec]:
        specs = [
            ToolSpec(
                name="zoho_query_coql",
                description="Run a COQL (SQL-like) SELECT query against the connected Zoho CRM org.",
                input_schema={"type": "object", "properties": {"select_query": {"type": "string"}}, "required": ["select_query"]},
            ),
            ToolSpec(
                name="zoho_upsert_record",
                description="Upsert (create-or-update by external key) a Zoho CRM record.",
                input_schema={"type": "object", "properties": {"module": {"type": "string"}, "data": _object_schema()}, "required": ["module", "data"]},
                read_only_hint=False,
            ),
            ToolSpec(
                name="zoho_convert_lead",
                description="Convert a single Zoho CRM Lead.",
                input_schema={"type": "object", "properties": {"lead_id": {"type": "string"}}, "required": ["lead_id"]},
                read_only_hint=False, idempotent_hint=False,
            ),
            ToolSpec(
                name="zoho_get_related_records",
                description="Get records related to a Zoho CRM record (e.g. a deal's contacts).",
                input_schema={"type": "object", "properties": {"module": {"type": "string"}, "id": {"type": "string"}, "related_list": {"type": "string"}}, "required": ["module", "id", "related_list"]},
            ),
            ToolSpec(
                name="zoho_mass_convert_leads",
                description="Convert up to 50 Zoho CRM Leads in one call. Bulk action — requires confirmation.",
                input_schema={"type": "object", "properties": {"lead_ids": {"type": "array", "items": {"type": "string"}}}, "required": ["lead_ids"]},
                read_only_hint=False, destructive_hint=True, requires_confirm=True,
            ),
            ToolSpec(
                name="zoho_bulk_read",
                description="Start an async bulk export job for a Zoho CRM module. Bulk action — requires confirmation.",
                input_schema={"type": "object", "properties": {"module": {"type": "string"}}, "required": ["module"]},
                destructive_hint=True, requires_confirm=True,
            ),
            ToolSpec(
                name="zoho_bulk_write",
                description="Start an async bulk insert/update/upsert job for a Zoho CRM module. Bulk action — requires confirmation.",
                input_schema={"type": "object", "properties": {
                    "module": {"type": "string"}, "operation": {"type": "string", "enum": ["insert", "update", "upsert"]}, "records": {"type": "array", "items": _object_schema()},
                }, "required": ["module", "operation", "records"]},
                read_only_hint=False, destructive_hint=True, requires_confirm=True,
            ),
        ]
        specs.extend(_crud_tools())
        return specs

    def call(self, tool: str, args: dict, conn: Connection) -> dict:
        host = _resolve_host(conn.credentials["dc"])
        token = conn.credentials["access_token"]

        if tool == "zoho_query_coql":
            return {"records": zh.query_coql(host, token, args["select_query"])}
        if tool == "zoho_upsert_record":
            return zh.upsert_record(host, token, args["module"], args["data"])
        if tool == "zoho_convert_lead":
            return zh.convert_lead(host, token, args["lead_id"])
        if tool == "zoho_get_related_records":
            return {"records": zh.get_related_records(host, token, args["module"], args["id"], args["related_list"])}
        if tool == "zoho_mass_convert_leads":
            return zh.mass_convert_leads(host, token, args["lead_ids"])
        if tool == "zoho_bulk_read":
            return zh.bulk_read(host, token, args["module"])
        if tool == "zoho_bulk_write":
            return zh.bulk_write(host, token, args["module"], args["operation"], args["records"])

        for obj, module in _MODULES.items():
            if tool == _list_tool_name(obj):
                return {"records": zh.list_records(host, token, module)}
            if tool == f"zoho_get_{obj}":
                return zh.get_record(host, token, module, args["id"])
            if tool == f"zoho_create_{obj}":
                return zh.create_record(host, token, module, args["data"])
            if tool == f"zoho_update_{obj}":
                return zh.update_record(host, token, module, args["id"], args["data"])
            if tool == f"zoho_delete_{obj}":
                zh.delete_record(host, token, module, args["id"])
                return {"status": "deleted"}

        raise ValueError(f"unknown tool: {tool}")

    def refresh(self, conn: Connection) -> Connection:
        import time
        import httpx
        import os
        host = _resolve_host(conn.credentials["dc"])
        accounts_host = host.replace("zohoapis", "zoho")
        resp = httpx.post(f"https://accounts.{accounts_host.split('.', 1)[1]}/oauth/v2/token", data={
            "grant_type": "refresh_token",
            "client_id": os.environ.get("ZOHO_CLIENT_ID"),
            "client_secret": os.environ.get("ZOHO_CLIENT_SECRET"),
            "refresh_token": conn.credentials["refresh_token"],
        }, timeout=15)
        if resp.status_code >= 400:
            raise zh.ZohoError(f"refresh failed: {resp.status_code}: {resp.text}")
        body = resp.json()
        conn.credentials["access_token"] = body["access_token"]
        # ponytail: defensive fallback, same reasoning as asana.py/salesforce.py —
        # don't crash/store None if a refresh response ever omits expires_in.
        conn.expires_at = time.time() + body.get("expires_in", 3600)
        return conn

    def verify(self, conn: Connection) -> None:
        host = _resolve_host(conn.credentials["dc"])
        zh.list_records(host, conn.credentials["access_token"], "Leads")


register(ZohoAdapter())
