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


def _norm(provider: str, record_id, name: str | None, email: str | None, phone: str | None, company: str | None, raw: dict) -> dict:
    return {
        "provider": provider,
        "id": str(record_id) if record_id is not None else None,
        "name": (name or "").strip(),
        "email": (email or "").strip().lower(),
        "phone": "".join(ch for ch in (phone or "") if ch.isdigit()),
        "company": (company or "").strip(),
        "raw": raw,
    }


def _fetch_hubspot(conn: Connection) -> list[dict]:
    token = conn.credentials["access_token"]
    raw = hs._request(
        "GET", token, "/crm/v3/objects/contacts",
        params={"limit": _HUBSPOT_FETCH_LIMIT, "properties": "firstname,lastname,email,phone,company"},
    ).json().get("results", [])
    out = []
    for r in raw:
        props = r.get("properties", {})
        name = " ".join(p for p in [props.get("firstname"), props.get("lastname")] if p)
        out.append(_norm("hubspot", r.get("id"), name, props.get("email"), props.get("phone"), props.get("company"), r))
    return out


def _fetch_salesforce(conn: Connection) -> list[dict]:
    instance_url = conn.credentials["instance_url"]
    token = conn.credentials["access_token"]
    soql = soql_guard.validate_and_cap(f"SELECT Id, Name, Email, Phone, Account.Name FROM Contact LIMIT {_FETCH_LIMIT}")
    raw = sf.query_soql(instance_url, token, soql)
    out = []
    for r in raw:
        account = r.get("Account")
        company = account.get("Name") if isinstance(account, dict) else None
        out.append(_norm("salesforce", r.get("Id"), r.get("Name"), r.get("Email"), r.get("Phone"), company, r))
    return out


def _fetch_zoho(conn: Connection) -> list[dict]:
    host = _zoho_host(conn.credentials["dc"])
    token = conn.credentials["access_token"]
    # COQL requires a WHERE clause (unlike SQL) — "id is not null" matches every record.
    raw = zh.query_coql(host, token, f"SELECT id, Full_Name, Email, Phone, Account_Name FROM Contacts WHERE id is not null LIMIT {_FETCH_LIMIT}")
    out = []
    for r in raw:
        account_name = r.get("Account_Name")
        company = account_name.get("name") if isinstance(account_name, dict) else account_name
        out.append(_norm("zoho", r.get("id"), r.get("Full_Name"), r.get("Email"), r.get("Phone"), company, r))
    return out


def _fetch_outreach(conn: Connection) -> list[dict]:
    token = conn.credentials["access_token"]
    raw = oc.list_resource(token, "prospects")
    out = []
    for r in raw[:_FETCH_LIMIT]:
        attrs = r.get("attributes", {})
        name = " ".join(p for p in [attrs.get("firstName"), attrs.get("lastName")] if p)
        out.append(_norm("outreach", r.get("id"), name, attrs.get("email"), attrs.get("homePhone") or attrs.get("mobilePhone"), attrs.get("company"), r))
    return out


def _fetch_salesloft(conn: Connection) -> list[dict]:
    token = conn.credentials["access_token"]
    raw = sl.list_resource(token, "people", {"per_page": _FETCH_LIMIT})
    out = []
    for r in raw:
        name = " ".join(p for p in [r.get("first_name"), r.get("last_name")] if p)
        account = r.get("account") or {}
        out.append(_norm("salesloft", r.get("id"), name, r.get("email_address"), r.get("phone"), account.get("name"), r))
    return out


_FETCHERS = {
    "hubspot": _fetch_hubspot,
    "salesforce": _fetch_salesforce,
    "zoho": _fetch_zoho,
    "outreach": _fetch_outreach,
    "salesloft": _fetch_salesloft,
}


def fetch_all_contacts(user_id: str) -> tuple[dict[str, list[dict]], dict[str, str]]:
    """Normalized contacts per connected CRM provider for this user, plus a
    second dict of {provider: reason} for any connected provider that was
    skipped (not active, or its fetch raised) — so a caller can tell "zero
    duplicates because nothing's actually wrong" apart from "zero duplicates
    because HubSpot's fetch silently failed," instead of both looking the
    same."""
    def _fetch_one(provider: str) -> tuple[str, list[dict] | None, str | None]:
        conn_row = connections.get_connection(user_id, provider)
        if not conn_row:
            return provider, None, None
        if conn_row["status"] != "active":
            return provider, None, f"connection status is {conn_row['status']} — reconnect from Connected accounts"
        conn = Connection(
            user_id=user_id, provider=provider, auth_type=conn_row["auth_type"],
            credentials=conn_row["credentials"], scopes=conn_row.get("scopes", []), expires_at=conn_row.get("expires_at"),
        )
        adapter = adapters.ADAPTERS[provider]

        # Same proactive+reactive refresh mcp_http_server.py's real tool
        # dispatch already does (oauth_refresh.py) — without this, a token
        # that's simply expired looks identical to a genuine fetch failure,
        # and silently drops that provider from every cross-CRM result.
        if conn.auth_type == "oauth2" and conn.expires_at and time.time() > conn.expires_at - 120:
            try:
                conn = oauth_refresh.refresh_and_persist(adapter, conn, provider)
            except Exception as exc:
                connections.mark_needs_reauth(user_id, provider)
                return provider, None, f"session expired and could not be refreshed: {exc}"

        try:
            return provider, _FETCHERS[provider](conn), None
        except Exception as exc:
            if conn.auth_type == "oauth2" and oauth_refresh.looks_like_auth_error(exc):
                try:
                    conn = oauth_refresh.refresh_and_persist(adapter, conn, provider)
                    return provider, _FETCHERS[provider](conn), None
                except Exception as retry_exc:
                    connections.mark_needs_reauth(user_id, provider)
                    return provider, None, f"session expired and could not be refreshed: {retry_exc}"
            # ponytail: skip a provider whose fetch fails (API error, not an
            # auth error) rather than failing the whole cross-CRM analysis —
            # the error itself is returned for the UI to show, not swallowed.
            return provider, None, str(exc)

    out = {}
    errors = {}
    # Each provider is an independent, blocking network round-trip — fetching
    # them one after another stacks their latencies (5 providers = 5x the
    # slowest one). Run them concurrently instead; ThreadPoolExecutor because
    # every call here (httpx, pymongo) is blocking I/O, not CPU-bound.
    with ThreadPoolExecutor(max_workers=len(CRM_PROVIDERS)) as pool:
        for provider, contacts, error in pool.map(_fetch_one, CRM_PROVIDERS):
            if error:
                errors[provider] = error
            elif contacts is not None:
                out[provider] = contacts
    return out, errors
