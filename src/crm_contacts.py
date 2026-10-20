"""Cross-CRM contact fetch + normalization — the shared foundation for
crm_ops.py's analysis tools (duplicate detection, data-quality audit,
customer 360). One normalized shape per contact regardless of provider:
{provider, id, name, email, phone, company, raw}.

Only providers that actually hold contact/prospect records participate
(hubspot, salesforce, zoho, outreach, salesloft) — Freshdesk (tickets) and
Asana (tasks) have no equivalent object and are skipped.

Each provider's own client already has a list call (hs._request,
sf.query_soql, zh.query_coql, oc.list_resource, sl.list_resource) — reused
here unchanged, same decrypted credentials connections.get_connection
already produces. This module only adds normalization and the per-user
"fetch everything I'm connected to" loop.
"""
import time
from concurrent.futures import ThreadPoolExecutor

try:
    import adapters
    import connections
    import hubspot_client as hs
    import oauth_refresh
    import outreach_client as oc
    import salesforce_client as sf
    import salesloft_client as sl
    import soql_guard
    import zoho_client as zh
    from adapters import Connection
    from adapters.zoho import _resolve_host as _zoho_host
except ImportError:
    from src import adapters
    from src import connections
    from src import hubspot_client as hs
    from src import oauth_refresh
    from src import outreach_client as oc
    from src import salesforce_client as sf
    from src import salesloft_client as sl
    from src import soql_guard
    from src import zoho_client as zh
    from src.adapters import Connection
    from src.adapters.zoho import _resolve_host as _zoho_host

CRM_PROVIDERS = ["hubspot", "salesforce", "zoho", "outreach", "salesloft"]

_FETCH_LIMIT = 200
_HUBSPOT_FETCH_LIMIT = 100  # HubSpot's contacts endpoint caps at 100/request, unlike other providers' 200


def _norm(provider: str, record_id, name: str | None, email: str | None, phone: str | None, company: str | None, raw: dict, last_activity: str | None = None) -> dict:
    return {
        "provider": provider,
        "id": str(record_id) if record_id is not None else None,
        "name": (name or "").strip(),
        "email": (email or "").strip().lower(),
        "phone": "".join(ch for ch in (phone or "") if ch.isdigit()),
        "company": (company or "").strip(),
        "last_activity": last_activity,
        "raw": raw,
    }


def _fetch_hubspot(conn: Connection) -> list[dict]:
    token = conn.credentials["access_token"]
    raw = hs._request(
        "GET", token, "/crm/v3/objects/contacts",
        params={"limit": _HUBSPOT_FETCH_LIMIT, "properties": "firstname,lastname,email,phone,company,notes_last_updated,lastmodifieddate"},
    ).json().get("results", [])
    out = []
    for r in raw:
        props = r.get("properties", {})
        name = " ".join(p for p in [props.get("firstname"), props.get("lastname")] if p)
        out.append(_norm("hubspot", r.get("id"), name, props.get("email"), props.get("phone"), props.get("company"), r,
                          props.get("notes_last_updated") or props.get("lastmodifieddate")))
    return out


def _fetch_salesforce(conn: Connection) -> list[dict]:
    instance_url = conn.credentials["instance_url"]
    token = conn.credentials["access_token"]
    soql = soql_guard.validate_and_cap(f"SELECT Id, Name, Email, Phone, Account.Name, LastActivityDate, LastModifiedDate FROM Contact LIMIT {_FETCH_LIMIT}")
    raw = sf.query_soql(instance_url, token, soql)
    out = []
    for r in raw:
        account = r.get("Account")
        company = account.get("Name") if isinstance(account, dict) else None
        out.append(_norm("salesforce", r.get("Id"), r.get("Name"), r.get("Email"), r.get("Phone"), company, r,
                          r.get("LastActivityDate") or r.get("LastModifiedDate")))
    return out


def _fetch_zoho(conn: Connection) -> list[dict]:
    host = _zoho_host(conn.credentials["dc"])
    token = conn.credentials["access_token"]
    # COQL requires a WHERE clause (unlike SQL) — "id is not null" matches every record.
    raw = zh.query_coql(host, token, f"SELECT id, Full_Name, Email, Phone, Account_Name, Last_Activity_Time, Modified_Time FROM Contacts WHERE id is not null LIMIT {_FETCH_LIMIT}")
    out = []
    for r in raw:
        account_name = r.get("Account_Name")
        company = account_name.get("name") if isinstance(account_name, dict) else account_name
        out.append(_norm("zoho", r.get("id"), r.get("Full_Name"), r.get("Email"), r.get("Phone"), company, r,
                          r.get("Last_Activity_Time") or r.get("Modified_Time")))
    return out


def _fetch_outreach(conn: Connection) -> list[dict]:
    token = conn.credentials["access_token"]
    raw = oc.list_resource(token, "prospects")
    out = []
    for r in raw[:_FETCH_LIMIT]:
        attrs = r.get("attributes", {})
        name = " ".join(p for p in [attrs.get("firstName"), attrs.get("lastName")] if p)
        out.append(_norm("outreach", r.get("id"), name, attrs.get("email"), attrs.get("homePhone") or attrs.get("mobilePhone"), attrs.get("company"), r,
                          attrs.get("updatedAt")))
    return out


def _fetch_salesloft(conn: Connection) -> list[dict]:
    token = conn.credentials["access_token"]
    raw = sl.list_resource(token, "people", {"per_page": _FETCH_LIMIT})
    out = []
    for r in raw:
        name = " ".join(p for p in [r.get("first_name"), r.get("last_name")] if p)
        account = r.get("account") or {}
        out.append(_norm("salesloft", r.get("id"), name, r.get("email_address"), r.get("phone"), account.get("name"), r,
                          r.get("updated_at")))
    return out


_FETCHERS = {
    "hubspot": _fetch_hubspot,
    "salesforce": _fetch_salesforce,
    "zoho": _fetch_zoho,
    "outreach": _fetch_outreach,
    "salesloft": _fetch_salesloft,
}


def get_fresh_conn(user_id: str, provider: str) -> tuple[Connection | None, str | None]:
    """Active connection for (user, provider), token proactively refreshed if
    near expiry. Returns (conn, None), (None, None) if not connected, or
    (None, reason) if inactive/unrefreshable."""
    conn_row = connections.get_connection(user_id, provider)
    if not conn_row:
        return None, None
    if conn_row["status"] != "active":
        return None, f"connection status is {conn_row['status']} — reconnect from Connected accounts"
    conn = Connection(
        user_id=user_id, provider=provider, auth_type=conn_row["auth_type"],
        credentials=conn_row["credentials"], scopes=conn_row.get("scopes", []), expires_at=conn_row.get("expires_at"),
    )
    # Proactive refresh, same as mcp_http_server.py's dispatch — without it an
    # expired token looks identical to a genuine fetch failure.
    if conn.auth_type == "oauth2" and conn.expires_at and time.time() > conn.expires_at - 120:
        try:
            conn = oauth_refresh.refresh_and_persist(adapters.ADAPTERS[provider], conn, provider)
        except Exception as exc:
            connections.mark_needs_reauth(user_id, provider)
            return None, f"session expired and could not be refreshed: {exc}"
    return conn, None


def fetch_all(user_id: str, fetchers: dict) -> tuple[dict[str, list[dict]], dict[str, str]]:
    """Run `fetchers` ({provider: fn(conn) -> list[dict]}) concurrently for
    every provider this user has connected. Returns ({provider: rows},
    {provider: skip_reason}) so "nothing found" is distinguishable from
    "fetch silently failed"."""
    def _fetch_one(provider: str) -> tuple[str, list[dict] | None, str | None]:
        conn, err = get_fresh_conn(user_id, provider)
        if not conn:
            return provider, None, err
        try:
            return provider, fetchers[provider](conn), None
        except Exception as exc:
            if conn.auth_type == "oauth2" and oauth_refresh.looks_like_auth_error(exc):
                try:
                    conn = oauth_refresh.refresh_and_persist(adapters.ADAPTERS[provider], conn, provider)
                    return provider, fetchers[provider](conn), None
                except Exception as retry_exc:
                    connections.mark_needs_reauth(user_id, provider)
                    return provider, None, f"session expired and could not be refreshed: {retry_exc}"
            # ponytail: skip a provider whose fetch fails (API error, not an
            # auth error) rather than failing the whole cross-CRM analysis —
            # the error itself is returned for the UI to show, not swallowed.
            return provider, None, str(exc)

    out, errors = {}, {}
    # Independent blocking network round-trips: run concurrently, not 5x serial.
    with ThreadPoolExecutor(max_workers=max(len(fetchers), 1)) as pool:
        for provider, rows, error in pool.map(_fetch_one, list(fetchers)):
            if error:
                errors[provider] = error
            elif rows is not None:
                out[provider] = rows
    return out, errors


def fetch_all_contacts(user_id: str) -> tuple[dict[str, list[dict]], dict[str, str]]:
    """Normalized contacts per connected CRM provider, plus {provider: reason}
    for any connected provider that was skipped."""
    return fetch_all(user_id, _FETCHERS)


# --- Deals (open pipeline) -------------------------------------------------
# Normalized: {provider, id, name, amount, stage, is_closed, account,
# contact_ids, last_activity (ISO str|None), raw}. `account` is the company
# name used to join to Freshdesk requesters; HubSpot deals carry no company
# inline, so they come with contact_ids and the caller resolves via contacts.

def _deal(provider, rid, name, amount, stage, is_closed, account, contact_ids, last_activity, raw) -> dict:
    try:
        amount = float(amount) if amount not in (None, "") else 0.0
    except (TypeError, ValueError):
        amount = 0.0
    return {
        "provider": provider, "id": str(rid) if rid is not None else None, "name": name or "",
        "amount": amount, "stage": stage or "", "is_closed": bool(is_closed),
        "account": (account or "").strip(), "contact_ids": [str(c) for c in contact_ids],
        "last_activity": last_activity, "raw": raw,
    }


def _deals_hubspot(conn: Connection) -> list[dict]:
    raw = hs._request(
        "GET", conn.credentials["access_token"], "/crm/v3/objects/deals",
        params={"limit": _HUBSPOT_FETCH_LIMIT, "associations": "contacts",
                "properties": "dealname,amount,dealstage,hs_is_closed,notes_last_updated,hs_lastmodifieddate"},
    ).json().get("results", [])
    out = []
    for r in raw:
        p = r.get("properties", {})
        assoc = (r.get("associations") or {}).get("contacts", {}).get("results", [])
        out.append(_deal("hubspot", r.get("id"), p.get("dealname"), p.get("amount"), p.get("dealstage"),
                         str(p.get("hs_is_closed")).lower() == "true", "", [a["id"] for a in assoc],
                         p.get("notes_last_updated") or p.get("hs_lastmodifieddate"), r))
    return out


def _deals_salesforce(conn: Connection) -> list[dict]:
    soql = soql_guard.validate_and_cap(
        f"SELECT Id, Name, Amount, StageName, IsClosed, Account.Name, LastActivityDate, LastModifiedDate FROM Opportunity LIMIT {_FETCH_LIMIT}")
    out = []
    for r in sf.query_soql(conn.credentials["instance_url"], conn.credentials["access_token"], soql):
        acct = r.get("Account")
        out.append(_deal("salesforce", r.get("Id"), r.get("Name"), r.get("Amount"), r.get("StageName"), r.get("IsClosed"),
                         acct.get("Name") if isinstance(acct, dict) else None, [],
                         r.get("LastActivityDate") or r.get("LastModifiedDate"), r))
    return out


def _deals_zoho(conn: Connection) -> list[dict]:
    raw = zh.query_coql(
        _zoho_host(conn.credentials["dc"]), conn.credentials["access_token"],
        f"SELECT id, Deal_Name, Amount, Stage, Account_Name, Last_Activity_Time, Modified_Time FROM Deals WHERE id is not null LIMIT {_FETCH_LIMIT}")
    out = []
    for r in raw:
        acct = r.get("Account_Name")
        stage = r.get("Stage") or ""
        out.append(_deal("zoho", r.get("id"), r.get("Deal_Name"), r.get("Amount"), stage, stage.lower().startswith("closed"),
                         acct.get("name") if isinstance(acct, dict) else acct, [],
                         r.get("Last_Activity_Time") or r.get("Modified_Time"), r))
    return out


_DEAL_FETCHERS = {"hubspot": _deals_hubspot, "salesforce": _deals_salesforce, "zoho": _deals_zoho}


def fetch_all_deals(user_id: str) -> tuple[dict[str, list[dict]], dict[str, str]]:
    return fetch_all(user_id, _DEAL_FETCHERS)


# --- Sequence / cadence memberships (Outreach, SalesLoft) -------------------
# Normalized: {provider, id (membership id), contact_id, active, state}.

def _memberships_outreach(conn: Connection) -> list[dict]:
    out = []
    for r in oc.list_resource(conn.credentials["access_token"], "sequenceStates"):
        state = (r.get("attributes") or {}).get("state")
        prospect = ((r.get("relationships") or {}).get("prospect") or {}).get("data") or {}
        out.append({"provider": "outreach", "id": str(r.get("id")), "contact_id": str(prospect.get("id")),
                    "active": state == "active", "state": state})
    return out


def _memberships_salesloft(conn: Connection) -> list[dict]:
    out = []
    for r in sl.list_resource(conn.credentials["access_token"], "cadence_memberships", {"per_page": _FETCH_LIMIT}):
        person = r.get("person") or {}
        active = bool(r.get("currently_on_cadence")) and not r.get("paused")
        out.append({"provider": "salesloft", "id": str(r.get("id")), "contact_id": str(person.get("id")),
                    "active": active, "state": "active" if active else "inactive"})
    return out


_MEMBERSHIP_FETCHERS = {"outreach": _memberships_outreach, "salesloft": _memberships_salesloft}


def fetch_all_memberships(user_id: str) -> tuple[dict[str, list[dict]], dict[str, str]]:
    return fetch_all(user_id, _MEMBERSHIP_FETCHERS)
