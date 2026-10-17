"""Freshdesk + CRM cross-provider analysis tools (PRD: power-connector-workflows,
Phase 3-5). Registered as a pseudo-provider ("support_ops"), same pattern as
crm_ops.py — no OAuth of its own, reads connections the user already made.

Freshdesk ticket fields used here (confirmed against freshdesk_client.py +
Freshdesk API v2 docs, PRD Phase 1):
- requester email: not on the ticket object itself — only available via
  get_ticket_for(..., include="requester"), as ticket["requester"]["email"].
- SLA due date: due_by (overall) / fr_due_by (first response), both ISO
  datetime strings already on every ticket, no include needed.
- status: 2=open, 3=pending, 4=resolved, 5=closed. "Unresolved" = not in (4, 5).
"""
from datetime import datetime, timezone

try:
    from adapters import AuthMode, Connection, ToolSpec, register
    from crm_contacts import fetch_all_contacts
    import connections
    import freshdesk_client as fd
except ImportError:
    from src.adapters import AuthMode, Connection, ToolSpec, register
    from src.crm_contacts import fetch_all_contacts
    from src import connections
    from src import freshdesk_client as fd


def _freshdesk_creds(user_id: str) -> tuple[str, str] | None:
    row = connections.get_connection(user_id, "freshdesk")
    if not row or row["status"] != "active":
        return None
    return row["credentials"]["domain"], row["credentials"]["api_key"]


def ticket_360(user_id: str, ticket_id: int) -> dict:
    creds = _freshdesk_creds(user_id)
    if not creds:
        return {"error": "Freshdesk is not connected or not active"}
    domain, api_key = creds
    ticket = fd.get_ticket_for(domain, api_key, ticket_id, include="requester")
    requester_email = (ticket.get("requester") or {}).get("email", "")
    requester_email = requester_email.strip().lower() if requester_email else None

    crm_matches = []
    if requester_email:
        by_provider, _ = fetch_all_contacts(user_id)
        for provider, contacts in by_provider.items():
            for c in contacts:
                if c["email"] == requester_email:
                    crm_matches.append({
                        "provider": provider, "id": c["id"], "name": c["name"], "company": c["company"],
                    })

    return {
        "ticket_id": ticket_id,
        "subject": ticket.get("subject"),
        "status": ticket.get("status"),
        "requester_email": requester_email,
        "crm_matches": crm_matches,
    }


def sla_risk_monitor(user_id: str) -> dict:
    creds = _freshdesk_creds(user_id)
    if not creds:
        return {"error": "Freshdesk is not connected or not active"}
    domain, api_key = creds
    tickets = fd.list_tickets_for(domain, api_key)

    now = datetime.now(timezone.utc)
    at_risk = []
    for t in tickets:
        if t.get("status") in (4, 5):  # resolved/closed — no SLA risk left
            continue
        due_by = t.get("due_by")
        if not due_by:
            continue
        due = datetime.fromisoformat(due_by.replace("Z", "+00:00"))
        hours_left = (due - now).total_seconds() / 3600
        at_risk.append({
            "ticket_id": t.get("id"), "subject": t.get("subject"),
            "due_by": due_by, "hours_remaining": round(hours_left, 1),
            "overdue": hours_left < 0,
        })

    at_risk.sort(key=lambda r: r["hours_remaining"])
    return {"tickets_checked": len(tickets), "at_risk": at_risk}


def repeated_issue_detector(user_id: str) -> dict:
    creds = _freshdesk_creds(user_id)
    if not creds:
        return {"error": "Freshdesk is not connected or not active"}
    domain, api_key = creds
    tickets = fd.list_tickets_for(domain, api_key)

    # ponytail: exact-subject grouping is the ceiling here — add fuzzy/
    # embedding similarity only if near-duplicate subjects slip through in
    # real use.
    groups: dict[str, list[dict]] = {}
    for t in tickets:
        subject = (t.get("subject") or "").strip().lower()
        if not subject:
            continue
        groups.setdefault(subject, []).append({"ticket_id": t.get("id"), "status": t.get("status")})

    repeated = [
        {"subject": subject, "count": len(members), "tickets": members}
        for subject, members in groups.items() if len(members) > 1
    ]
    repeated.sort(key=lambda g: -g["count"])
    return {"tickets_checked": len(tickets), "repeated_issues": repeated}


class SupportOpsAdapter:
    key = "support_ops"
    display_name = "Support Ops"
    auth_modes: set[AuthMode] = set()
    scopes: list[str] = []

    def tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="support_ops_ticket_360",
                description=(
                    "Given a Freshdesk ticket id, return the ticket plus any matching contact record "
                    "across connected CRMs (matched by the ticket requester's email)."
                ),
                input_schema={"type": "object", "properties": {"ticket_id": {"type": "integer"}}, "required": ["ticket_id"]},
            ),
            ToolSpec(
                name="support_ops_sla_risk_monitor",
                description=(
                    "List open/pending Freshdesk tickets sorted by how close they are to their SLA due date "
                    "(due_by), soonest/most overdue first."
                ),
                input_schema={"type": "object", "properties": {}},
            ),
            ToolSpec(
                name="support_ops_repeated_issue_detector",
                description="Group Freshdesk tickets with the same subject to surface recurring issues.",
                input_schema={"type": "object", "properties": {}},
            ),
        ]

    def call(self, tool: str, args: dict, conn: Connection) -> dict:
        if tool == "support_ops_ticket_360":
            return ticket_360(conn.user_id, args["ticket_id"])
        if tool == "support_ops_sla_risk_monitor":
            return sla_risk_monitor(conn.user_id)
        if tool == "support_ops_repeated_issue_detector":
            return repeated_issue_detector(conn.user_id)
        raise ValueError(f"unknown tool: {tool}")

    def refresh(self, conn: Connection) -> Connection:
        return conn

    def verify(self, conn: Connection) -> None:
        pass


register(SupportOpsAdapter())
