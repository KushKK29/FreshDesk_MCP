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
from datetime import datetime, timezone
from difflib import SequenceMatcher

try:
    from adapters import AuthMode, Connection, ToolSpec, register
    from crm_contacts import CRM_PROVIDERS, fetch_all_contacts, fetch_all_deals
    import connections
    import freshdesk_client as fd
except ImportError:
    from src.adapters import AuthMode, Connection, ToolSpec, register
    from src.crm_contacts import CRM_PROVIDERS, fetch_all_contacts, fetch_all_deals
    from src import connections
    from src import freshdesk_client as fd

def _group_key(contact: dict) -> str | None:
    """Exact-email match is the only matching strategy (PRD scope: no fuzzy
    name/phone matching yet — see find_duplicates docstring)."""
    return contact["email"] or None


def _build_groups(by_provider: dict[str, list[dict]]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for provider, contacts in by_provider.items():
        for c in contacts:
            key = _group_key(c)
            if not key:
                continue
            groups.setdefault(key, []).append(c)
    return groups


def _group_result(key: str, members: list[dict], confidence: str = "exact") -> dict:
    conflicts = {}
    for field in ("name", "phone", "company"):
        values = {m[field] for m in members if m[field]}
        if len(values) > 1:
            conflicts[field] = {m["provider"]: m[field] for m in members if m[field]}
    return {
        "email": key,
        "members": [{"provider": m["provider"], "id": m["id"], "name": m["name"], "company": m["company"]} for m in members],
        "conflicting_fields": conflicts,
        "recommended_primary": max(members, key=lambda m: sum(1 for f in ("name", "phone", "company") if m[f]))["provider"],
        "confidence": confidence,
    }


def find_duplicates(user_id: str) -> dict:
    by_provider, fetch_errors = fetch_all_contacts(user_id)
    groups = _build_groups(by_provider)

    duplicate_groups = []
    for key, members in groups.items():
        providers_seen = {m["provider"] for m in members}
        if len(providers_seen) < 2:
            continue  # same contact twice within one CRM isn't a cross-CRM duplicate
        duplicate_groups.append(_group_result(key, members, "exact"))

    return {
        "providers_checked": list(by_provider.keys()),
        "providers_skipped": fetch_errors,
        "duplicate_groups": duplicate_groups,
        "total_contacts_checked": sum(len(v) for v in by_provider.values()),
    }


_NAME_MATCH_THRESHOLD = 0.82  # SequenceMatcher ratio — "kush goel" vs "kush  goel" passes, unrelated names don't


def _phone_match(a: str, b: str) -> bool:
    # Last 7 digits — ignores country code / extension formatting differences already
    # stripped to digits-only by crm_contacts._norm, but a leading "1" or missing area
    # code would otherwise split what's really the same number.
    return bool(a) and bool(b) and a[-7:] == b[-7:]


def _name_match(a: str, b: str) -> bool:
    return bool(a) and bool(b) and SequenceMatcher(None, a.lower(), b.lower()).ratio() >= _NAME_MATCH_THRESHOLD


def find_duplicates_fuzzy(user_id: str) -> dict:
    """find_duplicates, plus a second pass over contacts that DIDN'T match on
    exact email: group remaining contacts whose name is a close fuzzy match
    (typo/nickname-tolerant) AND whose phone matches, or whose name matches
    AND company matches — two weaker signals agreeing is the bar, since name
    alone is too noisy (common names) and phone/company alone can coincide
    for unrelated people at the same firm or a shared office line.

    Confidence tiers, most to least certain: "exact" (find_duplicates' email
    match), "high" (fuzzy name + phone), "medium" (fuzzy name + company).
    Singletons (everyone else) aren't returned — nothing to merge.
    """
    by_provider, fetch_errors = fetch_all_contacts(user_id)
    exact_groups = _build_groups(by_provider)

    exact_duplicate_groups = []
    matched_ids: set[tuple[str, str]] = set()
    for key, members in exact_groups.items():
        providers_seen = {m["provider"] for m in members}
        if len(providers_seen) < 2:
            continue
        exact_duplicate_groups.append(_group_result(key, members, "exact"))
        matched_ids.update((m["provider"], m["id"]) for m in members)

    # Remaining pool: contacts not already placed in an exact-email group.
    remaining = [c for rows in by_provider.values() for c in rows if (c["provider"], c["id"]) not in matched_ids and c["name"]]

    fuzzy_groups = []
    used: set[tuple[str, str]] = set()
    for i, a in enumerate(remaining):
        a_key = (a["provider"], a["id"])
        if a_key in used:
            continue
        cluster = [a]
        seen_providers = {a["provider"]}
        for b in remaining[i + 1:]:
            b_key = (b["provider"], b["id"])
            if b_key in used or b["provider"] in seen_providers:
                continue
            # Match against any member already in the cluster, not just the seed —
            # otherwise a valid three-way chain (A~B, B~C, but A and C too far
            # apart to match directly) silently drops C.
            m = next((m for m in cluster if _name_match(m["name"], b["name"])), None)
            if not m:
                continue
            if _phone_match(m["phone"], b["phone"]) or (m["company"] and m["company"] == b["company"]):
                cluster.append(b)
                used.add(b_key)
                seen_providers.add(b["provider"])
        if len(cluster) < 2:
            continue
        used.add(a_key)
        confidence = "high" if any(_phone_match(x["phone"], y["phone"]) for x in cluster for y in cluster if x is not y) else "medium"
        fuzzy_groups.append(_group_result(a["name"].lower(), cluster, confidence))

    all_groups = exact_duplicate_groups + fuzzy_groups
    return {
        "providers_checked": list(by_provider.keys()),
        "providers_skipped": fetch_errors,
        "duplicate_groups": all_groups,
        "total_contacts_checked": sum(len(v) for v in by_provider.values()),
    }


def field_conflicts(user_id: str) -> dict:
    """Full per-record detail behind find_duplicates's conflicting_fields
    summary: every field value, per provider, for each cross-CRM duplicate
    group that disagrees on name/phone/company."""
    dup_result = find_duplicates(user_id)
    conflicts = [g for g in dup_result["duplicate_groups"] if g["conflicting_fields"]]
    return {
        "providers_checked": dup_result["providers_checked"],
        "providers_skipped": dup_result["providers_skipped"],
        "conflict_groups": conflicts,
        "total_conflicts": len(conflicts),
    }


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        v = value[:-1] + "+00:00" if value.endswith("Z") else value
        dt = datetime.fromisoformat(v)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def field_conflicts_with_suggestion(user_id: str) -> dict:
    """field_conflicts, plus a suggested resolution per conflicting field:
    the value from whichever provider has the most recent last_activity
    timestamp for that contact. A suggestion, never applied automatically —
    acting on it is still a write through that provider's own tool."""
    by_provider, fetch_errors = fetch_all_contacts(user_id)
    groups = _build_groups(by_provider)

    conflict_groups = []
    for key, members in groups.items():
        providers_seen = {m["provider"] for m in members}
        if len(providers_seen) < 2:
            continue
        base = _group_result(key, members, "exact")
        if not base["conflicting_fields"]:
            continue
        conflicts = {}
        for field, per_provider in base["conflicting_fields"].items():
            dated = [(m["provider"], m[field], _parse_ts(m["last_activity"])) for m in members if m[field]]
            with_ts = [d for d in dated if d[2] is not None]
            newest = max(with_ts, key=lambda d: d[2]) if with_ts else None
            conflicts[field] = {
                "values": per_provider,
                "suggested_value": newest[1] if newest else None,
                "suggested_from": newest[0] if newest else None,
                "basis": "most_recent_activity" if newest else "no_timestamp_available",
            }
        conflict_groups.append({
            "email": key,
            "members": base["members"],
            "conflicting_fields": conflicts,
        })

    return {
        "providers_checked": list(by_provider.keys()),
        "providers_skipped": fetch_errors,
        "conflict_groups": conflict_groups,
        "total_conflicts": len(conflict_groups),
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
    by_provider, fetch_errors = fetch_all_contacts(user_id)
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
    return {"providers_checked": list(by_provider.keys()), "providers_skipped": fetch_errors, "results": per_provider_results}


def customer_360(user_id: str, email: str) -> dict:
    email = email.strip().lower()
    by_provider, fetch_errors = fetch_all_contacts(user_id)
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
        "providers_skipped": fetch_errors,
        "found_in": [m["provider"] for m in matches],
        "records": matches,
    }


def customer_search(user_id: str, query: str) -> dict:
    """Customer 360, but by substring match on name/email/company instead of
    exact email only — for the hub UI's search box. Not exposed as an MCP
    tool (crm_ops_customer_360 keeps its narrower, agent-facing email-only
    contract); this is JSON-API only, via ops_api.py."""
    query = query.strip().lower()
    by_provider, fetch_errors = fetch_all_contacts(user_id)
    matches = []
    for provider, contacts in by_provider.items():
        for c in contacts:
            haystack = " ".join(filter(None, [c["name"], c["email"], c["company"]])).lower()
            if query in haystack:
                matches.append({
                    "provider": provider,
                    "id": c["id"],
                    "name": c["name"],
                    "email": c["email"],
                    "phone": c["phone"],
                    "company": c["company"],
                })
    return {
        "query": query,
        "providers_checked": list(by_provider.keys()),
        "providers_skipped": fetch_errors,
        "found_in": sorted({m["provider"] for m in matches}),
        "records": matches,
    }


def _freshdesk_requester_emails(user_id: str) -> set[str] | None:
    """Every email that has ever been a Freshdesk ticket requester, or None
    if Freshdesk isn't connected (stale_contact_finder then skips that
    signal rather than treating "not connected" as "no ticket")."""
    row = connections.get_connection(user_id, "freshdesk")
    if not row or row["status"] != "active":
        return None
    domain, api_key = row["credentials"]["domain"], row["credentials"]["api_key"]
    emails: set[str] = set()
    for page in range(1, 11):  # same _MAX_PAGES cap support_ops.py uses
        batch = fd.list_tickets_for(domain, api_key, include="requester", per_page=100, page=page)
        for t in batch:
            email = ((t.get("requester") or {}).get("email") or "").strip().lower()
            if email:
                emails.add(email)
        if len(batch) < 100:
            break
    return emails


def stale_contact_finder(user_id: str, days: int = 90) -> dict:
    """Contacts idle for at least `days` (by last_activity, or with no
    last_activity at all — a record a CRM has never touched is at least as
    stale as one it touched 90 days ago) that also have no open deal and,
    when Freshdesk is connected, no ticket ever raised by that email. Three
    independent signals of "nobody is doing anything with this record" —
    candidates for archival/cleanup, never auto-deleted."""
    by_provider, fetch_errors = fetch_all_contacts(user_id)
    deals_by_provider, deal_errors = fetch_all_deals(user_id)
    fetch_errors = {**fetch_errors, **{f"deals:{k}": v for k, v in deal_errors.items()}}

    # HubSpot deals carry no company inline (crm_contacts.py's own docstring) —
    # resolve via the deal's associated contact_ids, same as support_ops.py's _load.
    contact_by_id = {(c["provider"], c["id"]): c for rows in by_provider.values() for c in rows}
    open_accounts: set[str] = set()
    for provider, rows in deals_by_provider.items():
        for d in rows:
            if d["is_closed"]:
                continue
            account = d["account"]
            if not account:
                for cid in d["contact_ids"]:
                    c = contact_by_id.get((provider, cid))
                    if c and c["company"]:
                        account = c["company"]
                        break
            if account:
                open_accounts.add(account.strip().lower())
    ticket_emails = _freshdesk_requester_emails(user_id)
    now = datetime.now(timezone.utc)
    cutoff_days = max(days, 0)

    stale = []
    for provider, contacts in by_provider.items():
        for c in contacts:
            ts = _parse_ts(c["last_activity"])
            idle_days = (now - ts).days if ts else None
            is_stale_by_time = idle_days is None or idle_days >= cutoff_days
            if not is_stale_by_time:
                continue
            has_open_deal = bool(c["company"]) and c["company"].strip().lower() in open_accounts
            if has_open_deal:
                continue
            has_ticket = ticket_emails is not None and c["email"] in ticket_emails
            if has_ticket:
                continue
            stale.append({
                "provider": c["provider"], "id": c["id"], "name": c["name"], "email": c["email"],
                "company": c["company"], "last_activity": c["last_activity"], "idle_days": idle_days,
            })

    return {
        "providers_checked": list(by_provider.keys()),
        "providers_skipped": fetch_errors,
        "freshdesk_checked": ticket_emails is not None,
        "days_threshold": cutoff_days,
        "stale_contacts": stale,
        "total_contacts_checked": sum(len(v) for v in by_provider.values()),
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
                name="crm_ops_field_conflicts",
                description=(
                    "Full per-record detail for cross-CRM duplicates whose name/phone/company values "
                    "disagree across providers: every value, labeled by source provider, for each conflicting group."
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
            ToolSpec(
                name="crm_ops_find_duplicates_fuzzy",
                description=(
                    "Like crm_ops_find_duplicates, plus a second pass that catches likely duplicates missed by "
                    "exact-email matching: contacts whose name is a close fuzzy match and whose phone or company "
                    "also agrees. Each group is tagged with a confidence (exact/high/medium) instead of a flat yes/no."
                ),
                input_schema={"type": "object", "properties": {}},
            ),
            ToolSpec(
                name="crm_ops_stale_contact_finder",
                description=(
                    "Find CRM contacts idle for at least N days (default 90) — no recent activity in the CRM, no "
                    "open deal tied to their company, and (if Freshdesk is connected) no support ticket ever raised "
                    "by that email. Candidates for list cleanup; nothing is deleted."
                ),
                input_schema={"type": "object", "properties": {"days": {"type": "integer"}}},
            ),
        ]

    def call(self, tool: str, args: dict, conn: Connection) -> dict:
        if tool == "crm_ops_find_duplicates":
            return find_duplicates(conn.user_id)
        if tool == "crm_ops_data_quality_audit":
            return data_quality_audit(conn.user_id)
        if tool == "crm_ops_field_conflicts":
            return field_conflicts(conn.user_id)
        if tool == "crm_ops_customer_360":
            return customer_360(conn.user_id, args["email"])
        if tool == "crm_ops_find_duplicates_fuzzy":
            return find_duplicates_fuzzy(conn.user_id)
        if tool == "crm_ops_stale_contact_finder":
            return stale_contact_finder(conn.user_id, args.get("days", 90))
        raise ValueError(f"unknown tool: {tool}")

    def refresh(self, conn: Connection) -> Connection:
        return conn  # no credentials of its own to refresh

    def verify(self, conn: Connection) -> None:
        pass  # no credentials of its own to verify


register(CrmOpsAdapter())
