# Freshdesk Connector

A connector giving an AI agent read access (list/get/search) to Freshdesk
tickets, with two separate ways to authenticate depending on who's using it.
See [PLAN.md](PLAN.md) for full architecture and design rationale,
[CAPABILITIES.md](CAPABILITIES.md) for what the agent can and cannot do.

## Two modes

| | Single-tenant (API key) | Multi-tenant (OAuth 2.1 + real MCP) |
|---|---|---|
| Who it's for | One operator, one Freshdesk account | Anyone — each user connects their own Freshdesk account via Claude, ChatGPT, or Claude Code |
| Setup | Paste a key into `.env` | Click "Connect" in Claude/ChatGPT, log in with your Freshdesk credentials |
| Files | `src/mcp_server.py` | `src/mcp_http_server.py` (mounts on top of `src/oauth_server.py`) |
| Needs | Nothing beyond Python | MongoDB + a public HTTPS URL |

Both share the same Freshdesk HTTP client (`src/freshdesk_client.py`).

**Live deployment (Mode 2):** [`https://fresh-desk-mcp.vercel.app`](https://fresh-desk-mcp.vercel.app)
— this is the URL to use in the "Connect from ChatGPT / Claude / Claude Code"
sections below. No setup needed to try it; just connect and authorize with
your own Freshdesk domain and API key.

---

## Mode 1: Single-tenant (API key)

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in FRESHDESK_DOMAIN and FRESHDESK_API_KEY
```
Get the API key from Freshdesk: Profile Settings (top-right avatar) → View
API Key. (If it's disabled, enable it first under Admin → Agents → your
agent → Security and permission → API Key access.)

Run as an MCP server:
```bash
python src/mcp_server.py
```
Point Agent Studio's MCP client config at this process (stdio transport).

---

## Mode 2: Multi-tenant (OAuth 2.1 + real MCP over HTTP)

This connector acts as its own OAuth 2.1 authorization server **and** a
real MCP server (JSON-RPC over HTTP, the `streamable-http` transport) —
Claude, ChatGPT, or Claude Code "Connect" directly to it, and each user
authorizes their own Freshdesk account via a consent screen, without ever
touching an API key file. Full flow diagram and design rationale in
[PLAN.md's OAuth 2.1 architecture section](PLAN.md#oauth-21-architecture).

> **Why two files (`oauth_server.py` + `mcp_http_server.py`), not one?**
> `oauth_server.py` has the OAuth endpoints (register/authorize/token),
> proven correct against the OAuth 2.1 spec on their own. `mcp_http_server.py`
> mounts a real MCP server on top of those same endpoints — built after
> discovering live that ChatGPT's "Connect app" flow speaks MCP's JSON-RPC
> protocol directly (`POST /`, discovery at `/.well-known/oauth-protected-resource`),
> not the older ChatGPT-Plugins/OpenAPI format a first attempt used, which
> left ChatGPT saying "Authentication succeeded, action discovery failed."
> **Run `mcp_http_server.py`, not `oauth_server.py` directly** — it includes
> everything `oauth_server.py` has, plus the working MCP layer.

### Run it locally

Requires MongoDB (local for dev, Atlas free tier for a real deployment):
```bash
cp .env.example .env   # fill in MONGO_URI and OAUTH_BASE_URL
uvicorn mcp_http_server:app --app-dir src --port 8004
```

Try the full OAuth flow yourself (one script, no manual curl needed):
```bash
FRESHDESK_DOMAIN=yourcompany.freshdesk.com FRESHDESK_API_KEY=your_key \
  tests/test_oauth_flow.sh
```
This registers a test client, walks through PKCE authorize → consent (using
the real Freshdesk credentials you pass in) → token exchange → an
authenticated ticket fetch, and checks that a used authorization code can't
be replayed and that refresh tokens rotate correctly.

### Deploy it (so Claude/ChatGPT can connect from anywhere)

Already deployed at `https://fresh-desk-mcp.vercel.app` — to deploy your own copy:

1. Deploy this repo to Vercel (the included `pyproject.toml` has the
   `[tool.vercel] entrypoint = "src.mcp_http_server:app"` setting it needs).
2. Set `OAUTH_BASE_URL` to that deployed URL (e.g. `https://your-app.vercel.app`).
   This value feeds both the OAuth discovery metadata and the MCP transport's
   allowed-hosts check — get it wrong and every tool call 421s.
3. Point `MONGO_URI` at an Atlas free-tier cluster instead of localhost.
4. For local testing before a real deploy, `ngrok http 8004` gives a public
   HTTPS URL — set `OAUTH_BASE_URL` to that ngrok URL.

(Render also works with the same `src/mcp_http_server.py` entrypoint — see
the voice-agent project in this submission for that deployment pattern.)

### Connect from ChatGPT

1. ChatGPT → **Settings → Connectors** (or the legacy **Plugins** page,
   depending on your account) → **Connect / Add a connector**.
2. Enter `https://fresh-desk-mcp.vercel.app` (or your own deployed URL).
3. ChatGPT auto-discovers the OAuth + MCP endpoints from
   `/.well-known/oauth-protected-resource` and `/.well-known/oauth-authorization-server`.
4. Click through the OAuth consent screen — enter your real Freshdesk domain
   and API key when prompted (this is this connector's login step, not a
   Freshdesk-hosted page — see PLAN.md for why).
5. In a new chat, ask something like *"List my recent Freshdesk tickets"* or
   *"Search my Freshdesk tickets for authentication failure"* — ChatGPT
   calls `list_tickets` / `search_tickets` behind the scenes.

If tools don't show up after connecting, disconnect and reconnect the app —
ChatGPT can cache a stale manifest from an earlier connection attempt.

### Connect from Claude (claude.ai)

1. Claude → **Settings → Connectors** → **Add custom connector**.
2. Enter `https://fresh-desk-mcp.vercel.app` as the MCP server endpoint.
3. Same OAuth consent flow as above — Freshdesk domain + API key, approve.
4. Ask Claude to list or search your Freshdesk tickets in any chat.

### Connect from Claude Code

Claude Code speaks MCP natively over stdio or HTTP. To add this connector:
```bash
claude mcp add --transport http freshdesk https://fresh-desk-mcp.vercel.app
```
Claude Code will walk you through the same OAuth consent flow in your
browser the first time a tool from this connector is used. After that,
`list_tickets`, `get_ticket`, and `search_tickets` are available as tools
in any Claude Code session.

---

## Tests

```bash
python tests/test_freshdesk_client.py   # HTTP client: retry, pagination, auth, rate limits, search-query handling
python tests/test_oauth_server.py       # PKCE math, discovery, registration, auth rejections
tests/test_oauth_flow.sh                # live end-to-end OAuth flow (needs a running server + real Freshdesk creds)
```

## Tools exposed

- `list_tickets(status, updated_since, page)`
- `get_ticket(ticket_id, include)`
- `search_tickets(query)` — structured Freshdesk syntax (`status:2`) goes straight
  to Freshdesk's search API; anything else (plain text like `"authentication failure"`)
  is matched client-side against ticket subject/description, since Freshdesk's
  search has no free-text field — see PLAN.md for why.

Same three primitives in both modes — Mode 1 exposes them as MCP tools over
stdio (`src/mcp_server.py`), Mode 2 exposes them as real MCP tools over HTTP
(`src/mcp_http_server.py`), reachable by any MCP-speaking client after
completing the OAuth handshake.
