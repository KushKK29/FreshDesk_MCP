# Power Connector: Cross-System Workflow Tools

**Status**: Draft, awaiting decisions in Section "Open Decisions"
**Owner**: Kush Goel
**Date**: 2026-10-17
**Builds on**: `freshdesk-connector` — specifically the adapter framework and
pseudo-provider pattern already shipped in `src/crm_ops.py`, and the
provider clients already present for all 7 systems (`salesforce_client.py`,
`hubspot_client.py`, `zoho_client.py`, `salesloft_client.py`,
`outreach_client.py`, `asana_client.py`, `freshdesk_client.py`).
**Relation to other docs**: `crm-ticketing-connector-hub.prd.md` specifies
the *infrastructure* (OAuth, adapter contract, vault, hub accounts). This
PRD specifies a *workflow layer* on top of that infra — cross-provider
analysis and automation tools, the same category as the 3 tools already in
`crm_ops.py`. It does not replace or restate that PRD.

---

## Problem Statement

A RevOps/sales/support user with CRM (Salesforce/HubSpot/Zoho), sales
engagement (Salesloft/Outreach), support (Freshdesk), and delivery (Asana)
accounts connected has no single place to ask cross-system questions —
"which deals are stalled," "which tickets risk a renewal," "what's overdue
across all my tools." Each tool answers only questions about itself. Today
that means manually opening 4-7 tabs and cross-referencing by hand.

## Evidence

- Assumption, not yet validated by a real user — this repo is a build
  exercise, not a live product with users yet. Flagging rather than
  inventing a quote.
- Indirect evidence: the repo owner already built 3 cross-CRM tools
  (`find_duplicates`, `data_quality_audit`, `customer_360` in
  `src/crm_ops.py`) before this PRD existed, which is itself a signal the
  cross-system gap is real enough to have started solving it ad hoc.

## Proposed Solution

Extend the existing `crm_ops.py` pseudo-provider pattern — a tool module
with no OAuth of its own, reading data through connections the user already
made — into a small number of additional pseudo-providers, one per business
outcome category (support-ops, sales-ops, project-ops, reporting-ops). Each
exposes a handful of read-only MCP tools that join data across the
providers the user has connected, following the exact dispatch and
registration pattern `crm_ops.py` already establishes. Write actions (e.g.
"create Asana task from ticket") stay out of this PRD's MVP — they need the
preview-then-confirm mechanism the infra PRD defers to its own M4.

## Key Hypothesis

We believe read-only cross-system query tools (customer 360 for support,
SLA risk, stalled-deal detection, daily briefing) will reduce the
manual-tab-switching cost for RevOps/CS users enough to be used daily.
We'll know we're right when a user runs at least 3 of these tools from
Claude/ChatGPT in a session without falling back to opening the native
app UIs for the same question.

## What We're NOT Building

- Any write tool (ticket-to-task creation, sequence enrollment, CRM field
  sync) — needs the confirm-token write path, out of scope until that
  infra lands (tracked in the hub PRD's M4).
- Fuzzy/phone/name duplicate matching — `find_duplicates` is exact-email
  only today (`crm_ops.py:25`); extending match strategy is a separate,
  smaller follow-up, not bundled here.
- Scheduling/automation (#48 "scheduled operational audits", #44 audit
  trail, #46 usage analytics) — these are platform/ops features for the
  hub itself, not workflow tools, and belong with the infra PRD if ever
  built.
- A unified data model across CRMs — explicitly deferred in the infra PRD
  (Decision 2); this PRD inherits that constraint.
- Natural-language search (#39) — this is a client-side (Claude/ChatGPT)
  capability once enough tools exist, not a new tool to build.

## Success Metrics

| Metric | Target | How Measured |
|--------|--------|---------------|
| New pseudo-provider tools shipped | 6-8 (MVP set below) | Count of `ToolSpec`s registered |
| Tool call success rate (no fetch_errors swallowing data) | >95% per provider on sandbox accounts | `providers_skipped` field in tool output, measured in smoke tests |
| Session completeness | user answers a cross-system question with zero native-UI tab switches | Manual test script per MVP tool |

## Open Questions

- [ ] Does the repo owner have live/sandbox accounts for Salesloft, Outreach,
      and Asana to test against, or only Freshdesk + the CRMs already in
      `crm_contacts.py`? Determines whether category B/C/D tools can be
      smoke-tested before claiming done.
- [ ] Freshdesk ticket schema fields for SLA (`due_by`, `fr_due_by`) — needs
      checking against `freshdesk_client.py` before committing to SLA Risk
      Monitor's exact logic.
- [ ] Is "high-value account" defined by deal amount, a CRM field, or a
      manual tag? No such field exists in `crm_contacts.py`'s normalized
      shape today — needs a decision before building #20/#25.

---

## Users & Context

**Primary User**
- **Who**: A single RevOps/CS operator (the repo owner, initially) who has
  connected several of the 7 providers via the hub.
- **Current behavior**: Opens each tool's own UI and manually cross-checks.
- **Trigger**: A support ticket comes in, or a weekly review, and they want
  one cross-system answer instead of four lookups.
- **Success state**: Asks Claude/ChatGPT a cross-system question and gets
  a cited, multi-provider answer in one tool call.

**Job to Be Done**
When a ticket/deal/lead needs context from another system, I want to query
it from one place, so I can act without switching tools.

**Non-Users**
Not building for: unauthenticated/anonymous users, multi-tenant SaaS
customers (that's the hub PRD's concern), or write-heavy automation users
(deferred).

---

## Solution Detail

### Core Capabilities (MoSCoW)

Scoped against the 48-item catalogue. Numbers refer to the catalogue.

| Priority | Capability | Rationale |
|----------|------------|-----------|
| Must | #19 Customer 360 for support agents (Freshdesk + CRM) | Freshdesk client + CRM contacts both already exist; highest-leverage, smallest join |
| Must | #23 SLA risk monitor (Freshdesk only) | No CRM join needed; pure Freshdesk, fast to ship, clear value |
| Must | #24 Repeated issue detector (Freshdesk only) | Same — single-provider, no new client needed |
| Must | #03 Field conflict resolver | Already have the data from `find_duplicates`; this is the conflict half split into its own tool with full detail, not just the summary `conflicting_fields` dict |
| Should | #17 Stalled opportunity recovery (CRM only) | Needs deal/opportunity fields `crm_contacts.py` doesn't normalize yet — scope check first |
| Should | #11 Unworked lead detector (CRM + Salesloft/Outreach) | High sales value, needs engagement-activity read from Salesloft/Outreach clients (exist, unused by crm_ops yet) |
| Should | #40 Daily operational briefing | Cheap to build once #23/#17/#11 exist — mostly a summarizing wrapper |
| Could | #28 Unresolved-ticket account report | Freshdesk + CRM join, lower urgency than #19 |
| Could | #16 Rep workload analysis | Needs ownership fields not yet normalized |
| Won't (v1) | #08, #15, #18, #22, #26, #29-38, #44-48 | Writes, scheduling, or need infra (webhooks, audit log) not yet built |

### MVP Scope

The 4 "Must" tools above, each as a new or extended pseudo-provider tool,
following `crm_ops.py`'s existing pattern exactly: no new OAuth, reads
through existing connections, registered via `register()`.

### User Flow

User (via Claude/ChatGPT, already connected to Freshdesk + at least one
CRM) asks a support/sales question → model calls the relevant
`*_ops_*` tool → tool fetches from already-connected providers, joins by
email or ticket-contact link → returns one cited JSON result → model
answers with sources.

---

## Technical Approach

**Feasibility**: HIGH — the pattern, dispatch path, and all provider
clients already exist. This is mostly new pure-Python join logic plus
`ToolSpec` registration, no new infrastructure.

**Architecture Notes**
- New pseudo-provider modules follow `src/crm_ops.py`'s shape: module-level
  functions + a `*Adapter` class with empty `auth_modes`, registered via
  `register()`.
- Freshdesk-CRM joins need a link key. `crm_contacts.py`'s normalized shape
  has `email`; Freshdesk tickets have `requester_email` or similar — must
  confirm the exact field in `freshdesk_client.py` before coding #19/#28.
- Sales-engagement joins (Salesloft/Outreach) need their clients' existing
  read methods inspected — not yet used anywhere outside their own
  adapters, so the "activity" shape they return is unknown until read.

**Technical Risks**

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| Freshdesk/CRM contacts don't share a clean join key | Medium | Check `freshdesk_client.py` field names before coding #19; fall back to fuzzy match only as a Should-have |
| Salesloft/Outreach client methods don't expose what's needed for #11/#17 | Medium | Read `salesloft_client.py`/`outreach_client.py` first; descope to CRM-only version if missing |
| Tool count creep hits client tool-list limits (flagged in infra PRD Section 10) | Low (only 4 new tools in MVP) | Keep MVP to 4 tools, re-check budget before adding Should-haves |

---

## Implementation Phases

| # | Phase | Description | Status | Parallel | Depends | PRP Plan |
|---|-------|-------------|--------|----------|---------|----------|
| 1 | Freshdesk field audit | Confirm ticket requester/email field and SLA (`due_by`) field names | pending | with 2 | - | - |
| 2 | Field conflict resolver tool | Split full conflict detail out of `find_duplicates` into `crm_ops_field_conflicts` | pending | with 1 | - | - |
| 3 | Customer 360 for support (#19) | New `support_ops.py` pseudo-provider: Freshdesk ticket + CRM contact join | pending | - | 1 | - |
| 4 | SLA risk monitor (#23) | Add tool to `support_ops.py`: Freshdesk-only, no CRM join | pending | with 5 | 1 | - |
| 5 | Repeated issue detector (#24) | Add tool to `support_ops.py`: Freshdesk-only, group by subject similarity | pending | with 4 | 1 | - |
| 6 | Should-haves (#17, #11, #40) | Only after MVP ships and Salesloft/Outreach client shapes are confirmed | pending | - | 1-5 | - |

### Phase Details

**Phase 1: Freshdesk field audit**
- **Goal**: Know the exact field names for requester email and SLA due date
  before writing join/filter logic that guesses at them.
- **Scope**: Read `src/freshdesk_client.py` and one live/sandbox API
  response; no code changes.
- **Success signal**: A short note (in the PRP plan, not this PRD) listing
  the confirmed field names.

**Phase 2: Field conflict resolver (#03)**
- **Goal**: Expose the conflict data `find_duplicates` already computes as
  its own tool with full per-record detail (not just the field-name
  summary it returns today).
- **Scope**: One new function in `crm_ops.py` or a new tool entry reusing
  `find_duplicates`'s grouping; no new provider reads.
- **Success signal**: Tool returns conflicts for a seeded cross-CRM
  duplicate with full before/after values per provider.

**Phase 3: Customer 360 for support agents (#19)**
- **Goal**: Given a Freshdesk ticket ID, return the matching CRM contact,
  company, and owner.
- **Scope**: New `src/support_ops.py` pseudo-provider; one tool
  `support_ops_ticket_360`.
- **Success signal**: Given a sandbox ticket with a requester email that
  also exists in a connected CRM, tool returns both records joined.

**Phase 4: SLA risk monitor (#23)**
- **Goal**: List open tickets approaching their SLA deadline.
- **Scope**: Freshdesk-only read, sort by due date, no CRM join.
- **Success signal**: Sandbox tickets near/at/past SLA sort correctly by
  urgency.

**Phase 5: Repeated issue detector (#24)**
- **Goal**: Group tickets with similar subjects per customer/account.
- **Scope**: Freshdesk-only read; simple subject-string grouping (exact or
  near-exact), not ML clustering — ponytail: naive string match is the
  ceiling here, upgrade to embedding similarity only if false negatives
  show up in real use.
- **Success signal**: Seeded duplicate-subject tickets group together.

**Phase 6: Should-haves**
- Gated on Phase 1-5 landing and on reading the Salesloft/Outreach client
  method signatures to confirm they expose what #11/#17 need. Not detailed
  further here — re-plan when reached.

### Parallelism Notes

Phase 1 and 2 touch disjoint files and can run concurrently. Phases 4 and 5
both live in the same new `support_ops.py` file but touch different
functions/tools, so they're listed parallel but will serialize at the
file-edit level in practice — fine for one developer, worth splitting across
two only if parallel agents are used.

---

## Decisions Log

| Decision | Choice | Alternatives | Rationale |
|----------|--------|---------------|-----------|
| Scope of this PRD | Workflow tools only, not infra | Rewrite infra PRD to include workflows | Infra PRD (`crm-ticketing-connector-hub.prd.md`) already covers OAuth/adapter contract; duplicating it here would drift |
| MVP category | Support (C) over Sales (B) or Project (D) | Start with Sales intelligence (B) instead | Freshdesk+CRM join is simpler (direct email link) than Salesloft/Outreach join (unread client shapes, unconfirmed schema) |
| Pattern for new tools | Extend `crm_ops.py` pseudo-provider pattern | New top-level adapter type | Pattern already proven, reviewed, and working end-to-end for 3 tools |

---

## Research Summary

**Market Context**
Not researched — competitor analysis skipped per auto-mode bias toward
default decisions over stalling; this is an internal build exercise, not a
market-facing launch decision. Revisit if this becomes a real product.

**Technical Context**
- `src/crm_ops.py` (178 lines): 3 existing tools (`find_duplicates`,
  `data_quality_audit`, `customer_360`), all exact-email-match, read-only,
  registered as a pseudo-provider with no OAuth.
- All 7 provider clients exist: `salesforce_client.py`, `hubspot_client.py`,
  `zoho_client.py`, `salesloft_client.py`, `outreach_client.py`,
  `asana_client.py`, `freshdesk_client.py`.
- `.claude/prds/crm-ticketing-connector-hub.prd.md` already specifies the
  adapter contract, vault, OAuth, and a 7-provider milestone plan (M0-M5)
  that this PRD's tools sit on top of without needing changes to.
- `.claude/plans/crm-ticketing-connector-hub.plan.md` exists — not yet read
  in full; check before starting Phase 1 for any conflicting in-flight work.

---

*Generated: 2026-10-17*
*Status: DRAFT - needs validation (see Open Questions)*
