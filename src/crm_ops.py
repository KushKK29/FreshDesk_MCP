"""Cross-CRM analysis tools, built on crm_contacts.py's normalized fetch.
Registered as a pseudo-provider ("crm_ops") in ADAPTERS so its tools are
exposed over MCP through the exact same dispatch path every real provider
uses (mcp_http_server.py's _call_tool_for_current_caller) — but it has no
OAuth connect flow and never appears on the Connected-accounts page: it
has nothing of its own to connect, it only reads connections the user
already made to the five CRM-shaped providers.

All three tools here are read-only analysis (no requires_confirm) — they
recommend, they never write. Acting on a recommendation (merging, deleting,
updating a field) stays a call to the owning provider's own write tool,
which already goes through that provider's own confirm_token gate. That
split is deliberate, not a gap: analysis can run freely, but every write
still needs its existing one-at-a-time approval (PRD-aligned, same rule
the plan asked for).
"""
try:
    from adapters import AuthMode, Connection, ToolSpec, register
    from crm_contacts import CRM_PROVIDERS, fetch_all_contacts
except ImportError:
    from src.adapters import AuthMode, Connection, ToolSpec, register
    from src.crm_contacts import CRM_PROVIDERS, fetch_all_contacts

def _group_key(contact: dict) -> str | None:
    """Exact-email match is the only matching strategy (PRD scope: no fuzzy
    name/phone matching yet — see find_duplicates docstring)."""
    return contact["email"] or None


def find_duplicates(user_id: str) -> dict:
    by_provider = fetch_all_contacts(user_id)
    groups: dict[str, list[dict]] = {}
    for provider, contacts in by_provider.items():
        for c in contacts:
            key = _group_key(c)
            if not key:
                continue
            groups.setdefault(key, []).append(c)

    duplicate_groups = []
    for key, members in groups.items():
        providers_seen = {m["provider"] for m in members}
        if len(providers_seen) < 2:
            continue  # same contact twice within one CRM isn't a cross-CRM duplicate
        conflicts = {}
        for field in ("name", "phone", "company"):
            values = {m[field] for m in members if m[field]}
            if len(values) > 1:
                conflicts[field] = {m["provider"]: m[field] for m in members if m[field]}
        duplicate_groups.append({
            "email": key,
            "members": [{"provider": m["provider"], "id": m["id"], "name": m["name"], "company": m["company"]} for m in members],
            "conflicting_fields": conflicts,
            "recommended_primary": max(members, key=lambda m: sum(1 for f in ("name", "phone", "company") if m[f]))["provider"],
        })

    return {
        "providers_checked": list(by_provider.keys()),
        "duplicate_groups": duplicate_groups,
        "total_contacts_checked": sum(len(v) for v in by_provider.values()),
    }


def _quality_issues(contact: dict) -> list[str]:
    issues = []
    if not contact["email"]:
        issues.append("missing_email")
    elif "@" not in contact["email"]:
        issues.append("invalid_email")
    if not contact["phone"]:
        issues.append("missing_phone")
    elif len(contact["phone"]) < 7:
        issues.append("invalid_phone")
    if not contact["company"]:
        issues.append("missing_company")
    if not contact["name"]:
        issues.append("missing_name")
    return issues


def data_quality_audit(user_id: str) -> dict:
    by_provider = fetch_all_contacts(user_id)
    per_provider_results = {}
    for provider, contacts in by_provider.items():
        flagged = []
        for c in contacts:
            issues = _quality_issues(c)
            if issues:
                severity = "high" if "missing_email" in issues or "invalid_email" in issues else "medium" if len(issues) > 1 else "low"
                flagged.append({"id": c["id"], "name": c["name"], "issues": issues, "severity": severity})
        total = len(contacts)
        clean = total - len(flagged)
        per_provider_results[provider] = {
            "total_contacts": total,
            "flagged_count": len(flagged),
            "quality_score": round(100 * clean / total, 1) if total else None,
            "flagged": flagged,
        }
    return {"providers_checked": list(by_provider.keys()), "results": per_provider_results}


def customer_360(user_id: str, email: str) -> dict:
    email = email.strip().lower()
    by_provider = fetch_all_contacts(user_id)
    matches = []
    for provider, contacts in by_provider.items():
        for c in contacts:
            if c["email"] == email:
                matches.append({
                    "provider": provider,
                    "id": c["id"],
                    "name": c["name"],
                    "phone": c["phone"],
                    "company": c["company"],
                })
    return {
        "email": email,
        "providers_checked": list(by_provider.keys()),
        "found_in": [m["provider"] for m in matches],
        "records": matches,
    }


class CrmOpsAdapter:
    key = "crm_ops"
    display_name = "CRM Ops"
    auth_modes: set[AuthMode] = set()  # not an OAuth-connected provider — nothing to authenticate on its own
    scopes: list[str] = []

    def tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="crm_ops_find_duplicates",
                description=(
                    "Find contacts that exist in more than one connected CRM (HubSpot, Salesforce, Zoho, "
                    "Outreach, SalesLoft) with the same email address. Returns duplicate groups, any "
                    "conflicting fields (name/phone/company) across the matching records, and a recommended "
                    "primary record (the one with the most complete data)."
                ),
                input_schema={"type": "object", "properties": {}},
            ),
            ToolSpec(
                name="crm_ops_data_quality_audit",
                description=(
                    "Audit contacts across every connected CRM for missing/invalid email, phone, company, "
                    "or name. Returns a quality score and flagged records per provider, grouped by severity."
                ),
                input_schema={"type": "object", "properties": {}},
            ),
            ToolSpec(
                name="crm_ops_customer_360",
                description=(
                    "Given an email address, find every matching contact record across connected CRMs and "
                    "return one consolidated view, with each fact labeled by its source provider."
                ),
                input_schema={"type": "object", "properties": {"email": {"type": "string"}}, "required": ["email"]},
            ),
        ]

    def call(self, tool: str, args: dict, conn: Connection) -> dict:
        if tool == "crm_ops_find_duplicates":
            return find_duplicates(conn.user_id)
        if tool == "crm_ops_data_quality_audit":
            return data_quality_audit(conn.user_id)
        if tool == "crm_ops_customer_360":
            return customer_360(conn.user_id, args["email"])
        raise ValueError(f"unknown tool: {tool}")

    def refresh(self, conn: Connection) -> Connection:
        return conn  # no credentials of its own to refresh

    def verify(self, conn: Connection) -> None:
        pass  # no credentials of its own to verify


register(CrmOpsAdapter())
