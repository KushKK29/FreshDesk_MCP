# Agent Capabilities — Freshdesk Connector

## Can do
- List tickets, filtered by status or last-updated time, paginated.
- Fetch a single ticket by id, optionally including conversation thread or requester details.
- Search tickets using Freshdesk's query syntax (status, priority, tags, dates).

## Cannot do
- Create, update, or delete tickets or notes (read-only connector).
- Access contact records directly (only requester data embedded in a ticket, via `include=requester`).
- Download attachments (attachment URLs are signed and short-lived; not handled by this connector).
- Access tickets outside the Freshdesk account tied to the configured API key.
- Bypass Freshdesk's own rate limits — the connector sleeps/retries once on 429, then surfaces the error to the caller.

## Data handled
Ticket subject, description, status, priority, requester email/name (if included), conversation text. No payment data, no card numbers — this connector never touches Razorpay's own payment systems.
