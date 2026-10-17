"""Shared OAuth access-token refresh logic — extracted from
mcp_http_server.py's _call_tool_for_current_caller so crm_contacts.py's
fetch_all_contacts can refresh an expiring/expired token too, instead of
only the real per-tool dispatch path doing it (that gap is why a stale
HubSpot/Zoho token silently broke cross-CRM fetch but not a live MCP tool
call against the same provider).

Same two-layer strategy either caller uses:
- proactive: refresh ahead of a known expires_at
- reactive: on an actual 401/expired-session error, refresh once and retry
"""
try:
    import connections
    from adapters import Connection, ProviderAdapter
except ImportError:
    from src import connections
    from src.adapters import Connection, ProviderAdapter


def looks_like_auth_error(exc: Exception) -> bool:
    # Every provider client raises a generic {Provider}Error with the HTTP
    # status embedded in the message (f"{method} {path} -> {status}: ..."),
    # not a typed exception per status code — matching the string is what
    # the rest of this codebase already does for the same reason.
    message = str(exc)
    return "-> 401" in message or "INVALID_SESSION_ID" in message


def refresh_and_persist(adapter: ProviderAdapter, conn: Connection, provider: str) -> Connection:
    refreshed = adapter.refresh(conn)
    connections.update_credentials(conn.user_id, provider, refreshed.credentials, refreshed.expires_at)
    return refreshed
