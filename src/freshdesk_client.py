"""Minimal Freshdesk REST client: auth + rate-limit handling.
No retry tower — one retry on 429, honoring Retry-After. Good enough for a
connector making a handful of calls per agent turn.

Two ways to use this module:
- Single-tenant / local dev: set FRESHDESK_DOMAIN + FRESHDESK_API_KEY in the
  environment, call list_tickets() / get_ticket() / search_tickets() with no
  extra args — used by mcp_server.py and tests/test_freshdesk_client.py.
- Multi-tenant (oauth_server.py): each OAuth-connected user has their own
  Freshdesk domain + API key, resolved per-request from the Mongo grant —
  use the *_for() variants, which take domain/api_key explicitly instead of
  reading the environment.
"""
import os
import time

import httpx

FRESHDESK_DOMAIN = os.environ.get("FRESHDESK_DOMAIN")  # unused by oauth_server.py's *_for() calls
FRESHDESK_API_KEY = os.environ.get("FRESHDESK_API_KEY")


class FreshdeskError(Exception):
    pass


def _request(method: str, domain: str, api_key: str, path: str, **kwargs) -> httpx.Response:
    base_url = f"https://{domain}/api/v2"
    auth = (api_key, "X")

    resp = httpx.request(method, f"{base_url}{path}", auth=auth, timeout=30, **kwargs)
    if resp.status_code == 429:
        retry_after = int(resp.headers.get("Retry-After", "5"))
        time.sleep(retry_after)
        resp = httpx.request(method, f"{base_url}{path}", auth=auth, timeout=30, **kwargs)

    remaining = resp.headers.get("X-RateLimit-Remaining")
    if remaining is not None and float(remaining) < 5:
        time.sleep(1)  # ponytail: flat backoff near limit, add token-bucket if throughput grows

    if resp.status_code >= 400:
        raise FreshdeskError(f"{method} {path} -> {resp.status_code}: {resp.text}")
    return resp


def list_tickets_for(
    domain: str, api_key: str, status: str | None = None, updated_since: str | None = None, page: int = 1
) -> list[dict]:
    params = {"page": page}
    if status:
        params["filter"] = status
    if updated_since:
        params["updated_since"] = updated_since
    return _request("GET", domain, api_key, "/tickets", params=params).json()


def get_ticket_for(domain: str, api_key: str, ticket_id: int, include: str | None = None) -> dict:
    params = {"include": include} if include else {}
    return _request("GET", domain, api_key, f"/tickets/{ticket_id}", params=params).json()


def _looks_structured(query: str) -> bool:
    return ":" in query


# Freshdesk's /search/tickets endpoint only indexes a fixed set of structured fields
# (status, priority, type, source, agent_id, group_id, tag, created_at, due_by, etc.) — there is
# no subject/free-text field, confirmed live ("Unexpected/invalid field in request" on
# query=subject:'...'). A natural-language query like "authentication failure" (what an MCP
# client/ChatGPT actually sends) has no structured-search equivalent, so it's handled by paging
# through list_tickets_for and matching the subject/description client-side instead.
_FREE_TEXT_SEARCH_PAGES = 5  # ponytail: scans up to 500 tickets (Freshdesk's default page size is 100); fine for a small/medium helpdesk, add a real search index if this becomes a bottleneck


def search_tickets_for(domain: str, api_key: str, query: str) -> list[dict]:
    if _looks_structured(query):
        resp = _request("GET", domain, api_key, "/search/tickets", params={"query": f'"{query}"'})
        return resp.json().get("results", [])

    needle = query.lower()
    matches = []
    for page in range(1, _FREE_TEXT_SEARCH_PAGES + 1):
        tickets = list_tickets_for(domain, api_key, page=page)
        if not tickets:
            break
        matches.extend(
            t for t in tickets
            if needle in (t.get("subject") or "").lower() or needle in (t.get("description_text") or "").lower()
        )
    return matches


# --- Single-tenant convenience wrappers, reading FRESHDESK_DOMAIN/API_KEY from the environment ---

def list_tickets(status: str | None = None, updated_since: str | None = None, page: int = 1) -> list[dict]:
    if not (FRESHDESK_DOMAIN and FRESHDESK_API_KEY):
        raise FreshdeskError("FRESHDESK_DOMAIN and FRESHDESK_API_KEY must be set for single-tenant use")
    return list_tickets_for(FRESHDESK_DOMAIN, FRESHDESK_API_KEY, status, updated_since, page)


def get_ticket(ticket_id: int, include: str | None = None) -> dict:
    if not (FRESHDESK_DOMAIN and FRESHDESK_API_KEY):
        raise FreshdeskError("FRESHDESK_DOMAIN and FRESHDESK_API_KEY must be set for single-tenant use")
    return get_ticket_for(FRESHDESK_DOMAIN, FRESHDESK_API_KEY, ticket_id, include)


def search_tickets(query: str) -> list[dict]:
    if not (FRESHDESK_DOMAIN and FRESHDESK_API_KEY):
        raise FreshdeskError("FRESHDESK_DOMAIN and FRESHDESK_API_KEY must be set for single-tenant use")
    return search_tickets_for(FRESHDESK_DOMAIN, FRESHDESK_API_KEY, query)
