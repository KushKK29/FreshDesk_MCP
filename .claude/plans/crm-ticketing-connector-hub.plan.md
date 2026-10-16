# Plan: Connector Hub for CRM and Ticketing Tools

**Source PRD**: `.claude/prds/crm-ticketing-connector-hub.prd.md`
**Selected Milestones**: M0b, M1, M2, M3, M4, M5 (M0a already shipped — `src/auth.py`, `src/login_pages.py`, hub login gate wired into `src/oauth_server.py`)
**Complexity**: Large (multi-month, multiple milestones blocked on external app registrations only the user can do)

**Scope deviation from PRD, confirmed with user**: PRD §4/§9/§12-decision-3 scope read-only, ~8-tool-per-provider, write tools deferred to M4 behind preview+confirm. User explicitly overrode this (session decision) to **full SDK parity per provider** — every object the platform exposes (leads, contacts, accounts, deals, cases, campaigns, tasks, notes, emails, etc.), every action the REST API supports (list/search/get/create/update/delete/convert/merge/bulk), available from the start, not staged behind M4. The one guardrail kept: **preview-then-confirm applies only to delete, send(email), and bulk/mass actions** — plain create/update/read fire immediately. This changes PRD §9's tool table, §12 decision 3, and narrows (does not remove) §7.4. Everything else in the PRD — adapter interface, encrypted vault, connection model, milestone's external-app-registration dependency — is unchanged.

## Summary

M0a (hub user accounts) is done: real email/password login, Mongo-backed sessions, `/oauth/authorize` gated behind hub login. This plan covers the rest: splitting the Freshdesk API key out of the login path into a generic encrypted credential store behind a provider-adapter interface (M0b), then adding upstream-OAuth providers one batch at a time (M1-M3) with full-parity tool surfaces, then audit/hardening (M4) and distribution (M5).

M1-M3 each depend on an external app registration (Asana, then HubSpot+Salesforce, then Zoho+Outreach+SalesLoft) that only the user can start — developer account, redirect URI, in some cases a review queue. Code for each milestone can be written and tested against a sandbox/mocked HTTP layer before that registration completes, but the live smoke test (N5) cannot run until it does. This is flagged per milestone below, not glossed over.

Per-provider tool count is now much larger than the PRD's ~8-tool budget (PRD §10 risk: "client tool-count or description limits cut off tools"). This risk is now live, not hypothetical — see Risks section.

Ladder check before building anything: M0b's "provider adapter interface" is the one real abstraction this plan adds on purpose — PRD explicitly requires it (Goal 3: "adding a provider means one adapter module, no MCP/auth changes") and it currently has zero implementations behind a Protocol, which is exactly the "interface with one implementation" ponytail forbids. Resolved by building adapter + Freshdesk port + Asana adapter in the same milestone pass (M0b bleeds slightly into M1) so the interface proves itself against two real implementations before M2 extends it further — not speculative, PRD-mandated, and validated against 2 providers before trusting it for 5 more.

## Patterns to Mirror

| Category | Source | Pattern |
|---|---|---|
| Naming | `src/auth.py:49-52` | `@dataclass` return types (`HubUser`), module-level `_mongo`/`_db`/`_collection` handles, `AuthError` as the one exception type per module |
| Errors | `src/freshdesk_client.py:23-24,41-43` | One `{Module}Error(Exception)` subclass, raised with upstream status+body in the message; no custom hierarchy |
| Secrets at rest | `src/auth.py:55-56,99-106` | Never store raw secret — SHA-256 hash for lookup (sessions, OAuth tokens); bcrypt for passwords. Comments explicitly call out *why* a field is hashed vs plaintext — same discipline needed for `credentials_enc`, not just "encrypt everything" |
| Mongo collections | `src/oauth_server.py:62-66` | Flat pymongo, no ORM, one `_db[...]` per collection, indexes created at import time (`create_index(..., expireAfterSeconds=...)` for TTL) |
| Import fallback | `src/oauth_server.py:41-46` | `try: import x / except ImportError: from src import x` — supports both `--app-dir src` local dev and Vercel's `src.module:app` package import. Every new src module needs this same try/except at its top if it imports sibling modules |
| Tool registration | `src/mcp_http_server.py:105-128` | `@mcp.tool()` decorated function, docstring doubles as the tool description shown to the model, pulls per-request grant via `_current_access_token()` → `_grant_for_token()`, no shared mutable state across calls |
| Tests | `tests/test_auth.py`, `tests/test_oauth_server.py` | No pytest — plain `assert`-based functions, each starts with `if not MONGO_AVAILABLE: return`, real local Mongo on an isolated `test_*` db (not mongomock), `_reset_db()` helper, `if __name__ == "__main__"` block calls every test function in order and prints `"all checks passed"` |
| HTTP client | `src/freshdesk_client.py:27-43` | One retry on 429 honoring `Retry-After`, flat function not a class, `_for()` suffix variant takes credentials as explicit args (multi-tenant) alongside env-var-reading convenience wrappers (single-tenant) |

No existing pattern for: envelope encryption/KMS, OAuth-to-upstream (as opposed to OAuth-from-client) token refresh, or a Protocol-based adapter registry. State this explicitly rather than inventing a precedent — these are genuinely new to this codebase.

## Full tool surface per provider (supersedes PRD §9)

Generic action set per object, unless noted: `list`, `search`, `get`, `create`, `update`, `delete`. Tool name pattern: `{provider}_{action}_{object}` (e.g. `salesforce_create_lead`, `hubspot_delete_contact`). Destructive/send/bulk actions (marked **PC**) go through preview-then-confirm (Task 16); everything else fires on first call.

| Provider | Objects | Actions beyond generic CRUD |
|---|---|---|
| **Freshdesk** | Ticket, Contact, Company, TimeEntry, CannedResponse, Solution(category/folder/article) | `reply_ticket`, `add_note_ticket` (public/private), `merge_contacts`, `bulk_update_tickets` **PC**, `forward_ticket` **PC** (send) |
| **Asana** | Task, Subtask, Project, Section, Tag, Story(comment), Attachment, Portfolio, CustomField | `complete_task`, `set_parent_task`, `add_followers`/`remove_followers`, `add_tag`/`remove_tag`, `add_dependencies`, `duplicate_project`, `add_comment` (create Story) |
| **HubSpot** | Contact, Company, Deal, Ticket, Lead, Quote, LineItem, Product, Note, Call, Email(engagement), Meeting, Task(engagement) | `associate_objects`/`get_associations` (v4), `search_objects` (per-type), `batch_create`/`batch_update`/`batch_archive` **PC** (bulk), `enroll_in_workflow` |
| **Salesforce** | Lead, Contact, Account, Opportunity, Case, Campaign, Task, Event, Note, EmailMessage, User | `query_soql` (SELECT-only, capped — defense-in-depth per PRD §9), `search_sosl`, `convert_lead`, `merge_records` **PC**, `bulk_ingest` **PC** (Bulk API 2.0: insert/update/upsert/delete), `send_email` **PC** |
| **Zoho CRM** | Lead, Contact, Account, Deal, Task, Event, Call, Campaign, Product, Note, Attachment | `query_coql`, `upsert_record`, `convert_lead`, `mass_convert_leads` **PC**, `bulk_read`/`bulk_write` **PC**, `get_related_records` |
| **Outreach** | Prospect, Account, Sequence, SequenceStep, SequenceState, Mailbox(read-only), Call, Task, Template | `enroll_in_sequence` (create SequenceState), `pause_sequence`/`resume_sequence`/`finish_sequence`, `log_call`, `request_compliance_deletion` **PC** (GDPR delete) |
| **SalesLoft** | Person, Account, Cadence(read-mostly), CadenceMembership, Step(read), Activity(email/call, read) | `enroll_in_cadence` (create CadenceMembership), `pause_membership`/`resume_membership`/`remove_membership` **PC** |

Note on sourcing: object/action lists above were pulled from each platform's public REST docs via research pass; a few specifics (Salesforce REST lead-convert exact path/version, SalesLoft's native send-email endpoint, Freshdesk v2 canned-response paths) came from secondary sources and need a final direct-docs confirmation per adapter task below before locking that adapter's tool schema — flagged inline in each adapter's task, not silently assumed correct.

## Files to Change

| File | Action | Why |
|---|---|---|
| `src/vault.py` | CREATE | Envelope encryption for `credentials_enc`: data key per record wrapped by a local master key (env var `VAULT_MASTER_KEY`, 32 raw bytes, base64). `encrypt(plaintext: dict) -> bytes`, `decrypt(blob: bytes) -> dict`. Stdlib-first: add `cryptography` as a direct dependency (AES-256-GCM via `cryptography.hazmat`), skip rolling custom crypto |
| `src/adapters/__init__.py` | CREATE | `ProviderAdapter` Protocol (PRD §6.2) + `ToolSpec`/`Connection`/`AuthMode` dataclasses + a flat `ADAPTERS: dict[str, ProviderAdapter]` registry populated by each adapter module importing and registering itself |
| `src/adapters/freshdesk.py` | CREATE | Port `freshdesk_client.py`'s existing `*_for()` functions behind the adapter interface — `auth_modes={API_KEY}`, tools = `list_tickets`/`get_ticket`/`search_tickets` (unchanged names/behavior per M0b exit criteria) |
| `src/connections.py` | CREATE | `connections` Mongo collection (PRD §7.2 schema) + CRUD: `create_connection`, `get_connection`, `list_connections_for_user`, `disconnect`, `mark_needs_reauth`. Calls `vault.encrypt`/`decrypt` at the boundary — nothing outside this module touches plaintext credentials |
| `src/oauth_server.py` | UPDATE | `/oauth/authorize/decision`'s Freshdesk-domain-and-key form becomes a generic "connect a provider" step that writes through `connections.create_connection` instead of embedding `freshdesk_domain`/`freshdesk_api_key` directly in the `_grants` doc. Wire `/oauth/revoke` to also call `connections.disconnect` for the matching connection |
| `src/mcp_http_server.py` | UPDATE | Tool list built from `ADAPTERS` + the caller's active `connections` rows instead of 3 hardcoded `@mcp.tool()` Freshdesk functions — PRD F3/Goal 3. Freshdesk tool behavior must stay identical (M0b exit criteria) |
| `src/connected_accounts.py` | CREATE | "Connected accounts" page (PRD F10): list connections, status, last_used, a Disconnect button per row, an API-key connect form for Freshdesk (no upstream OAuth app exists yet at M0b) |
| `src/connect_oauth.py` | CREATE (M1) | Generic upstream-OAuth connect flow: `/connect/{provider}/start` (state+PKCE where supported) → provider consent → `/connect/{provider}/callback`, writes a `connections` row via `connections.create_connection`. One module, provider-parametrized — not one file per provider, since the state/PKCE/callback shape is identical across providers (PRD M1 calls this out as "generic") |
| `src/adapters/asana.py` | CREATE (M1) | `auth_modes={OAUTH2}`, `refresh()`, tools: `list_projects`/`list_tasks`/`get_task`/`search_tasks` |
| `src/adapters/hubspot.py`, `src/adapters/salesforce.py` | CREATE (M2) | Per PRD §9 tool lists; Salesforce `search_records` takes SOQL — `SELECT`-only / no DML / `LIMIT`-capped regex-level filter as defense-in-depth only, real control is the user's own OAuth-scoped session (PRD §9 explicit) |
| `src/adapters/zoho.py`, `src/adapters/outreach.py`, `src/adapters/salesloft.py` | CREATE (M3) | Zoho adapter stores `dc` (`eu`/`au`/`in`/`jp`/`uk`/`us`) in the connection record and resolves API host from it, never from user input (SSRF guard, PRD §7.5) |
| `src/audit_log.py` | CREATE (M4) | `audit_log` collection: user, provider, tool, target id, timestamp — no payload bodies (PRD §7.4) |
| write-tool preview/confirm plumbing | UPDATE adapters (M4) | Each write tool returns a preview on first call, executes only given a confirmation token from that preview |
| `tests/test_vault.py`, `tests/test_connections.py`, `tests/test_adapters_freshdesk.py` | CREATE | Mirror `tests/test_auth.py` shape exactly |
| per-provider `tests/test_adapters_{provider}.py` | CREATE (per milestone) | Mocked-HTTP unit tests (PRD N5) — use `httpx`'s `MockTransport`, no new mocking dependency |

## Tasks

### M0b — Credential split and adapter base

#### Task 1: Vault (envelope encryption)
- **Action**: `src/vault.py`. Master key from `VAULT_MASTER_KEY` env var (32-byte, base64). Per-record: generate a random 32-byte data key, encrypt the data key with the master key (AES-256-GCM), encrypt plaintext with the data key (AES-256-GCM), store `{wrapped_data_key, nonce, ciphertext}` as one base64 blob in `credentials_enc`.
- **Mirror**: `auth.py`'s "only store a hash, never the raw value" discipline — extend it to "only store ciphertext, never the plaintext," with a comment stating the exact threat this stops (DB dump alone ≠ token use, PRD N3).
- **Validate**: `tests/test_vault.py` — encrypt/decrypt round-trip, tampered ciphertext raises, missing `VAULT_MASTER_KEY` raises clearly at import time not silently.

#### Task 2: Adapter interface + registry
- **Action**: `src/adapters/__init__.py` defines `ProviderAdapter` Protocol exactly per PRD §6.2 (`key`, `display_name`, `auth_modes`, `scopes`, `tools()`, `call()`, `refresh()`, `verify()`), plus `ToolSpec` (name, description, input schema, `readOnlyHint`/`destructiveHint`/`idempotentHint`/`openWorldHint`), plus a module-level `ADAPTERS: dict[str, ProviderAdapter]` that each adapter module appends itself into on import.
- **Mirror**: `mcp_http_server.py`'s existing `@mcp.tool()` functions become the reference behavior the Freshdesk adapter's `tools()`/`call()` must reproduce exactly (M0b exit criteria: "no change in tool names or behavior").
- **Validate**: no test of its own (a Protocol has no behavior) — proven by Task 3's tests passing unchanged.

#### Task 3: Port Freshdesk to the adapter + expand to full object/action coverage
- **Action**: `src/adapters/freshdesk.py` wraps `freshdesk_client.py`'s existing `*_for()` functions (`list_tickets`/`get_ticket`/`search_tickets`, unchanged — M0b exit criteria) as `call()` dispatch, then adds the rest of the full tool surface (table above): Contact/Company/TimeEntry/CannedResponse/Solution CRUD, `reply_ticket`, `add_note_ticket`, `merge_contacts`. `bulk_update_tickets` and `forward_ticket` (send) are **PC**-gated (Task 16) — the only two Freshdesk actions needing the confirm step. `verify()` does the same `GET /tickets?per_page=1` check that's inline in `oauth_server.py:540-549` today, moved here so it's reusable per-provider at connect time, not Freshdesk-specific code sitting in the OAuth server.
- **Mirror**: `freshdesk_client.py:27-43`'s HTTP/retry pattern stays untouched — adapter is a thin wrapper, not a rewrite.
- **Validate**: `tests/test_adapters_freshdesk.py` using `httpx.MockTransport` — the original 3 tool calls (unchanged behavior, same assertions as `tests/test_freshdesk_client.py`) plus one test per new object family, plus a test confirming `bulk_update_tickets`/`forward_ticket` require a confirm token.

#### Task 4: Connections store
- **Action**: `src/connections.py`, schema exactly per PRD §7.2. `create_connection(user_id, provider, auth_type, credentials: dict, scopes, expires_at=None)` calls `vault.encrypt` internally; `get_connection(user_id, provider)` calls `vault.decrypt` internally — no caller outside this module ever sees `credentials_enc` directly, matching the "tokens never appear outside the module that needs them" principle already used for session cookies in `auth.py`.
- **Mirror**: `auth.py`'s collection-handle + index-at-import-time pattern.
- **Validate**: `tests/test_connections.py` — round-trip create/get, `disconnect` deletes the row, a second `create_connection` for the same `(user_id, provider)` overwrites (PRD's stated v1 non-goal: no multi-account-per-provider).

#### Task 5: Rewire oauth_server.py's Freshdesk connect step onto the connections store
- **Action**: `/oauth/authorize/decision`'s approve branch calls `adapters.freshdesk.verify()` (replacing the inline `httpx.get` check) then `connections.create_connection(hub_user_id, "freshdesk", "api_key", {...})` instead of writing `freshdesk_domain`/`freshdesk_api_key` straight onto the `_grants` doc. The `_grants` doc keeps only `user_id` — tool calls look up the connection by `user_id` + provider at call time.
- **Mirror**: existing error-page branches on `verify()` raising stay the same shape (401 → "couldn't verify credentials" page).
- **Validate**: `tests/test_oauth_server.py` — existing tests must still pass unchanged (M0b exit criteria: "existing tests pass"); add one asserting `_grants` no longer contains a `freshdesk_api_key` field after a successful approve.

#### Task 6: Update mcp_http_server.py to read tools from the adapter registry
- **Action**: Replace the 3 hardcoded `@mcp.tool()` functions with a loop over `ADAPTERS[provider].tools()` for the providers the calling user has active connections for, registering each dynamically with FastMCP. `_current_access_token()`/`_grant_for_token()` stay; add a lookup from grant's `user_id` to that user's `connections` rows.
- **Mirror**: same `_current_access_token()` plumbing already in the file — only the registration loop is new.
- **Validate**: manual smoke test (below) — `list_tickets`/`get_ticket`/`search_tickets` still show up and behave identically from a real MCP client.

#### Task 7: Connected accounts page + wire /oauth/revoke
- **Action**: `src/connected_accounts.py`, a `GET /connected-accounts` page (table: provider, status, last_used, scopes, Disconnect button) behind the hub-login gate, mounted the same way `login_pages.router` is. Disconnect posts to `/oauth/revoke` with the connection id; `revoke()` in `oauth_server.py` additionally calls `connections.disconnect`.
- **Mirror**: `login_pages.py`'s `APIRouter` + `_PAGE_STYLES` reuse pattern exactly.
- **Validate**: manual — sign in, connect Freshdesk, see it listed, disconnect, confirm `connections` row and `credentials_enc` are gone and tools disappear from a fresh `tools/list`.

### M1 — Upstream OAuth and Asana
*Blocked on: user registering an Asana developer app + redirect URI before live testing. Code below is written and unit-tested against mocked HTTP regardless of registration status; flag this to the user as a parallel action item, do not wait on it to start coding.*

#### Task 8: Generic upstream OAuth connect flow
- **Action**: `src/connect_oauth.py` — `/connect/{provider}/start` builds the provider's authorize URL from a per-provider config (`client_id`, `authorize_url`, `token_url`, `scopes`) stored in a small `PROVIDER_OAUTH_CONFIG: dict[str, dict]` in the same file (not one file per provider — PRD explicitly calls this flow "generic"), generates `state` (+ PKCE verifier/challenge where the provider supports it), stores a short-lived pending-connect record (same TTL-indexed Mongo pattern as `_pending_authorizations`). `/connect/{provider}/callback` validates `state`, exchanges `code` for tokens, calls the adapter's `verify()`, then `connections.create_connection`.
- **Mirror**: `oauth_server.py`'s `_pending_authorizations` TTL-indexed collection and single-use `find_one_and_delete` consumption pattern.
- **Validate**: `tests/test_connect_oauth.py` with `httpx.MockTransport` standing in for the provider's token endpoint — state mismatch rejected, expired pending record rejected, successful exchange writes a connection.

#### Task 9: Refresh + needs_reauth
- **Action**: Each OAuth adapter's `refresh()` is called lazily by `mcp_http_server.py`'s tool-call path when `expires_at` is near/past, or on a single 401 retry (PRD F5). On refresh failure, `connections.mark_needs_reauth` sets status and the tool call returns an error telling the user to reconnect — no retry loop (PRD §7.3).
- **Mirror**: `freshdesk_client.py`'s "one retry, not a tower" discipline — same ceiling applied to refresh-on-401.
- **Validate**: mocked test — expired token + mocked 401 triggers exactly one refresh attempt, not a loop; failed refresh sets `needs_reauth`.

#### Task 10: Asana adapter — full object/action coverage
- **Action**: `src/adapters/asana.py` — `auth_modes={OAUTH2}` (PRD notes a personal access token also works for internal testing; support both via two `auth_modes` entries so internal testing isn't blocked on app approval). Tools per the full tool surface table above: Task/Subtask/Project/Section/Tag/Story/Attachment/Portfolio/CustomField CRUD plus `complete_task`, `set_parent_task`, `add_followers`/`remove_followers`, `add_tag`/`remove_tag`, `add_dependencies`, `duplicate_project`, `add_comment`. No preview+confirm gate on this provider — Asana has no destructive bulk/send action in its API surface (deletes are single-object, scoped to the task/project the user already owns).
- **Mirror**: `adapters/freshdesk.py`'s shape from Task 3.
- **Validate**: `tests/test_adapters_asana.py` mocked, one test per action family (not per individual tool — group CRUD tests, separate tests for the special actions); live smoke test only once the user's Asana app registration lands (flagged as external dependency, not skipped silently).

### M2 — HubSpot and Salesforce
*Blocked on: Salesforce Connected App + HubSpot app registration, same parallel-track note as M1.*

#### Task 11: Per-provider rate budget
- **Action**: extend `freshdesk_client.py`'s single-retry-on-429 pattern into a shared `src/common_http.py` helper (`request_with_backoff(method, url, provider, **kwargs)`) parametrized per provider's own rate-limit headers, used by every adapter from here on — including going back to wrap Freshdesk's own calls, so there's one rate-limit code path, not N copies (ladder rung 2: reuse, don't reinvent per adapter).
- **Validate**: mocked test — 429 triggers exactly one retry honoring `Retry-After`; near-limit header triggers the same flat backoff Freshdesk already does.

#### Task 12: HubSpot adapter — full object/action coverage
- **Action**: `src/adapters/hubspot.py`, OAuth2 + refresh (reference pattern noted in PRD: `humantic_enrichment/api/hubspot_client.js` — read for the refresh-timing approach only, do not copy any stored secret or plaintext-token pattern from it per PRD §7.3). Tools per the tool surface table: Contact/Company/Deal/Ticket/Lead/Quote/LineItem/Product CRUD, Note/Call/Email/Meeting/Task engagement CRUD, `associate_objects`/`get_associations` (Associations v4), `search_objects` per-type, `enroll_in_workflow`. `batch_create`/`batch_update`/`batch_archive` are the **PC**-gated bulk actions (Task 16).
- **Validate**: `tests/test_adapters_hubspot.py` mocked, grouped by object family; sandbox smoke test once app registration completes.

#### Task 13: Salesforce adapter — full object/action coverage + SOQL guard
- **Action**: `src/adapters/salesforce.py`. Instance URL taken only from the OAuth token response (never user input — SSRF guard PRD §7.5). Tools per the table: Lead/Contact/Account/Opportunity/Case/Campaign/Task/Event/Note/EmailMessage/User CRUD, `query_soql`, `search_sosl`, `convert_lead`. `query_soql` applies a `SELECT`-only / no-DML / `LIMIT`-capped filter before sending, with an explicit comment (mirroring PRD §9's own language) that this is defense-in-depth, not the access-control boundary — the user's OAuth-scoped session is. `merge_records`, `bulk_ingest` (Bulk API 2.0), and `send_email` are **PC**-gated (Task 16) — send and merge/bulk are exactly the destructive/send/bulk category the narrowed safety net targets. Flag in the plan (not silently defer): PRD requires "its own security review before M2 ships" for the SOQL tool specifically, now broadened in scope since full parity adds more write surface area than the PRD's read-only design assumed — surface that to the user as a checkpoint, don't self-approve it.
- **Validate**: mocked test asserting the SOQL filter rejects `INSERT`/`UPDATE`/`DELETE`/multi-statement SOQL and accepts a plain `SELECT ... LIMIT n`; a separate mocked test per **PC** action confirming it returns a preview and performs no upstream call without a confirm token; sandbox smoke test once Connected App exists.

### M3 — Zoho, Outreach, SalesLoft
*Blocked on: three more app registrations, same parallel-track note.*

#### Task 14: Zoho adapter — full object/action coverage + DC handling
- **Action**: `src/adapters/zoho.py` stores `dc` on the connection (from the OAuth response, PRD §7.2), resolves `https://www.zohoapis.{dc}/...` from a fixed allowlist of the 6 known DCs — never interpolating a user-supplied string into the host (SSRF guard). Tools per the table: Lead/Contact/Account/Deal/Task/Event/Call/Campaign/Product/Note/Attachment CRUD, `query_coql`, `upsert_record`, `convert_lead`, `get_related_records`. `mass_convert_leads` and `bulk_read`/`bulk_write` (Bulk API, job-based async) are **PC**-gated bulk actions.
- **Validate**: mocked test — unknown DC value rejected before any HTTP call is made; separate test confirming **PC** actions require a confirm token.

#### Task 15: Outreach + SalesLoft adapters — full object/action coverage
- **Action**: `src/adapters/outreach.py` — Prospect/Account/Sequence/SequenceStep/Template CRUD, `enroll_in_sequence`, `pause_sequence`/`resume_sequence`/`finish_sequence`, `log_call` (Call create), Mailbox read-only. `request_compliance_deletion` (GDPR prospect delete) is **PC**-gated. `src/adapters/salesloft.py` — Person/Account CRUD, Cadence/Step/Activity read, `enroll_in_cadence` (create CadenceMembership); `pause_membership`/`resume_membership`/`remove_membership` **PC**-gated. Reference configs noted in PRD (`humantic_apis/config/outreach.config.js`, `.../salesloft.config.js` — read for OAuth URLs/scopes only, do not copy stored secrets). Flag for final-docs check before lock: SalesLoft's native send-email endpoint wasn't confirmed against primary docs during scoping — verify against developers.salesloft.com directly before shipping any SalesLoft send-capable tool.
- **Validate**: mocked tests per adapter, grouped by object/action family; exit criteria "all seven providers connect and list tools" checked via a combined manual `tools/list` smoke test across all 7.

### M4 — Hardening

Writes are no longer staged to M4 (full-parity scope decision above) — every adapter ships its complete tool surface, including create/update/delete, as it's built in M0b-M3. **PC** (preview-then-confirm) is a shared mechanism those adapters call into, built once here in M4 so it exists before M2's Salesforce `send_email`/`merge_records`/`bulk_ingest` need it — meaning Task 16 below must land ahead of or alongside Task 13, not strictly after. Sequencing note, not a milestone reorder: build Task 16 first if M2 starts before M4 in practice.

#### Task 16: Preview-then-confirm mechanism (destructive/send/bulk only)
- **Action**: `src/confirm_tokens.py` — one shared helper used by every adapter's **PC**-marked actions (delete, email/send, merge, bulk/mass operations — see the tool surface table's **PC** markers per provider). First call with no `confirm_token` returns `{preview: {...}, confirm_token}` and performs no upstream call. Second call with a valid, unexpired `confirm_token` executes exactly once. `confirm_token` is a short-lived signed/hashed value (same `secrets.token_urlsafe` + hash-at-rest pattern as everywhere else in this codebase), not a new mechanism. Plain create/update/read actions never touch this path — they execute immediately, per the session's scope decision.
- **Validate**: mocked test — call without confirm_token returns preview only and performs no upstream write; call with a stale/wrong/reused token is rejected; call with a valid token performs exactly one upstream write; a non-PC action (e.g. plain `create_contact`) executes with no confirm_token round-trip at all.

#### Task 17: Audit log
- **Action**: `src/audit_log.py` — one `log_tool_call(user_id, provider, tool, target_id, status)` function, called from `mcp_http_server.py`'s tool-call path for every call (not just writes, per PRD N4), no payload bodies.
- **Validate**: `tests/test_audit_log.py` — a logged call never contains a secret-shaped string (reuse the same "token not in str(doc)" assertion style as `tests/test_auth.py:100`).

#### Task 18: Connection health page + tool toggles
- **Action**: extend `src/connected_accounts.py` (Task 7) with per-tool enable/disable checkboxes persisted on the `connections.enabled_tools` field already in the PRD §7.2 schema; `mcp_http_server.py`'s tool-registration loop (Task 6) filters by it.
- **Validate**: manual — disabling one tool removes only that tool from the next `tools/list`, others on the same provider stay.

#### Task 19: Security review checkpoint
- **Action**: Before closing M4, re-run the SOQL guard tests (Task 13) and a log/DB scan for plaintext tokens (PRD Acceptance checklist item 3) as an explicit, separate step — not folded silently into "tests pass."
- **Validate**: a one-off script scanning the test DB dump + log output for any string matching a bearer-token/API-key shape.

### M5 — Distribution

#### Task 20: Operator runbook
- **Action**: a markdown runbook (not code) covering: rotating upstream secrets per provider, rotating `VAULT_MASTER_KEY` and the re-encryption procedure for every `credentials_enc` row, revoke-storm handling, rate-limit monitoring. This is documentation the PRD explicitly asks for — write it, don't skip it as "non-code."
- **Validate**: n/a (documentation).

#### Task 21: Directory/marketplace submissions
- **Action**: per-provider submission steps, entirely dependent on each platform's own process — cannot be scripted or tested locally. Flag to the user as manual, external-lead-time work, same as the app-registration dependencies in M1-M3.

## Validation

```bash
# Requires local mongod running (same as existing tests):
cd /home/kush-goel/RazorPay_assignment/freshdesk-connector
python tests/test_auth.py
python tests/test_oauth_server.py
python tests/test_vault.py
python tests/test_connections.py
python tests/test_adapters_freshdesk.py
# ...one per adapter added, same invocation style, as each lands

# Manual MCP smoke test (mirrors existing oauth flow verification):
uvicorn src.mcp_http_server:app --reload --app-dir src --port 8004
# connect a real MCP client (Claude Code / Claude.ai custom connector) to
# http://localhost:8004, run the OAuth flow, confirm tools appear and work.
```

## Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| External app registration (Asana/Salesforce/HubSpot/Zoho/Outreach/SalesLoft) blocks live smoke tests | High | Code + mocked-HTTP tests proceed independent of registration status (PRD-acknowledged); flag each milestone's live-test gap to the user explicitly rather than silently marking it done |
| Adapter Protocol designed before 2nd implementation exists, shape turns out wrong | Medium | M0b bundles Freshdesk port + validates against Asana's shape in M1 before M2-M3 lock in further providers |
| Vault master-key handling: a local env var is weaker than a real KMS (PRD §12 decision 4 flags this as the B option) | Medium | Decision 4 recommends local/cloud KMS "where available" — ship local env-var master key for v1, note KMS as the upgrade path in the vault.py module docstring (ponytail: ceiling = single static key in env, upgrade to KMS when multi-instance key rotation is needed) |
| Salesforce SOQL tool read-beyond-rights | Low likelihood, high impact | Task 13's explicit security-review checkpoint before M2 ships, per PRD §9 |
| Rewiring oauth_server.py's Freshdesk connect path breaks the existing OAuth flow | Medium | Task 5's validate step requires existing `tests/test_oauth_server.py` to keep passing unchanged — this is the M0b exit criterion, not optional |
| Full-parity tool count (dozens per provider, ~7 providers) exceeds Claude/ChatGPT client tool-count or description-length limits (PRD §10, previously hypothetical, now live given the scope decision) | High | Check client limits before M2 (PRD §10 says to do this anyway); if exceeded, the per-user `tools/list` filter (Task 6/18, already filtered by active connections + `enabled_tools`) is the mitigation lever — user disables unused tools/objects per provider rather than the hub shrinking scope unilaterally |
| Full parity removes the PRD's read-first staging — a newly connected write-capable provider is live immediately, with only **PC** (destructive/send/bulk) gated, not plain create/update | Medium | This is the session's explicit scope decision, not an oversight — documented at the top of this plan. Confirm-token mechanism (Task 16) still gates the highest-blast-radius actions (delete/send/bulk); plain create/update rely on the adapter's own input validation plus Task 17's audit log for after-the-fact visibility |
| Prompt injection via CRM/ticket text triggers a write (PRD §13 risk) is now broader: a create/update tool fires with no confirm step at all, only delete/send/bulk do | Medium-High | Tool descriptions must state results are data not instructions (PRD §7.5, unchanged); audit log (Task 17) is the primary detective control for create/update since there's no preventive gate on them by design |

## Acceptance
- [ ] M0b: existing tests pass; Freshdesk works from Claude/ChatGPT exactly as before, behind hub login + encrypted credential store
- [ ] M1: connect/disconnect Asana from the UI; tools appear and work in Claude
- [ ] M2: HubSpot + Salesforce read tools pass sandbox smoke tests from all three clients
- [ ] M3: all seven providers connect and list tools
- [ ] M4: write path tested; SOQL + token-storage security review done
- [ ] M5: runbook written; submissions tracked as external/manual
- [ ] Patterns mirrored (flat pymongo, hash-not-plaintext, one-retry HTTP, no-pytest assert-style tests) — not reinvented per provider
