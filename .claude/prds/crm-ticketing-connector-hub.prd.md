# PRD: Connector Hub for CRM and Ticketing Tools

**Status**: Draft, awaiting decisions in Section 12
**Owner**: Kush Goel
**Date**: 2026-10-15
**Builds on**: `freshdesk-connector` (this repo)
**Reference code**: `/home/kush-goel/HumanticAI_Work/humantic_apis`, `humantic_enrichment`, `payment_website`

---

## 1. Summary

Build one hosted connector service that lets a user log in, connect their
CRM or ticketing account (Salesforce, HubSpot, Zoho, Outreach, SalesLoft,
Freshdesk, Asana, and later others), and then use that platform's data as
tools inside Claude and ChatGPT. The user adds one custom connector in
Claude or ChatGPT. Its tools then cover every platform they connected.

The current repo does this for one platform, Freshdesk, read-only. This PRD
generalizes it into a provider adapter framework and adds CRM and
sales-engagement providers.

## 2. Problem

- Sales and support users work across several tools. AI assistants cannot
  reach those tools without a custom connector per tool, built by hand.
- Each platform has its own OAuth app, token lifecycle, rate limits, and
  data model. Rebuilding that per assistant (Claude, ChatGPT, Copilot)
  duplicates work.
- The current Freshdesk connector takes a raw API key as the login
  credential. That is acceptable for a demo but not for users who expect
  "Connect with Salesforce".

## 3. Goals

1. A user connects a provider by logging in, with no API keys handled by hand
   wherever the provider supports OAuth.
2. Claude and ChatGPT both connect to one URL and see the tools for every
   provider the user connected.
3. Adding a provider means writing one adapter module and one config entry.
   No changes to the MCP layer or the auth server.
4. Read access first. Write access is opt-in per provider and per tool, and
   each write requires explicit user approval.
5. Upstream tokens are encrypted at rest and never returned to the client or
   to the model.

## 4. Non-goals (v1)

- Syncing CRM data into our own database. Tools call the upstream API live.
- A unified CRM data model that merges Salesforce and HubSpot objects. Each
  provider keeps its own shapes (Section 6.3).
- A web dashboard beyond the consent and "Connected accounts" pages.
- A general proxy for arbitrary API paths.
- Bulk export or mass-messaging features. These carry high abuse and
  compliance risk and need a separate review first.

## 5. Users and use cases

| User | Connects | Example request |
|---|---|---|
| AE using Salesforce and Outreach | Salesforce, Outreach | "Which of my open opps haven't had a touch in 14 days?" |
| CS manager using Freshdesk | Freshdesk, Asana | "Summarize open Freshdesk tickets for account X" (create Asana task is a v2 write) |
| Marketing ops using HubSpot | HubSpot | "List contacts created this week with no owner" |
| Engineer or operator | Freshdesk (API key) | Same as today's connector |

## 6. Architecture

### 6.1 Components

```
Claude / ChatGPT / Claude Code
        │  MCP over streamable-http, OAuth 2.1 (PKCE, DCR)
        ▼
┌──────────────────────────────────────────────────────────┐
│ Connector Hub (this repo, evolved)                       │
│                                                          │
│  auth/        MCP OAuth 2.1 authorization server         │
│               (today's oauth_server.py, promoted)        │
│  mcp/         MCP tool router: lists tools for the       │
│               user's connected providers only            │
│  connect/     Upstream OAuth + API-key connect flows     │
│  adapters/    One module per provider (Section 6.2)      │
│  vault/       Encrypted token and credential store       │
│  common/      HTTP client, retry, rate limit, logging    │
└──────────────────────────────────────────────────────────┘
        │ upstream OAuth / REST
        ▼
Salesforce · HubSpot · Zoho · Outreach · SalesLoft · Freshdesk · Asana
```

Two login steps, kept separate on purpose (same reasoning as PLAN.md):

1. **Platform login**: the user signs in to the Connector Hub. This gives
   the assistant an access token for the hub only.
2. **Provider connect**: from the "Connected accounts" page, the user
   authorizes each upstream provider. The hub stores that provider's tokens.

The current design merges these two steps, with the Freshdesk API key as the
login. v1 splits them.

### 6.2 Provider adapter contract

Every provider implements the same interface. Names are indicative:

```python
class ProviderAdapter(Protocol):
    key: str                      # "salesforce", "hubspot", ...
    display_name: str
    auth_modes: set[AuthMode]     # {OAUTH2}, {API_KEY}, or both
    scopes: list[str]             # minimum scopes requested
    def tools(self) -> list[ToolSpec]: ...                          # read tools, plus write tools when enabled
    async def call(self, tool: str, args: dict, conn: Connection) -> dict: ...
    async def refresh(self, conn: Connection) -> Connection: ...    # OAuth only
    async def verify(self, conn: Connection) -> None: ...           # raises on bad credentials
```

Tool names are namespaced by provider: `salesforce_search_records`,
`hubspot_search_contacts`, `freshdesk_list_tickets`. Namespacing stops two
providers from colliding on a generic name like `search`.

Each `ToolSpec` declares MCP annotations: `readOnlyHint`, `destructiveHint`,
`idempotentHint`, `openWorldHint`. Clients use these to decide when to ask
for approval. A write tool must set `readOnlyHint=false` and
`destructiveHint` accurately.

### 6.3 Data shape

Tools return provider-native JSON, passed through and not reshaped. This is
the same decision PLAN.md makes for Freshdesk. It avoids a translation layer
that drifts from upstream schemas. Each result includes `provider` and
`source_url` so the model can cite records.

A shared normalized view (`crm_search_contacts` across providers) is a v2
idea, not v1 (Section 12, decision 2).

### 6.4 Tool exposure

- `tools/list` returns only tools for providers the user has connected and
  enabled.
- Each provider exposes at most about 8 tools in v1. Total tool count is
  checked against client limits (Section 10).
- A user can disable individual tools or whole providers in "Connected
  accounts".

## 7. Auth and security

### 7.1 Platform auth (assistant to hub)

Keep the existing design in `src/oauth_server.py`, with these changes:

- Move in-memory `_sessions` to MongoDB with a TTL index. The current code
  loses consent state on restart and breaks across instances (PLAN.md
  Limitations).
- Keep PKCE S256 mandatory, single-use codes, rotating refresh tokens, and
  hashed codes and tokens.
- Wire `/oauth/revoke` to a "Disconnect" button, not only an endpoint.
- Hub accounts get a real user ID as the `sub` claim, with email or SSO login.
  Today the Freshdesk API key doubles as identity. v1 separates the two.
  This is its own subsystem (M0a below), not a bullet folded into the OAuth
  server work: signup, login, session storage, password reset or SSO. It is
  the one piece every later milestone depends on, since `connections` key on
  the hub `user_id`.

### 7.2 Provider auth (hub to upstream)

| Provider | Auth | Notes |
|---|---|---|
| Salesforce | OAuth 2.0 web server flow via a Connected App we own | Instance URL per user comes from the token response. Refresh token policy depends on the app settings. |
| HubSpot | OAuth 2.0 with refresh tokens | Store access-token expiry and refresh before it passes. Pattern in `humantic_enrichment/api/hubspot_client.js`. |
| Zoho | OAuth 2.0 per data center | Accounts and API hosts differ by DC (`eu`, `au`, `in`, `jp`, `uk`, `us`). Store the DC with the connection. Pattern in `humantic_apis/config/zoho.config.js`. |
| Outreach | OAuth 2.0 | Pattern in `humantic_apis/config/outreach.config.js`. |
| SalesLoft | OAuth 2.0 | Pattern in `humantic_apis/config/salesloft.config.js`. |
| Asana | OAuth 2.0 | Standard flow. A personal access token works for internal testing. |
| Freshdesk | API key (current); OAuth later if Freshdesk offers it for this use | Keep API-key connect as the fallback. |

**Connection record**, stored in Mongo, keyed `(user_id, provider)` in v1 —
one connected account per provider per hub user:

```
connections {
  _id, user_id, provider, status (active | needs_reauth | revoked),
  auth_type (oauth2 | api_key),
  credentials_enc,        // envelope-encrypted JSON: tokens, api key, instance_url, dc
  scopes, expires_at, created_at, last_used_at,
  enabled_tools: [...]
}
```

**Non-goal, v1**: a user connecting two accounts of the same provider (two
Salesforce orgs, two HubSpot portals). The schema above cannot express it —
reconnecting the same provider overwrites the existing connection. If this
is needed later, the key becomes `(user_id, provider, external_account_id)`
and the UI needs an "add another account" path. Flagging now so nobody is
surprised when a user with two Salesforce orgs hits this limit.

**External dependency, not yet built**: this table assumes a registered app
per provider (Connected App for Salesforce, app registration for HubSpot,
Zoho, Outreach, SalesLoft). None exist yet. Each needs, before any adapter
code can be tested end-to-end:
- a developer account with that platform,
- an app/connected-app created there with redirect URIs pointing at this
  hub's callback,
- for some (HubSpot, Salesforce app listings used beyond internal testing),
  a review or approval step with its own lead time, independent of our
  build schedule.
This is tracked as its own task per provider in the milestone it lands in
(M2/M3), not assumed to exist when that milestone starts. See Section 13
risk "External app approval lead time."

### 7.3 Secrets handling

- Upstream client IDs and secrets come from environment variables or a secret
  manager. Never in source.
- **Found in the reference code**: `humantic_apis/config/*.config.js` and
  `humantic_enrichment/config.js` contain client secrets as literals. Do not
  copy these files. Rotate those secrets, since they sit in a working tree
  that other tools read. This PRD does not repeat the values.
- Credentials are encrypted at rest with envelope encryption (a data key per
  record, wrapped by a KMS or local master key). The reference code stores
  plaintext access tokens and uses `cryptr` only for email fields. Do not
  copy that pattern.
- Tokens and secrets never appear in tool results, logs, error messages, or
  MCP responses. Log the provider and status code only.
- On provider 401 or revoke, set the connection to `needs_reauth` and tell
  the user. Do not retry in a loop.

### 7.4 Write safety

- Write tools are disabled by default and opt-in per provider in "Connected
  accounts".
- A write tool returns a preview first (what will change). It executes only
  on a second call with a confirmation token. Assistants that support
  elicitation can use that instead.
- Writes are audit-logged: user, provider, tool, target ID, timestamp. No
  payload bodies in logs.

### 7.5 Other threats

- SSRF: instance URLs (Salesforce) and DCs (Zoho) come from the provider's
  token response or a fixed allowlist, never from user input.
- Rate limits: honor each provider's 429 and `Retry-After`. Per-user and
  per-provider budgets live in the common HTTP client.
- Scope creep: request the minimum scopes per provider. Re-consent when
  scopes grow.
- Prompt injection: tool results contain customer-written text (ticket
  bodies, notes). Tool descriptions state that results are data, not
  instructions.

## 8. Requirements

### Functional

| ID | Requirement | Priority |
|---|---|---|
| F1 | A user signs in to the hub and connects an MCP client via OAuth 2.1 with PKCE and DCR. | P0 |
| F2 | The user connects a provider via upstream OAuth or API key from a "Connected accounts" page. | P0 |
| F3 | `tools/list` returns tools only for active, enabled connections. | P0 |
| F4 | Each adapter exposes the read tools in Section 9. | P0 |
| F5 | Tokens refresh before expiry and on 401, with one retry. | P0 |
| F6 | The user can disconnect a provider. That revokes upstream where supported and deletes stored credentials. | P0 |
| F7 | The user can disable individual tools. | P1 |
| F8 | Write tools use preview-then-confirm. | P1 (v2) |
| F9 | Provider webhooks for change events. The reference repo has webhook routes for HubSpot, Outreach, SalesLoft, and Zoho. | P2 |
| F10 | "Connected accounts" page shows status, last used, and scopes. | P1 |

### Non-functional

| ID | Requirement |
|---|---|
| N1 | Single-record read p95 under 2 s, excluding upstream slowness. |
| N2 | Survives restarts and runs on more than one instance. Session and consent state live in Mongo. |
| N3 | Tokens encrypted at rest. A database dump alone does not allow token use. |
| N4 | Each tool call logged with user, provider, tool, status, duration. No secrets in logs. |
| N5 | Adapters have mocked-HTTP unit tests. Each provider gets one live smoke test against a sandbox account. |
| N6 | Works in Claude.ai, ChatGPT, and Claude Code. Tested in all three before release. |

## 9. v1 tool scope per provider

Read tools only in v1. Names are proposals.

| Provider | Tools |
|---|---|
| Freshdesk | `list_tickets`, `get_ticket`, `search_tickets` (existing) |
| Asana | `list_projects`, `list_tasks`, `get_task`, `search_tasks` |
| HubSpot | `search_contacts`, `get_contact`, `search_deals`, `get_deal`, `list_companies` |
| Salesforce | `search_records` (SOQL, validated), `get_record`, `list_opportunities` |
| Zoho CRM | `search_records`, `get_record`, `list_deals` |
| Outreach | `search_prospects`, `list_sequences`, `list_opportunities` |
| SalesLoft | `search_people`, `list_cadences`, `list_activities` |

Salesforce `search_records` takes SOQL. The primary control is running the
query through the connected user's own OAuth-scoped session, so Salesforce's
own object- and field-level security already applies — the tool cannot see
data the user's Salesforce profile does not grant. A `SELECT`-only,
DML-rejecting, `LIMIT`-capped filter sits in front of that as defense in
depth, not as the real gate: a hand-rolled SOQL allowlist alone is bypassable
(subqueries, relationship traversal) and must not be the only thing standing
between the tool and cross-object data within the same org. Give this its
own security review before M2 ships.

## 10. Platform constraints to verify before build

These come from general knowledge of the platforms, not a fresh review. Check
each before committing to its milestone.

- **Claude and ChatGPT custom connectors**: confirm current limits on tool
  count, description length, and how many connectors a user can enable at
  once. Set the per-provider tool budget after this check.
- **Public distribution**: HubSpot, Salesforce, Zoho, Outreach, and SalesLoft
  each have an app review or marketplace process for apps used by other
  customers. Private or internal use avoids most of it. Confirm which mode
  applies (Section 12, decision 1).
- **Hosting**: Vercel serverless works for request/response MCP. Confirm
  streamable-http behavior on the chosen host before relying on it.
- **Data retention and compliance**: CRM data often includes personal data.
  Decide log retention and whether each customer needs a DPA.

## 11. Milestones

Each milestone ends with working, tested software. Each is one `/plan` run
against the milestone and one PR.

M0 was one "about 1 week" line covering six distinct subsystems (session
storage, a new user-auth system, key management, an adapter interface, a
migration, and a UI page). That understated it and buried hub-native user
auth as an unlisted bullet. Split into two milestones so hub auth gets its
own line, tasks, and exit criteria, and so slipping one does not silently
eat the other's time.

**M0a. Hub user accounts (about 1 week)**
- Hub user model: email/password or SSO login, session storage, password
  reset. This is the identity every `connections` row keys on — it has to
  exist before M0b's credential split means anything.
- Move platform OAuth server's `_sessions` to Mongo with a TTL index (fixes
  the restart/multi-instance loss in PLAN.md Limitations).
- `sub` claim on hub-issued tokens carries the real hub user ID, not a
  Freshdesk API key.
- Exit: a user can sign up, log in, log out, and the OAuth server survives a
  restart mid-consent.

**M0b. Credential split and adapter base (about 1-2 weeks)**
- Split platform login (M0a) from provider credentials: Freshdesk API key
  becomes a connection type, not the login.
- Envelope encryption for `credentials_enc` (data key per record, wrapped by
  a KMS or local master key).
- Provider adapter interface. Port Freshdesk to it with no change in tool
  names or behavior.
- Wire `/oauth/revoke` to a "Disconnect" action; add a "Connected accounts"
  page (API-key connect only, since no upstream OAuth app exists yet).
- Exit: existing tests pass; Freshdesk works from Claude and ChatGPT exactly
  as before, now sitting behind a real hub login and an encrypted credential
  store.

**M1. Upstream OAuth and Asana (about 1-2 weeks)**
- Register an Asana app (developer account, redirect URI) before any connect
  code can be tested end to end — start this in parallel with the generic
  flow below, it is external lead time outside our control.
- Generic upstream OAuth connect flow (state, PKCE where supported, callback).
- Refresh and `needs_reauth` handling.
- Asana adapter, read tools.
- Exit: connect and disconnect Asana from the UI; tools appear and work in
  Claude.

**M2. HubSpot and Salesforce (about 2-3 weeks)**
- Register a Salesforce Connected App and a HubSpot app at the start of this
  milestone, not the day the code needs them — Salesforce connected-app
  approval and any HubSpot review have lead time independent of the build.
- HubSpot adapter, using the refresh pattern from `humantic_enrichment`.
- Salesforce adapter with per-user instance URL and validated SOQL (the
  user's own OAuth-scoped session is the real access control; see Section 9).
- Per-provider rate budget.
- Exit: both providers' read tools pass sandbox smoke tests from all three
  clients.

**M3. Zoho, Outreach, SalesLoft (about 2-3 weeks)**
- Register Zoho, Outreach, and SalesLoft apps at the start of this milestone,
  same reasoning as M2.
- Zoho adapter with DC handling.
- Outreach and SalesLoft adapters.
- Exit: all seven providers connect and list tools.

**M4. Writes and hardening (about 2 weeks)**
- Preview-then-confirm write tools (F8), starting with Asana task create and
  Freshdesk note add.
- Audit log, connection health page (F10), tool toggles (F7).
- Webhook ingestion (F9) if still needed.
- Exit: write path tested; security review of the SOQL tool and token storage.

**M5. Distribution**
- Directory or marketplace submissions per Section 10.
- Operator runbook: rotate upstream secrets; rotate the hub's own master
  encryption key and define the blast radius if it leaks (who re-encrypts
  `credentials_enc`, whether existing connections must re-auth); handle
  revoke storms; watch rate limits.

Estimates assume one developer familiar with the reference code, and assume
each provider's app registration is started at the start of its milestone,
not after. Treat them as relative sizing, not commitments — the app
registration and review steps in M1-M3 depend on each platform's own
turnaround, which this PRD cannot control or predict.

## 12. Open decisions

Recommended default in bold.

1. **Who uses the hub?**
   - **A. Internal first, then private or invite-only apps (recommended).**
     Avoids most marketplace review while the design gets proven.
   - B. Public multi-tenant from day one. Needs review for every provider and
     a DPA story before launch.
2. **Tool shape?**
   - **A. Provider-namespaced tools (recommended).** Matches the Freshdesk
     design and keeps data native.
   - B. Unified cross-CRM tools (`crm_search_contacts`). Easier for the model,
     but needs a normalization layer that will drift. Consider for v2.
3. **Writes in v1?**
   - **A. No. Read-only through M3 (recommended).** Writes arrive in M4 with
     confirmation.
   - B. Yes, from M2. Higher risk, more review.
4. **Credential storage?**
   - **A. Envelope encryption with a local or cloud KMS (recommended).**
   - B. Mongo field-level encryption library only. Simpler, weaker key
     management.
5. **Host?**
   - **A. Keep Vercel and Mongo Atlas for v1 (recommended),** after verifying
     streamable-http behavior (Section 10).
   - B. Move to a long-running container host if serverless limits bite.

Provider order is not an open decision — it is fixed by the milestone order
in Section 11 (Freshdesk, Asana, HubSpot, Salesforce, then Zoho, Outreach,
SalesLoft).

## 13. Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Client tool-count or description limits cut off tools | Medium | High | Measure before M2. Keep per-provider tools small. |
| Upstream refresh tokens revoked or expire silently | Medium | Medium | `needs_reauth` status and user notice. |
| Secrets in reference code reused by mistake | Medium | High | Do not copy config files. Rotate listed secrets. Secret scanning in CI. |
| Prompt injection via CRM or ticket text triggers writes | Medium | High | Writes need confirmation. Tool descriptions mark results as data. |
| SOQL tool used to read beyond user rights | Low | High | Upstream permissions still apply. Validate SOQL, cap rows, audit. |
| Provider rate limits throttle many users | Medium | Medium | Per-user and per-provider budgets, backoff on 429. |
| Marketplace review blocks public launch | High if public | High | Decision 1: start internal. |
| In-memory state breaks multi-instance deploys | High today | Medium | Fixed in M0a. |
| External app approval lead time (Salesforce Connected App, HubSpot/Zoho/Outreach/SalesLoft app registration) slips M1-M3 | Medium | Medium | Register each app at the start of its milestone, not when code needs it (Section 11). Track approval status separately from code status. |
| Hub master encryption key leaks or needs rotation | Low | High | Define rotation procedure and blast radius before M5 (Section 11 runbook). |

## 14. Acceptance (v1 = end of M3)

- [ ] A new user signs in, connects Salesforce and HubSpot via OAuth, and runs
      a read query from Claude and ChatGPT.
- [ ] Revoking a provider removes its tools from `tools/list` within one
      refresh cycle and deletes stored credentials.
- [ ] A test scans logs, tool results, and the database and finds no
      plaintext token or secret.
- [ ] Hub restarts mid-consent without losing the flow.
- [ ] Each adapter has mocked-HTTP unit tests. Each provider has one sandbox
      smoke test.
- [ ] `freshdesk-connector` existing tests and OAuth flow script still pass.

## 15. Relation to existing docs

- `PLAN.md` describes the current Freshdesk design. After M0a/M0b, update it to
  point here and mark the in-memory sessions and plaintext-key limitations as
  fixed.
- `CAPABILITIES.md` becomes per-provider. Each adapter lists what its agent
  can and cannot do.
- `README.md`: the Mode 1 and Mode 2 split becomes "single-tenant" and "hub".
  Mode 1 stays for operator use.
