# Freshdesk Connector for Agent Studio — Implementation Plan

## Goal
Private connector letting an Agent Studio agent read (list/get/search) Freshdesk
tickets. Two auth modes (API-key and full OAuth 2.1), rate-limit handling,
MCP tool spec, capability doc.

## Why Freshdesk
Ticket read/search maps cleanly to 3 primitives (list, get, search).

## Auth: two modes

The assignment allows either OAuth or API-key auth. This connector ships
both, for two different deployment shapes:

**1. Single-tenant API key** (`freshdesk_client.py`, `mcp_server.py`) —
`Authorization: Basic base64(api_key:X)`. Key obtained from Freshdesk UI
(Profile Settings → API Key), stored as `FRESHDESK_API_KEY` + `FRESHDESK_DOMAIN`
env vars. Simplest path: one operator, one Freshdesk account, config via
`.env`. No web server beyond the MCP stdio server itself.

**2. Multi-tenant OAuth 2.1** (`oauth_server.py`) — lets anyone click
"Connect" from Claude or ChatGPT and authorize their own Freshdesk account,
without ever handling an API key themselves or editing a config file. This
connector is its own OAuth **authorization server** (not a client to
Freshdesk's OAuth — Freshdesk doesn't need an OAuth app registered at all
in this design). See **OAuth 2.1 architecture** below.

Both modes share `freshdesk_client.py`'s core HTTP/rate-limit logic —
the single-tenant functions (`list_tickets`, `get_ticket`, `search_tickets`)
read `FRESHDESK_DOMAIN`/`FRESHDESK_API_KEY` from the environment, the
multi-tenant `*_for()` variants take `domain`/`api_key` explicitly per call.

## OAuth 2.1 architecture

```
Claude/ChatGPT          oauth_server.py              Freshdesk            MongoDB
      │                        │                          │                  │
      │  GET /oauth/register   │                          │                  │
      ├───────────────────────>│                          │                  │
      │  client_id              │                          │                  │
      │<───────────────────────┤                          │                  │
      │                        │                          │                  │
      │  GET /oauth/authorize   │                          │                  │
      │  (PKCE challenge)       │                          │                  │
      ├───────────────────────>│                          │                  │
      │  consent page (HTML)    │                          │                  │
      │<───────────────────────┤                          │                  │
      │                        │                          │                  │
      │  user enters Freshdesk  │                          │                  │
      │  domain + API key       │                          │                  │
      ├───────────────────────>│  verify creds             │                  │
      │                        ├─────────────────────────>│                  │
      │                        │  200 OK (valid)           │                  │
      │                        │<─────────────────────────┤                  │
      │                        │  store grant (hashed code) │                  │
      │                        ├──────────────────────────────────────────────>│
      │  302 redirect w/ code   │                          │                  │
      │<───────────────────────┤                          │                  │
      │                        │                          │                  │
      │  POST /oauth/token      │                          │                  │
      │  (code + PKCE verifier) │                          │                  │
      ├───────────────────────>│  atomic claim (code_hash -> NULL)             │
      │                        ├──────────────────────────────────────────────>│
      │  access + refresh token │                          │                  │
      │<───────────────────────┤                          │                  │
      │                        │                          │                  │
      │  GET /tickets            │                          │                  │
      │  Authorization: Bearer   │                          │                  │
      ├───────────────────────>│  look up grant by token_hash                  │
      │                        ├──────────────────────────────────────────────>│
      │                        │  call Freshdesk w/ stored domain+key          │
      │                        ├─────────────────────────>│                  │
      │  ticket data             │  ticket data              │                  │
      │<───────────────────────┤<─────────────────────────┤                  │
```

Design points, mirroring [Humantic's own MCP OAuth server](../../../HumanticAI_Work/payment_website/mcp-oauth.js) (same shape, Mongo instead of SQL):

- **Dynamic Client Registration** (RFC 7591, `/oauth/register`): any MCP
  client self-registers, no manual app-registration step for each one.
- **PKCE mandatory** (S256, OAuth 2.1 requirement for public clients —
  Claude/ChatGPT are public clients, no client secret).
- **The Freshdesk API key IS the login credential** at the consent screen —
  no separate username/password system. Verified live against Freshdesk
  before a code is ever issued, so a bad key fails at consent, not later.
- **Single-use authorization codes**: the token exchange does one atomic
  MongoDB `update_one` that both claims the code (`code_hash -> None`) and
  fills in the token hashes — a code can't be redeemed twice even under a
  race.
- **Rotating refresh tokens**: every refresh issues a new access+refresh
  pair; the old pair stops working immediately.
- **Only hashes stored** for codes/access/refresh tokens (SHA-256) — a
  database read alone can't be replayed as a live credential. The
  Freshdesk API key itself is stored as-is in this demo (needed again on
  every tool call); see Limitations for the production fix.
- **RFC 8414 discovery** (`/.well-known/oauth-authorization-server`) so
  clients can find every endpoint automatically from just the base URL.

## Primitives
| MCP tool | Freshdesk endpoint | Notes |
|---|---|---|
| `list_tickets` | `GET /api/v2/tickets` | pagination via `page`, filters via `updated_since`, `status` |
| `get_ticket` | `GET /api/v2/tickets/{id}` | optional `include=conversations,requester` |
| `search_tickets` | `GET /api/v2/search/tickets?query=` | Freshdesk's query syntax, e.g. `"status:2 AND priority:3"` |

## Rate-limit handling
Freshdesk returns `429` + `Retry-After` header, and every response carries
`X-RateLimit-Remaining` (observed as a float-formatted string, e.g. `"49.0"`,
not always an integer — parsed as `float()` after an earlier version crashed
on this in live testing). Client:
- Reads `X-RateLimit-Remaining` after each call; if < 5, sleeps briefly before next call.
- On `429`, sleeps exactly `Retry-After` seconds, retries once, then raises.
- Single retry, no exponential backoff tower — ponytail: good enough for a connector calling a handful of times per agent turn; add backoff/queue if this becomes high-throughput.

## Files
```
freshdesk-connector/
  PLAN.md
  README.md
  CAPABILITIES.md             # what the agent can / cannot do
  requirements.txt
  .env.example
  src/
    freshdesk_client.py        # HTTP + rate-limit handling; single-tenant env-based auth AND per-request *_for() variants
    mcp_server.py               # MCP stdio server, single-tenant mode (list/get/search as tools)
    oauth_server.py              # OAuth 2.1 authorization server, multi-tenant mode
  tests/
    test_freshdesk_client.py    # mocked HTTP: retry-on-429, pagination, auth header, rate-limit parsing
    test_oauth_server.py         # PKCE math, discovery shape, registration, auth/token endpoint rejections
    test_oauth_flow.sh            # live end-to-end script: register -> authorize -> consent -> token -> fetch
```

## MCP tool spec (mcp_server.py)
Three tools, each returns JSON matching Freshdesk's response shape (passed
through, not reshaped — avoids a translation layer that would drift from
Freshdesk's schema):
- `list_tickets(status: str | None, updated_since: str | None, page: int = 1) -> list[dict]`
- `get_ticket(ticket_id: int, include: str | None) -> dict`
- `search_tickets(query: str) -> list[dict]`

## Guardrails / what agent can and cannot do
See [CAPABILITIES.md](CAPABILITIES.md). Summary: read-only, no ticket creation/
update/deletion, no access to contact PII beyond what's in the ticket payload,
no attachment download (would need separate signed-URL handling).

## Evaluation
`tests/test_freshdesk_client.py`: mocks `httpx` responses —
- asserts 429 triggers exactly one retry honoring `Retry-After`
- asserts pagination param is forwarded correctly
- asserts auth header is correctly base64-encoded
- asserts a float-formatted `X-RateLimit-Remaining` header doesn't crash the client

`tests/test_oauth_server.py`: against a real local MongoDB (test database,
cleaned up after) —
- PKCE S256 challenge computation matches the RFC 7636 test vector
- discovery endpoint advertises the right grant types and PKCE support
- client registration persists and rejects missing `redirect_uris`
- authorize rejects an unknown `client_id` and an unregistered `redirect_uri`
- token exchange rejects an unknown authorization code
- `/tickets` rejects a request with no bearer token

`tests/test_oauth_flow.sh`: a live, runnable end-to-end script against a
running `oauth_server.py` and a real Freshdesk account — registers a client,
completes the full PKCE authorize/consent/token flow, fetches real tickets
with the issued token, confirms the authorization code can't be reused, and
confirms refresh-token rotation issues a genuinely new access token. This is
the test that was actually run to validate the OAuth build (see
`results/` equivalent — this connector doesn't have call recordings the way
the voice agent does, so this script is the evidence).

## Limitations & long-term fix
- Read-only. Long-term: add `update_ticket`/`add_note` tools behind an
  explicit write-scope flag + human-in-the-loop confirmation before any
  Agent Studio agent can mutate a live ticket.
- No webhook/event subscription — agent must poll `list_tickets` with
  `updated_since`. Long-term: Freshdesk webhook → push updates into Agent
  Studio's event bus instead of polling.
- **The Freshdesk API key is stored as-is in MongoDB** per OAuth grant (not
  hashed, unlike the OAuth tokens themselves) because it has to be reused on
  every tool call — there's no way to "hash" a credential you still need in
  plaintext later. Long-term: field-level encryption at rest (e.g. envelope
  encryption with a KMS key), or move to a proper Freshdesk OAuth2 app
  integration (Freshdesk does support being an OAuth provider itself) so we
  never hold a long-lived Freshdesk credential at all, only short-lived
  Freshdesk-issued tokens.
- **No real user accounts** — the Freshdesk API key doubles as the login
  credential, which means anyone with a given Freshdesk API key can connect
  as "that identity" with no additional factor. Fine for a demo; production
  would add a real account layer (email verification, password or SSO) in
  front of the Freshdesk-linking step, so the two identities (our platform
  account vs. the linked Freshdesk account) are separate.
- **In-memory consent sessions** (`_sessions` dict in `oauth_server.py`) —
  survives fine for a single server instance but doesn't survive a restart
  mid-flow, and won't work if ever scaled to multiple instances behind a
  load balancer. Long-term: move this into MongoDB too (short TTL collection)
  alongside the grants.
- **No token revocation UI** — `/oauth/revoke` exists and works, but nothing
  in this connector calls it; a user has no way to "disconnect" their
  Freshdesk account short of someone manually hitting that endpoint.
  Long-term: a small account-management page listing active connections
  with a revoke button.
