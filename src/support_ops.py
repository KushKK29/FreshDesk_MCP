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
import hashlib
import json
import re
from datetime import datetime, timezone
from difflib import SequenceMatcher

try:
    from adapters import AuthMode, Connection, ToolSpec, register
    from crm_contacts import fetch_all_contacts, fetch_all_deals, fetch_all_memberships, get_fresh_conn
    import asana_client as az
    import connections
    import db as _dbmod
    import freshdesk_client as fd
    import hubspot_client as hs
    import outreach_client as oc
    import salesforce_client as sf
    import salesloft_client as sl
    import zoho_client as zh
    from adapters.zoho import _resolve_host as _zoho_host
except ImportError:
    from src.adapters import AuthMode, Connection, ToolSpec, register
    from src.crm_contacts import fetch_all_contacts, fetch_all_deals, fetch_all_memberships, get_fresh_conn
    from src import asana_client as az
    from src import connections
    from src import db as _dbmod
    from src import freshdesk_client as fd
    from src import hubspot_client as hs
    from src import outreach_client as oc
    from src import salesforce_client as sf
    from src import salesloft_client as sl
    from src import zoho_client as zh
    from src.adapters.zoho import _resolve_host as _zoho_host


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



# ── Shared loader for the cross-system tools below ──────────────────────────
# All of these join Freshdesk tickets to CRM data through the requester email
# (same join as ticket_360), then CRM contact -> company name. Read-only
# except the functions explicitly marked as writes (all confirm-gated tools).

_MAX_PAGES = 3  # ponytail: 300 most-recent tickets; add paging-until-empty if a helpdesk outgrows it
_CLOSED = (4, 5)
_HIGH_PRIORITY = (3, 4)  # Freshdesk: 1 low, 2 medium, 3 high, 4 urgent


def _parse(ts: str | None) -> datetime | None:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None


def _hours(a: datetime, b: datetime) -> float:
    return (a - b).total_seconds() / 3600


def _ckey(name: str | None) -> str:
    return (name or "").strip().lower()


def _load(user_id: str, deals: bool = False, memberships: bool = False, description: bool = False) -> dict:
    creds = _freshdesk_creds(user_id)
    if not creds:
        return {"error": "Freshdesk is not connected or not active"}
    domain, api_key = creds
    include = "requester,stats" + (",description" if description else "")
    tickets: list[dict] = []
    for page in range(1, _MAX_PAGES + 1):
        batch = fd.list_tickets_for(domain, api_key, page=page, include=include, per_page=100)
        tickets += batch
        if len(batch) < 100:
            break

    contacts, skipped = fetch_all_contacts(user_id)
    contact_by_id, company_by_email, emails = {}, {}, set()
    for provider, rows in contacts.items():
        for c in rows:
            contact_by_id[(provider, c["id"])] = c
            if c["email"]:
                emails.add(c["email"])
                if c["company"]:
                    company_by_email.setdefault(c["email"], c["company"])
    for t in tickets:
        r = t.get("requester") or {}
        t["_email"] = (r.get("email") or "").strip().lower()
        t["_name"] = r.get("name") or ""
        t["_company"] = company_by_email.get(t["_email"], "")

    ctx = {"domain": domain, "tickets": tickets, "contacts": contacts, "skipped": skipped,
           "contact_by_id": contact_by_id, "crm_emails": emails, "now": datetime.now(timezone.utc)}
    if deals:
        by_provider, derr = fetch_all_deals(user_id)
        skipped.update({f"deals:{k}": v for k, v in derr.items()})
        flat = []
        for provider, rows in by_provider.items():
            for d in rows:
                if not d["account"]:  # HubSpot: resolve company via associated contacts
                    for cid in d["contact_ids"]:
                        c = contact_by_id.get((provider, cid))
                        if c and c["company"]:
                            d["account"] = c["company"]
                            break
                d.pop("raw", None)
                flat.append(d)
        ctx["deals"] = flat
    if memberships:
        by_provider, merr = fetch_all_memberships(user_id)
        skipped.update({f"memberships:{k}": v for k, v in merr.items()})
        ctx["memberships"] = [m for rows in by_provider.values() for m in rows]
    return ctx


def _brief(t: dict, now: datetime) -> dict:
    due = _parse(t.get("due_by"))
    left = round(_hours(due, now), 1) if due else None
    return {
        "ticket_id": t.get("id"), "subject": t.get("subject"), "status": t.get("status"),
        "priority": t.get("priority"), "requester_email": t["_email"], "company": t["_company"],
        "due_by": t.get("due_by"), "hours_remaining": left,
        "overdue": left is not None and left < 0 and t.get("status") not in _CLOSED,
    }


def _open(t: dict) -> bool:
    return t.get("status") not in _CLOSED


def _breached(t: dict, now: datetime) -> bool:
    due = _parse(t.get("due_by"))
    if not due:
        return False
    if _open(t):
        return now > due
    done = _parse((t.get("stats") or {}).get("resolved_at"))
    return bool(done and done > due)


def _deal_brief(d: dict) -> dict:
    return {k: d[k] for k in ("provider", "id", "name", "amount", "stage", "account", "last_activity")}


def _open_deals_by_company(ctx: dict) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for d in ctx["deals"]:
        if not d["is_closed"] and d["account"]:
            out.setdefault(_ckey(d["account"]), []).append(d)
    return out


# 1 ── outreach_collision_guard ─────────────────────────────────────────────

def outreach_collision_guard(user_id: str) -> dict:
    ctx = _load(user_id, memberships=True)
    if "error" in ctx:
        return ctx
    now = ctx["now"]
    by_email: dict[str, dict] = {}
    for t in ctx["tickets"]:
        if _open(t) and t["_email"]:
            by_email.setdefault(t["_email"], {"email": t["_email"], "tickets": [], "memberships": []})["tickets"].append(_brief(t, now))
    for m in ctx["memberships"]:
        if not m["active"]:
            continue
        c = ctx["contact_by_id"].get((m["provider"], m["contact_id"]))
        if c and c["email"] in by_email:
            by_email[c["email"]]["memberships"].append({"provider": m["provider"], "membership_id": m["id"], "state": m["state"]})
    collisions = [v for v in by_email.values() if v["memberships"]]
    return {"collisions": collisions, "providers_skipped": ctx["skipped"]}


def pause_collisions(user_id: str, memberships: list[dict]) -> dict:
    """WRITE (confirm-gated tool). Pauses exactly the memberships passed in —
    take them from outreach_collision_guard's result."""
    results = []
    conns: dict[str, Connection | None] = {}
    for m in memberships:
        provider, mid = m["provider"], str(m["membership_id"])
        try:
            if provider not in ("outreach", "salesloft"):
                raise ValueError(f"cannot pause on {provider}")
            if provider not in conns:
                conns[provider] = get_fresh_conn(user_id, provider)[0]
            conn = conns[provider]
            if not conn:
                raise ValueError(f"{provider} is not connected")
            token = conn.credentials["access_token"]
            (oc.pause_sequence if provider == "outreach" else sl.pause_membership)(token, mid)
            results.append({"provider": provider, "membership_id": mid, "paused": True})
        except Exception as exc:
            results.append({"provider": provider, "membership_id": mid, "paused": False, "error": str(exc)})
    return {"results": results}


# 2 ── churn_risk_radar ─────────────────────────────────────────────────────
# Weights are a heuristic, not a model — tune here. Freshdesk only exposes
# whether a ticket was reopened (stats.reopened_at), not how many times.
_W_OPEN, _W_REOPEN, _W_HIGH, _W_BREACH = 1, 3, 2, 3


def _score(tickets: list[dict], now: datetime) -> dict:
    open_n = sum(1 for t in tickets if _open(t))
    reopened = sum(1 for t in tickets if (t.get("stats") or {}).get("reopened_at"))
    high = sum(1 for t in tickets if _open(t) and t.get("priority") in _HIGH_PRIORITY)
    breaches = sum(1 for t in tickets if _breached(t, now))
    return {
        "score": open_n * _W_OPEN + reopened * _W_REOPEN + high * _W_HIGH + breaches * _W_BREACH,
        "open_tickets": open_n, "total_tickets": len(tickets), "reopened": reopened,
        "high_priority_open": high, "sla_breaches": breaches,
    }


def churn_risk_radar(user_id: str) -> dict:
    ctx = _load(user_id, deals=True)
    if "error" in ctx:
        return ctx
    by_co: dict[str, list[dict]] = {}
    names: dict[str, str] = {}
    for t in ctx["tickets"]:
        if t["_company"]:
            by_co.setdefault(_ckey(t["_company"]), []).append(t)
            names[_ckey(t["_company"])] = t["_company"]
    deals = _open_deals_by_company(ctx)
    accounts = []
    for key, ts in by_co.items():
        row = {"account": names[key], **_score(ts, ctx["now"])}
        row["open_deals"] = [_deal_brief(d) for d in deals.get(key, [])]
        row["revenue_at_risk"] = sum(d["amount"] for d in deals.get(key, []))
        row["flagged"] = bool(row["open_deals"]) and row["score"] > 0
        accounts.append(row)
    accounts.sort(key=lambda a: (-a["flagged"], -a["score"]))
    return {"accounts": accounts, "providers_skipped": ctx["skipped"]}


# 3 ── vip_ticket_escalator ─────────────────────────────────────────────────

def vip_ticket_escalator(user_id: str, min_amount: float = 0) -> dict:
    ctx = _load(user_id, deals=True)
    if "error" in ctx:
        return ctx
    deals = _open_deals_by_company(ctx)
    rows = []
    for t in ctx["tickets"]:
        if not _open(t) or not t.get("due_by"):
            continue
        b = _brief(t, ctx["now"])
        ds = deals.get(_ckey(t["_company"]), [])
        amount = sum(d["amount"] for d in ds)
        b.update({"deal_amount": amount, "deals": [_deal_brief(d) for d in ds], "vip": bool(ds) and amount >= min_amount})
        rows.append(b)
    # VIPs first, then plain SLA order inside each tier.
    rows.sort(key=lambda r: (not r["vip"], r["hours_remaining"]))
    return {"tickets": rows, "providers_skipped": ctx["skipped"]}


# 4 ── ticket_to_task_sync ──────────────────────────────────────────────────

def _links():
    c = _dbmod.db["ticket_task_links"]
    c.create_index([("user_id", 1), ("ticket_id", 1)], unique=True)
    return c


def _asana_token(user_id: str) -> str | None:
    conn, _ = get_fresh_conn(user_id, "asana")
    return conn.credentials["access_token"] if conn else None


def _is_bug(t: dict) -> bool:
    return (t.get("type") or "").lower() == "bug" or "bug" in [x.lower() for x in (t.get("tags") or [])]


def ticket_to_task(user_id: str, ticket_id: int, project_gid: str) -> dict:
    """WRITE (confirm-gated): Asana task with a link back to the ticket, plus a
    private note on the ticket linking forward to the task."""
    creds = _freshdesk_creds(user_id)
    token = _asana_token(user_id)
    if not creds or not token:
        return {"error": "Freshdesk and Asana must both be connected"}
    if _links().find_one({"user_id": user_id, "ticket_id": ticket_id}):
        return {"error": f"ticket {ticket_id} is already linked to an Asana task"}
    domain, api_key = creds
    t = fd.get_ticket_for(domain, api_key, ticket_id)
    url = f"https://{domain}/a/tickets/{ticket_id}"
    task = az.create_resource(token, "tasks", {
        "name": f"[Freshdesk #{ticket_id}] {t.get('subject')}",
        "notes": f"{url}\n\n{t.get('description_text') or ''}",
        "projects": [project_gid],
    })
    task_url = f"https://app.asana.com/0/{project_gid}/{task['gid']}"
    fd.add_note_ticket(domain, api_key, ticket_id, f"Escalated to engineering: {task_url}")
    _links().insert_one({"user_id": user_id, "ticket_id": ticket_id, "task_gid": task["gid"], "task_url": task_url, "synced": False})
    return {"ticket_id": ticket_id, "task_gid": task["gid"], "task_url": task_url}


def task_links(user_id: str) -> dict:
    """Linked tickets with live Asana completion state, plus bug-class open
    tickets that have no task yet."""
    creds = _freshdesk_creds(user_id)
    if not creds:
        return {"error": "Freshdesk is not connected or not active"}
    token = _asana_token(user_id)
    links = list(_links().find({"user_id": user_id}, {"_id": 0, "user_id": 0}))
    for l in links:
        l["completed"] = None
        if token:
            try:
                l["completed"] = bool(az.get_resource(token, "tasks", l["task_gid"], "completed").get("completed"))
            except Exception as exc:
                l["error"] = str(exc)
    linked = {l["ticket_id"] for l in links}
    domain, api_key = creds
    candidates = [
        {"ticket_id": t["id"], "subject": t.get("subject"), "priority": t.get("priority")}
        for t in fd.list_tickets_for(domain, api_key, per_page=100)
        if _open(t) and _is_bug(t) and t["id"] not in linked
    ]
    return {"links": links, "bug_candidates": candidates, "asana_connected": bool(token)}


def sync_completed_tasks(user_id: str, close: bool = False) -> dict:
    """WRITE (confirm-gated): for every linked, not-yet-synced task that is
    complete in Asana, comment on the ticket (and close it if `close`)."""
    creds = _freshdesk_creds(user_id)
    token = _asana_token(user_id)
    if not creds or not token:
        return {"error": "Freshdesk and Asana must both be connected"}
    domain, api_key = creds
    synced = []
    for l in _links().find({"user_id": user_id, "synced": False}):
        try:
            if not az.get_resource(token, "tasks", l["task_gid"], "completed").get("completed"):
                continue
            fd.add_note_ticket(domain, api_key, l["ticket_id"], f"Engineering task completed: {l['task_url']}", private=False)
            if close:
                fd.update_resource(domain, api_key, "tickets", l["ticket_id"], {"status": 5})
            _links().update_one({"_id": l["_id"]}, {"$set": {"synced": True}})
            synced.append({"ticket_id": l["ticket_id"], "closed": close})
        except Exception as exc:
            synced.append({"ticket_id": l["ticket_id"], "error": str(exc)})
    return {"synced": synced}


# 5 ── unlinked_requester_finder ────────────────────────────────────────────

def unlinked_requester_finder(user_id: str) -> dict:
    ctx = _load(user_id)
    if "error" in ctx:
        return ctx
    if not ctx["contacts"]:
        return {"error": "No CRM connected — nothing to compare requesters against", "providers_skipped": ctx["skipped"]}
    seen: dict[str, dict] = {}
    for t in ctx["tickets"]:
        e = t["_email"]
        if e and e not in ctx["crm_emails"]:
            r = seen.setdefault(e, {"email": e, "name": t["_name"], "ticket_count": 0})
            r["ticket_count"] += 1
    return {"requesters": sorted(seen.values(), key=lambda r: -r["ticket_count"]),
            "crm_providers": list(ctx["contacts"]), "providers_skipped": ctx["skipped"]}


def _split_name(name: str, email: str) -> tuple[str, str]:
    parts = (name or "").split()
    if not parts:
        return "", email.split("@")[0]
    return " ".join(parts[:-1]), parts[-1]


def create_missing_contacts(user_id: str, provider: str, contacts: list[dict]) -> dict:
    """WRITE (confirm-gated): create a contact/lead in one CRM per entry."""
    conn, err = get_fresh_conn(user_id, provider)
    if not conn:
        return {"error": err or f"{provider} is not connected"}
    token, cr = conn.credentials["access_token"], conn.credentials
    results = []
    for c in contacts:
        email = c["email"].strip().lower()
        first, last = _split_name(c.get("name", ""), email)
        try:
            if provider == "hubspot":
                hs.create_object(token, "contacts", {"email": email, "firstname": first, "lastname": last})
            elif provider == "salesforce":
                sf.create_object(cr["instance_url"], token, "Lead", {"Email": email, "FirstName": first, "LastName": last, "Company": "[not provided]"})
            elif provider == "zoho":
                zh.create_record(_zoho_host(cr["dc"]), token, "Contacts", {"Email": email, "First_Name": first, "Last_Name": last})
            elif provider == "outreach":
                oc.create_resource(token, "prospects", "prospect", {"emails": [email], "firstName": first, "lastName": last})
            elif provider == "salesloft":
                sl.create_resource(token, "people", {"email_address": email, "first_name": first, "last_name": last})
            else:
                raise ValueError(f"unsupported provider {provider}")
            results.append({"email": email, "created": True})
        except Exception as exc:
            results.append({"email": email, "created": False, "error": str(exc)})
    return {"provider": provider, "results": results}


# 6 ── outreach_reply_triage ────────────────────────────────────────────────
# ponytail: keyword match, not an LLM classifier — misses paraphrases and
# negations ("not interested in pricing"). Swap _signals() for an LLM call if
# precision matters. CRM owner isn't in the normalized contact shape, so
# routing to a rep is the existing ticket_to_task tool (Asana), not auto.
_BUYING = re.compile(r"\b(pricing|price|quote|demo|upgrade|purchase|buy|license|licence|discount|contract|renewal|enterprise|seats?|plan|trial)\b", re.I)


def _signals(text: str) -> list[str]:
    return sorted({m.lower() for m in _BUYING.findall(text or "")})


def outreach_reply_triage(user_id: str) -> dict:
    ctx = _load(user_id, memberships=True, description=True)
    if "error" in ctx:
        return ctx
    active_by_email: dict[str, list[dict]] = {}
    for m in ctx["memberships"]:
        c = ctx["contact_by_id"].get((m["provider"], m["contact_id"]))
        if m["active"] and c and c["email"]:
            active_by_email.setdefault(c["email"], []).append({"provider": m["provider"], "membership_id": m["id"]})
    out = []
    for t in ctx["tickets"]:
        sig = _signals(f"{t.get('subject') or ''} {t.get('description_text') or ''}")
        if sig:
            out.append({**_brief(t, ctx["now"]), "signals": sig, "active_cadences": active_by_email.get(t["_email"], [])})
    return {"tickets": out, "providers_skipped": ctx["skipped"]}


# 7 ── repeated_issue_by_account ────────────────────────────────────────────
# Fuzzy subject = difflib ratio on a normalized subject (stdlib; the upgrade
# the exact-match detector's ponytail comment asked for). Greedy single-pass
# clustering, O(n*clusters) — fine at a few hundred tickets.
_SIMILARITY = 0.75


def _norm_subject(s: str | None) -> str:
    s = re.sub(r"^(\s*(re|fw|fwd)\s*:\s*)+", "", (s or "").lower())
    return re.sub(r"\d+", "#", re.sub(r"[^\w\s]", " ", s)).strip()


def _cluster(tickets: list[dict]) -> list[list[dict]]:
    clusters: list[tuple[str, list[dict]]] = []
    for t in tickets:
        n = _norm_subject(t.get("subject"))
        if not n:
            continue
        for head, members in clusters:
            if SequenceMatcher(None, head, n).ratio() >= _SIMILARITY:
                members.append(t)
                break
        else:
            clusters.append((n, [t]))
    return [m for _, m in clusters if len(m) > 1]


def repeated_issue_by_account(user_id: str) -> dict:
    ctx = _load(user_id)
    if "error" in ctx:
        return ctx
    by_co: dict[str, list[dict]] = {}
    for t in ctx["tickets"]:
        by_co.setdefault(_ckey(t["_company"]) or "(no CRM company)", []).append(t)
    accounts = []
    for co, ts in by_co.items():
        issues = [{"subject": m[0].get("subject"), "count": len(m), "open": sum(1 for t in m if _open(t)),
                   "ticket_ids": [t["id"] for t in m]} for m in _cluster(ts)]
        if issues:
            accounts.append({"account": ts[0]["_company"] or co, "issues": sorted(issues, key=lambda i: -i["count"]),
                             "repeat_tickets": sum(i["count"] for i in issues)})
    accounts.sort(key=lambda a: -a["repeat_tickets"])
    return {"accounts": accounts, "providers_skipped": ctx["skipped"]}


# 8 ── support_load_by_segment ──────────────────────────────────────────────
# Segment = CRM company, or a Freshdesk field. CRM region/owner aren't in the
# normalized contact shape, so they aren't offered (add to crm_contacts._norm).
_GROUPS = ("company", "priority", "type", "source")


def support_load_by_segment(user_id: str, group_by: str = "company") -> dict:
    if group_by not in _GROUPS:
        return {"error": f"group_by must be one of {list(_GROUPS)}"}
    ctx = _load(user_id)
    if "error" in ctx:
        return ctx
    buckets: dict[str, list[dict]] = {}
    for t in ctx["tickets"]:
        k = (t["_company"] or "(no CRM company)") if group_by == "company" else str(t.get(group_by))
        buckets.setdefault(k, []).append(t)

    def avg(vals: list[float]) -> float | None:
        return round(sum(vals) / len(vals), 1) if vals else None

    rows = []
    for k, ts in buckets.items():
        fr, res = [], []
        for t in ts:
            created, st = _parse(t.get("created_at")), t.get("stats") or {}
            if created and st.get("first_responded_at"):
                fr.append(_hours(_parse(st["first_responded_at"]), created))
            if created and st.get("resolved_at"):
                res.append(_hours(_parse(st["resolved_at"]), created))
        rows.append({"segment": k, "tickets": len(ts), "open": sum(1 for t in ts if _open(t)),
                     "avg_first_response_hours": avg(fr), "avg_resolution_hours": avg(res)})
    rows.sort(key=lambda r: -r["tickets"])
    return {"group_by": group_by, "segments": rows, "providers_skipped": ctx["skipped"]}


# 9 ── stale_deal_vs_open_ticket ────────────────────────────────────────────

def stale_deal_vs_open_ticket(user_id: str, days: int = 14) -> dict:
    ctx = _load(user_id, deals=True)
    if "error" in ctx:
        return ctx
    open_by_co: dict[str, list[dict]] = {}
    for t in ctx["tickets"]:
        if _open(t) and t["_company"]:
            open_by_co.setdefault(_ckey(t["_company"]), []).append(_brief(t, ctx["now"]))
    stuck = []
    for d in ctx["deals"]:
        if d["is_closed"] or not d["account"]:
            continue
        last = _parse(d["last_activity"]) if d["last_activity"] else None
        if last and last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        idle = round(_hours(ctx["now"], last) / 24, 1) if last else None
        tickets = open_by_co.get(_ckey(d["account"]), [])
        if tickets and (idle is None or idle >= days):
            stuck.append({**_deal_brief(d), "idle_days": idle, "open_tickets": tickets})
    stuck.sort(key=lambda d: -(d["idle_days"] if d["idle_days"] is not None else 1e9))
    return {"days": days, "stuck_deals": stuck, "providers_skipped": ctx["skipped"]}


def customer_followup_gap_detector(user_id: str, days: int = 7) -> dict:
    """Open tickets and open deals that nobody has touched in `days` days —
    a single review queue for things slipping through, across support and
    sales. Ticket freshness is Freshdesk's own updated_at (bumped by any
    agent or requester reply, or a status change); deal freshness is the
    CRM's own last_activity. Distinct from stale_deal_vs_open_ticket, which
    cross-references deals against *tickets on the same account* — this one
    flags each side on its own idle clock, tickets and deals both."""
    ctx = _load(user_id, deals=True)
    if "error" in ctx:
        return ctx
    now = ctx["now"]

    stale_tickets = []
    for t in ctx["tickets"]:
        if not _open(t):
            continue
        updated = _parse(t.get("updated_at"))
        if updated and updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        idle = round(_hours(now, updated) / 24, 1) if updated else None
        if idle is None or idle >= days:
            stale_tickets.append({**_brief(t, now), "idle_days": idle})

    stale_deals = []
    for d in ctx["deals"]:
        if d["is_closed"]:
            continue
        last = _parse(d["last_activity"]) if d["last_activity"] else None
        if last and last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        idle = round(_hours(now, last) / 24, 1) if last else None
        if idle is None or idle >= days:
            stale_deals.append({**_deal_brief(d), "idle_days": idle})

    stale_tickets.sort(key=lambda t: -(t["idle_days"] if t["idle_days"] is not None else 1e9))
    stale_deals.sort(key=lambda d: -(d["idle_days"] if d["idle_days"] is not None else 1e9))
    return {
        "days": days,
        "stale_tickets": stale_tickets,
        "stale_deals": stale_deals,
        "providers_skipped": ctx["skipped"],
    }


# 10 ── daily_digest_scheduler ──────────────────────────────────────────────
# Scheduler = Vercel cron hitting /api/cron/digest (see ops_api.py). State =
# one fingerprint per user in `ops_digests`. Channel = an Asana task in the
# project the user picked in `ops_settings` (no email sender exists here).

def _digest_state():
    return _dbmod.db["ops_digests"]


def _settings():
    return _dbmod.db["ops_settings"]


def daily_digest(user_id: str) -> dict:
    sla = sla_risk_monitor(user_id)
    if "error" in sla:
        return sla
    coll = outreach_collision_guard(user_id)
    churn = churn_risk_radar(user_id)
    digest = {
        "sla": [r for r in sla["at_risk"] if r["overdue"] or r["hours_remaining"] < 24][:10],
        "collisions": [c["email"] for c in coll.get("collisions", [])],
        "churn": [a["account"] for a in churn.get("accounts", []) if a["flagged"]][:5],
    }
    fp = hashlib.sha256(json.dumps({"s": [r["ticket_id"] for r in digest["sla"]], "c": digest["collisions"], "r": digest["churn"]}, sort_keys=True).encode()).hexdigest()
    last = _digest_state().find_one({"user_id": user_id})
    return {"digest": digest, "fingerprint": fp, "changed": not last or last["fingerprint"] != fp}


def run_digest(user_id: str) -> dict:
    """Cron entry: compute, and only if something changed since the last run,
    post one Asana task and record the new fingerprint."""
    cfg = _settings().find_one({"user_id": user_id})
    if not cfg or not cfg.get("project_gid"):
        return {"skipped": "no digest project configured"}
    token = _asana_token(user_id)
    if not token:
        return {"skipped": "Asana not connected"}
    d = daily_digest(user_id)
    if "error" in d or not d["changed"]:
        return {"skipped": d.get("error", "no change")}
    dg = d["digest"]
    lines = [f"Overdue / <24h SLA: {len(dg['sla'])}"] + [f"  #{r['ticket_id']} {r['subject']} ({r['hours_remaining']}h)" for r in dg["sla"]]
    lines += [f"Outreach collisions: {', '.join(dg['collisions']) or 'none'}", f"Churn-risk accounts with open deals: {', '.join(dg['churn']) or 'none'}"]
    az.create_resource(token, "tasks", {"name": f"Support digest {datetime.now(timezone.utc):%Y-%m-%d}", "notes": "\n".join(lines), "projects": [cfg["project_gid"]]})
    _digest_state().update_one({"user_id": user_id}, {"$set": {"fingerprint": d["fingerprint"]}}, upsert=True)
    return {"posted": True}


def set_digest_project(user_id: str, project_gid: str | None) -> dict:
    if project_gid:
        _settings().update_one({"user_id": user_id}, {"$set": {"project_gid": project_gid}}, upsert=True)
    else:
        _settings().delete_one({"user_id": user_id})
    return {"project_gid": project_gid}


def digest_user_ids() -> list[str]:
    return [d["user_id"] for d in _settings().find({"project_gid": {"$ne": None}})]


def get_digest_project(user_id: str) -> str | None:
    cfg = _settings().find_one({"user_id": user_id})
    return cfg.get("project_gid") if cfg else None


def _obj(props: dict | None = None, required: list[str] | None = None) -> dict:
    s = {"type": "object", "properties": props or {}}
    if required:
        s["required"] = required
    return s


_CONTACT_ITEM = {"type": "object", "properties": {"email": {"type": "string"}, "name": {"type": "string"}}, "required": ["email"]}
_MEMBERSHIP_ITEM = {"type": "object", "properties": {"provider": {"type": "string"}, "membership_id": {"type": "string"}}, "required": ["provider", "membership_id"]}

# (tool name, fn, description, schema, requires_confirm). One table drives
# both tools() and call(), so adding a tool is one row.
_NEW_TOOLS = [
    ("support_ops_outreach_collision_guard", outreach_collision_guard,
     "Contacts with an open Freshdesk ticket who are still active in an Outreach sequence or SalesLoft cadence. Returns the pause list.", _obj(), False),
    ("support_ops_pause_collisions", pause_collisions,
     "Pause the given Outreach sequence states / SalesLoft memberships (from outreach_collision_guard). Requires confirmation.",
     _obj({"memberships": {"type": "array", "items": _MEMBERSHIP_ITEM}}, ["memberships"]), True),
    ("support_ops_churn_risk_radar", churn_risk_radar,
     "Score accounts by open tickets, reopens, high priority and SLA breaches; flag those with an open CRM deal.", _obj(), False),
    ("support_ops_vip_ticket_escalator", vip_ticket_escalator,
     "Open tickets ranked with high-value accounts (open CRM deal, optional min_amount) above plain SLA order.",
     _obj({"min_amount": {"type": "number"}}), False),
    ("support_ops_ticket_to_task", ticket_to_task,
     "Create an Asana task for a Freshdesk ticket with a back-link, and note the task on the ticket. Requires confirmation.",
     _obj({"ticket_id": {"type": "integer"}, "project_gid": {"type": "string"}}, ["ticket_id", "project_gid"]), True),
    ("support_ops_task_links", task_links,
     "Tickets linked to Asana tasks with live completion state, plus open bug tickets with no task yet.", _obj(), False),
    ("support_ops_sync_completed_tasks", sync_completed_tasks,
     "For linked tasks completed in Asana, comment on the ticket (and optionally close it). Requires confirmation.",
     _obj({"close": {"type": "boolean"}}), True),
    ("support_ops_unlinked_requester_finder", unlinked_requester_finder,
     "Freshdesk requesters who match no contact in any connected CRM.", _obj(), False),
    ("support_ops_create_missing_contacts", create_missing_contacts,
     "Create contacts/leads for unlinked requesters in one CRM. Requires confirmation.",
     _obj({"provider": {"type": "string"}, "contacts": {"type": "array", "items": _CONTACT_ITEM}}, ["provider", "contacts"]), True),
    ("support_ops_outreach_reply_triage", outreach_reply_triage,
     "Tickets whose text shows buying interest (pricing, demo, upgrade...), with any active cadence for the requester.", _obj(), False),
    ("support_ops_repeated_issue_by_account", repeated_issue_by_account,
     "Cluster tickets by CRM company and fuzzy subject to show which accounts hit the same issue.", _obj(), False),
    ("support_ops_support_load_by_segment", support_load_by_segment,
     "Ticket volume, first-response and resolution time grouped by company, priority, type or source.",
     _obj({"group_by": {"type": "string", "enum": list(_GROUPS)}}), False),
    ("support_ops_stale_deal_vs_open_ticket", stale_deal_vs_open_ticket,
     "Open CRM deals idle for N days (default 14) whose account also has unresolved tickets.", _obj({"days": {"type": "integer"}}), False),
    ("support_ops_daily_digest", daily_digest,
     "SLA, collision and churn digest, with whether it changed since the last scheduled run.", _obj(), False),
    ("support_ops_customer_followup_gap_detector", customer_followup_gap_detector,
     "Open tickets and open CRM deals with no activity in N days (default 7) — a combined review queue for support and sales follow-up gaps.",
     _obj({"days": {"type": "integer"}}), False),
]
_FNS = {name: fn for name, fn, *_ in _NEW_TOOLS}


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
        ] + [
            ToolSpec(name=n, description=d, input_schema=sch, requires_confirm=confirm,
                     read_only_hint=not confirm, idempotent_hint=not confirm)
            for n, _fn, d, sch, confirm in _NEW_TOOLS
        ]

    def call(self, tool: str, args: dict, conn: Connection) -> dict:
        if tool == "support_ops_ticket_360":
            return ticket_360(conn.user_id, args["ticket_id"])
        if tool == "support_ops_sla_risk_monitor":
            return sla_risk_monitor(conn.user_id)
        if tool == "support_ops_repeated_issue_detector":
            return repeated_issue_detector(conn.user_id)
        if tool in _FNS:
            return _FNS[tool](conn.user_id, **args)
        raise ValueError(f"unknown tool: {tool}")

    def refresh(self, conn: Connection) -> Connection:
        return conn

    def verify(self, conn: Connection) -> None:
        pass


register(SupportOpsAdapter())
