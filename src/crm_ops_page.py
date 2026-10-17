"""CRM Ops page: visual view of the cross-CRM analysis tools in crm_ops.py
(duplicate detection, data-quality audit, customer 360 lookup) — the same
three things an agent can call over MCP, run live and rendered as HTML
instead of raw JSON, for a hub user who isn't going through an agent.

Mounted the same way connected_accounts.py/provider_detail.py are, behind
the hub-login gate, reusing their _page_shell so it reads as one product.
"""
import html

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

try:
    import auth
    import connections
    import crm_contacts
    import crm_ops
    from connected_accounts import PROVIDER_DISPLAY_NAMES, _page_shell
    from login_pages import SESSION_COOKIE_NAME
except ImportError:
    from src import auth
    from src import connections
    from src import crm_contacts
    from src import crm_ops
    from src.connected_accounts import PROVIDER_DISPLAY_NAMES, _page_shell
    from src.login_pages import SESSION_COOKIE_NAME

router = APIRouter()

_OPS_STYLES = """
    .ops-tabs { display:flex; gap:6px; margin-bottom:24px; border-bottom:1px solid var(--border); }
    .ops-tab { padding:9px 14px; font-size:13px; font-weight:500; color:var(--muted); text-decoration:none; border-bottom:2px solid transparent; margin-bottom:-1px; }
    .ops-tab.active { color:var(--text); border-bottom-color:var(--text); }
    .ops-empty { padding:28px 18px; text-align:center; color:var(--muted); font-size:13px; border:1px solid var(--border); border-radius:10px; background:var(--card); }
    .dup-group { border:1px solid var(--border); border-radius:10px; padding:14px 16px; margin-bottom:10px; background:var(--card); }
    .dup-email { font-family:"JetBrains Mono", ui-monospace, monospace; font-size:12.5px; font-weight:600; margin-bottom:8px; }
    .dup-member { display:flex; align-items:center; gap:8px; font-size:13px; padding:4px 0; }
    .dup-provider-tag { font-size:10.5px; font-weight:600; padding:2px 7px; border-radius:5px; background:var(--border-soft); color:var(--muted); min-width:72px; text-align:center; }
    .dup-provider-tag.primary { background:var(--success-bg); color:var(--success); }
    .dup-conflict { font-size:12px; color:var(--danger); margin-top:8px; }
    .qa-provider-row { border:1px solid var(--border); border-radius:10px; padding:14px 16px; margin-bottom:10px; background:var(--card); }
    .qa-score { font-size:20px; font-weight:700; }
    .qa-score.ok { color:var(--success); }
    .qa-score.warn { color:var(--danger); }
    .qa-info { display:inline-flex; align-items:center; justify-content:center; width:14px; height:14px; border-radius:50%; border:1px solid var(--muted); color:var(--muted); font-size:10px; font-weight:600; margin-left:6px; cursor:help; vertical-align:middle; }
    .qa-issue-row { display:flex; justify-content:space-between; font-size:12.5px; padding:5px 0; border-top:1px solid var(--border-soft); }
    .sev-badge { font-size:10px; font-weight:600; padding:1px 6px; border-radius:5px; }
    .sev-badge.high { background:var(--danger-bg); color:var(--danger); }
    .sev-badge.medium { background:var(--border-soft); color:var(--muted); }
    .sev-badge.low { background:var(--border-soft); color:var(--muted-soft); }
    .lookup-form { display:flex; gap:8px; margin-bottom:24px; }
    .lookup-form input { flex:1 1 auto; padding:9px 12px; border:1px solid var(--border); border-radius:8px; background:var(--bg); color:var(--text); font-family:inherit; font-size:13px; }
    .lookup-form button { padding:9px 16px; border:none; border-radius:8px; background:var(--accent); color:var(--accent-text); font-size:13px; font-weight:500; cursor:pointer; font-family:inherit; }
    .r360-record { border:1px solid var(--border); border-radius:10px; padding:14px 16px; margin-bottom:10px; background:var(--card); }
"""


def _provider_name(key: str) -> str:
    return PROVIDER_DISPLAY_NAMES.get(key, key)


def _tabs_html(active: str) -> str:
    tabs = [("duplicates", "Duplicate detection"), ("quality", "Data quality"), ("360", "Customer 360")]
    return '<div class="ops-tabs">' + "".join(
        f'<a href="/crm-ops/{key}" class="ops-tab {"active" if key == active else ""}">{html.escape(label)}</a>'
        for key, label in tabs
    ) + "</div>"


def _connected_crm_count(user_id: str) -> int:
    return sum(1 for p in crm_contacts.CRM_PROVIDERS if connections.get_connection(user_id, p))


def _no_crm_connected_html() -> str:
    return '<div class="ops-empty">Connect at least one CRM (HubSpot, Salesforce, Zoho, Outreach, or SalesLoft) from <a href="/connected-accounts">Connected accounts</a> to run cross-CRM analysis.</div>'


def _skipped_html(providers_skipped: dict[str, str]) -> str:
    if not providers_skipped:
        return ""
    lines = "".join(
        f'<div>{html.escape(_provider_name(p))}: {html.escape(reason)}</div>'
        for p, reason in providers_skipped.items()
    )
    return f'<div class="dup-conflict" style="margin-bottom:16px;">⚠ Not included in this result —<br>{lines}</div>'


def _duplicates_body(user_id: str) -> str:
    if _connected_crm_count(user_id) == 0:
        return _no_crm_connected_html()
    result = crm_ops.find_duplicates(user_id)
    checked = ", ".join(_provider_name(p) for p in result["providers_checked"]) or "none"
    header = f'<div class="page-sub" style="margin-bottom:20px;">Checked {result["total_contacts_checked"]} contacts across {html.escape(checked)}.</div>' + _skipped_html(result["providers_skipped"])
    if not result["duplicate_groups"]:
        return header + '<div class="ops-empty">No cross-CRM duplicates found.</div>'
    groups_html = []
    for g in result["duplicate_groups"]:
        members_html = "".join(
            f'<div class="dup-member"><span class="dup-provider-tag {"primary" if m["provider"] == g["recommended_primary"] else ""}">{html.escape(_provider_name(m["provider"]))}</span>'
            f'<span>{html.escape(m["name"] or "(no name)")}</span>'
            f'<span style="color:var(--muted); font-size:12px;">{html.escape(m["company"] or "")}</span></div>'
            for m in g["members"]
        )
        conflicts_html = ""
        if g["conflicting_fields"]:
            conflict_lines = ", ".join(f"{field} differs" for field in g["conflicting_fields"])
            conflicts_html = f'<div class="dup-conflict">⚠ {html.escape(conflict_lines)}</div>'
        groups_html.append(f'<div class="dup-group"><div class="dup-email">{html.escape(g["email"])}</div>{members_html}{conflicts_html}</div>')
    return header + "".join(groups_html)


def _quality_body(user_id: str) -> str:
    if _connected_crm_count(user_id) == 0:
        return _no_crm_connected_html()
    result = crm_ops.data_quality_audit(user_id)
    skipped_banner = _skipped_html(result["providers_skipped"])
    if not result["results"]:
        return skipped_banner + '<div class="ops-empty">No connected CRM has any contacts to audit.</div>'
    rows = [skipped_banner]
    for provider, data in result["results"].items():
        score = data["quality_score"]
        score_title = "Percent of contacts with no missing/conflicting fields. Higher is better."
        score_html = (
            f'<span class="qa-score {"ok" if score is None or score >= 80 else "warn"}">'
            f'{score if score is not None else "—"}{"%" if score is not None else ""}'
            f'<span class="qa-info" title="{html.escape(score_title)}">i</span></span>'
        )
        flagged_html = "".join(
            f'<div class="qa-issue-row"><span>{html.escape(f["name"] or "(no name)")} — {html.escape(", ".join(f["issues"]))}</span><span class="sev-badge {f["severity"]}">{f["severity"]}</span></div>'
            for f in data["flagged"][:25]
        )
        more = f'<div class="qa-issue-row" style="color:var(--muted);">+{len(data["flagged"]) - 25} more</div>' if len(data["flagged"]) > 25 else ""
        rows.append(f"""
        <div class="qa-provider-row">
          <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;">
            <div style="font-weight:600; font-size:14px;">{html.escape(_provider_name(provider))}</div>
            {score_html}
          </div>
          <div style="font-size:12.5px; color:var(--muted); margin-bottom:6px;">{data["total_contacts"]} contacts · {data["flagged_count"]} flagged</div>
          {flagged_html}{more}
        </div>
        """)
    return "".join(rows)


def _r360_body(user_id: str, email: str | None) -> str:
    form = f"""
    <form class="lookup-form" method="get" action="/crm-ops/360">
      <input type="text" name="email" placeholder="person@company.com" value="{html.escape(email or "")}" required>
      <button type="submit">Look up</button>
    </form>
    """
    if not email:
        return form
    if _connected_crm_count(user_id) == 0:
        return form + _no_crm_connected_html()
    result = crm_ops.customer_360(user_id, email)
    skipped_banner = _skipped_html(result["providers_skipped"])
    if not result["records"]:
        return form + skipped_banner + f'<div class="ops-empty">No matching contact found for {html.escape(email)} in any connected CRM.</div>'
    records_html = "".join(f"""
        <div class="r360-record">
          <div style="display:flex; align-items:center; gap:10px; margin-bottom:6px;">
            <span class="dup-provider-tag primary">{html.escape(_provider_name(r["provider"]))}</span>
            <span style="font-weight:600;">{html.escape(r["name"] or "(no name)")}</span>
          </div>
          <div style="font-size:12.5px; color:var(--muted);">{html.escape(r["company"] or "no company")} · {html.escape(r["phone"] or "no phone")}</div>
        </div>
    """ for r in result["records"])
    return form + skipped_banner + f'<div class="page-sub" style="margin-bottom:16px;">Found in {len(result["found_in"])} of {len(result["providers_checked"])} connected CRMs.</div>' + records_html


def _ops_page(active: str, hub_email: str, body: str) -> str:
    title = {"duplicates": "Duplicate detection", "quality": "Data quality audit", "360": "Customer 360"}[active]
    full_body = f"""
      <div style="margin-bottom:8px;">
        <h1 class="page-title">CRM Ops</h1>
        <div class="page-sub">Cross-CRM analysis across your connected HubSpot, Salesforce, Zoho, Outreach, and SalesLoft accounts.</div>
      </div>
      {_tabs_html(active)}
      {body}
    """
    return _page_shell(f"{title} — CRM Ops", hub_email, full_body, active_nav="crm-ops").replace("</style>", _OPS_STYLES + "</style>")


@router.get("/crm-ops")
def crm_ops_index(request: Request):
    return RedirectResponse("/crm-ops/duplicates", status_code=302)


@router.get("/crm-ops/{tab}")
def crm_ops_tab(tab: str, request: Request, email: str | None = Query(None)):
    user = auth.resolve_session(request.cookies.get(SESSION_COOKIE_NAME))
    if not user:
        return RedirectResponse(f"/login?next=/crm-ops/{tab}", status_code=302)

    if tab == "360":
        body = _r360_body(user.user_id, email)
    elif tab == "duplicates":
        body = _duplicates_body(user.user_id)
    elif tab == "quality":
        body = _quality_body(user.user_id)
    else:
        return RedirectResponse("/crm-ops/duplicates", status_code=302)

    return HTMLResponse(_ops_page(tab, user.email, body))
