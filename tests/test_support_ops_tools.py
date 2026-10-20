"""Logic checks for the support_ops cross-system tools: _load is stubbed with
a hand-built context, so no Freshdesk/CRM network calls. Needs local Mongo
only because importing support_ops imports the shared db module."""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
os.environ.setdefault("MONGO_URI", "mongodb://localhost:27017")
os.environ.setdefault("VAULT_MASTER_KEY", "QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE=")

import support_ops as so  # noqa: E402

NOW = datetime(2026, 1, 10, tzinfo=timezone.utc)


def iso(days: float) -> str:
    return (NOW + timedelta(days=days)).isoformat()


def ticket(i, subject, email="a@x.com", company="Acme", status=2, priority=1, due=1, updated=None, **stats):
    return {"id": i, "subject": subject, "status": status, "priority": priority, "due_by": iso(due),
            "updated_at": iso(updated) if updated is not None else None,
            "stats": stats, "_email": email, "_name": "", "_company": company, "created_at": iso(-1)}


def deal(account, amount=1000, last=-30, closed=False):
    return {"provider": "salesforce", "id": "d1", "name": "Big", "amount": amount, "stage": "Prospecting",
            "is_closed": closed, "account": account, "contact_ids": [], "last_activity": iso(last)}


def ctx(tickets, **extra):
    return {"tickets": tickets, "now": NOW, "skipped": {}, "contacts": {"hubspot": []}, "crm_emails": {"a@x.com"},
            "contact_by_id": {}, "deals": [], "memberships": [], **extra}


def stub(monkeypatch_target, c):
    so._load = lambda *a, **k: c


def test_fuzzy_cluster_and_signals():
    ts = [ticket(1, "Re: Login fails for user 123"), ticket(2, "Login fails for user 456"), ticket(3, "Billing question")]
    assert [len(m) for m in so._cluster(ts)] == [2]
    assert so._signals("What is your PRICING for 10 seats?") == ["pricing", "seats"]
    assert so._signals("hello") == []


def test_churn_flags_only_accounts_with_open_deal():
    stub(0, ctx([ticket(1, "a", due=-1, priority=4, reopened_at=iso(-1)), ticket(2, "b", company="Zed")],
                deals=[deal("acme")]))
    accts = so.churn_risk_radar("u")["accounts"]
    assert accts[0]["account"] == "Acme" and accts[0]["flagged"] and accts[0]["revenue_at_risk"] == 1000
    assert accts[0]["score"] == 1 + 3 + 2 + 3  # open + reopen + high prio + breach
    assert not accts[1]["flagged"]


def test_vip_ranked_above_more_urgent_non_vip():
    stub(0, ctx([ticket(1, "urgent free user", company="Zed", due=-5), ticket(2, "vip", due=3)], deals=[deal("acme")]))
    assert [t["ticket_id"] for t in so.vip_ticket_escalator("u")["tickets"]] == [2, 1]


def test_stale_deal_needs_open_ticket_and_idle():
    stub(0, ctx([ticket(1, "x")], deals=[deal("acme", last=-30), deal("acme", last=-1), deal("acme", closed=True)]))
    out = so.stale_deal_vs_open_ticket("u", 14)["stuck_deals"]
    assert len(out) == 1 and out[0]["idle_days"] >= 30


def test_collision_only_active_memberships_with_open_ticket():
    c = {"provider": "outreach", "id": "p1", "email": "a@x.com", "name": "", "company": "", "phone": ""}
    mems = [{"provider": "outreach", "id": "s1", "contact_id": "p1", "active": True, "state": "active"},
            {"provider": "outreach", "id": "s2", "contact_id": "p1", "active": False, "state": "paused"}]
    stub(0, ctx([ticket(1, "x"), ticket(2, "closed", status=5, email="b@x.com")], contact_by_id={("outreach", "p1"): c}, memberships=mems))
    out = so.outreach_collision_guard("u")["collisions"]
    assert len(out) == 1 and [m["membership_id"] for m in out[0]["memberships"]] == ["s1"]


def test_unlinked_requesters_and_load_by_segment():
    stub(0, ctx([ticket(1, "x"), ticket(2, "y", email="new@x.com", company=""), ticket(3, "z", email="new@x.com", company="")]))
    r = so.unlinked_requester_finder("u")["requesters"]
    assert r == [{"email": "new@x.com", "name": "", "ticket_count": 2}]
    seg = so.support_load_by_segment("u", "company")["segments"]
    assert {s["segment"]: s["tickets"] for s in seg} == {"Acme": 1, "(no CRM company)": 2}
    assert "error" in so.support_load_by_segment("u", "bogus")


def test_followup_gap_flags_idle_open_items_only():
    stub(0, ctx(
        [ticket(1, "stale open", updated=-10), ticket(2, "fresh open", updated=-1),
         ticket(3, "stale but closed", status=5, updated=-10), ticket(4, "no updated_at ever")],
        deals=[deal("acme", last=-10), deal("beta", last=-1), deal("gamma", last=-10, closed=True)],
    ))
    out = so.customer_followup_gap_detector("u", days=7)
    stale_ids = {t["ticket_id"] for t in out["stale_tickets"]}
    assert stale_ids == {1, 4}
    assert {d["account"] for d in out["stale_deals"]} == {"acme"}


def test_confirm_gated_tools_are_marked():
    gated = {s.name for s in so.SupportOpsAdapter().tools() if s.requires_confirm}
    assert gated == {"support_ops_pause_collisions", "support_ops_ticket_to_task",
                     "support_ops_sync_completed_tasks", "support_ops_create_missing_contacts"}


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("all checks passed")
