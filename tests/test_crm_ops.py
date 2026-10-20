"""Unit tests for crm_ops.py's duplicate-detection group shape. _group_result
is stubbed with hand-built normalized contacts (same shape as
crm_contacts._norm), no Mongo/network needed — fetch_all_contacts is
monkeypatched directly.

Covers the fix for a real bug: find_duplicates_fuzzy put a lowercased NAME
in the "email" field for fuzzy-only groups (those cluster members were, by
construction, never matched on email), which the UI then rendered under an
email-styled label. Renamed to "group_key" so callers can't mistake a
fuzzy group's matched name for an address.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import crm_ops  # noqa: E402


def contact(provider, id, name="", email="", phone="", company="", last_activity=None):
    return {"provider": provider, "id": id, "name": name, "email": email, "phone": phone,
            "company": company, "last_activity": last_activity, "raw": {}}


def test_find_duplicates_group_key_is_the_real_email(monkeypatch):
    by_provider = {
        "hubspot": [contact("hubspot", "1", name="Kush Goel", email="kush@x.com")],
        "salesforce": [contact("salesforce", "2", name="Kush Goel", email="kush@x.com")],
    }
    monkeypatch.setattr(crm_ops, "fetch_all_contacts", lambda uid: (by_provider, {}))
    out = crm_ops.find_duplicates("u")
    assert len(out["duplicate_groups"]) == 1
    assert out["duplicate_groups"][0]["group_key"] == "kush@x.com"
    assert "email" not in out["duplicate_groups"][0]


def test_find_duplicates_fuzzy_group_key_is_the_matched_name_not_an_email(monkeypatch):
    """The exact scenario the bug report matched: two contacts with
    DIFFERENT emails, same name + phone, caught only by the fuzzy pass."""
    by_provider = {
        "hubspot": [contact("hubspot", "1", name="Jamie Carter", email="jamie@northwind.com", phone="5551234567")],
        "salesforce": [contact("salesforce", "2", name="Jaime Carter", email="j.carter@different.com", phone="5551234567")],
    }
    monkeypatch.setattr(crm_ops, "fetch_all_contacts", lambda uid: (by_provider, {}))
    out = crm_ops.find_duplicates_fuzzy("u")
    assert len(out["duplicate_groups"]) == 1
    group = out["duplicate_groups"][0]
    assert group["confidence"] == "high"  # fuzzy name + phone match
    # group_key is the seed member's lowercased name — correct, documented
    # behavior now that it's no longer mislabeled as "email".
    assert group["group_key"] == "jamie carter"
    assert "email" not in group
    emails_in_group = {m.get("email") for m in group["members"]}
    assert emails_in_group == {None}  # members dict never carried email to begin with — name/company only


def test_find_duplicates_fuzzy_includes_exact_matches_too(monkeypatch):
    by_provider = {
        "hubspot": [contact("hubspot", "1", name="A", email="a@x.com")],
        "zoho": [contact("zoho", "2", name="A", email="a@x.com")],
    }
    monkeypatch.setattr(crm_ops, "fetch_all_contacts", lambda uid: (by_provider, {}))
    out = crm_ops.find_duplicates_fuzzy("u")
    assert len(out["duplicate_groups"]) == 1
    assert out["duplicate_groups"][0]["confidence"] == "exact"
    assert out["duplicate_groups"][0]["group_key"] == "a@x.com"


if __name__ == "__main__":
    import types
    _mp = types.SimpleNamespace(setattr=setattr)
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(_mp)
    print("all checks passed")
