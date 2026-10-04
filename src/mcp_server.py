"""MCP server exposing Freshdesk read primitives as tools for Agent Studio."""
from mcp.server.fastmcp import FastMCP

import freshdesk_client as fd

mcp = FastMCP("freshdesk-connector")


@mcp.tool()
def list_tickets(status: str | None = None, updated_since: str | None = None, page: int = 1) -> list[dict]:
    """List Freshdesk tickets, optionally filtered by status or updated_since (ISO 8601)."""
    return fd.list_tickets(status=status, updated_since=updated_since, page=page)


@mcp.tool()
def get_ticket(ticket_id: int, include: str | None = None) -> dict:
    """Get a single Freshdesk ticket by id. include: 'conversations' or 'requester'."""
    return fd.get_ticket(ticket_id, include=include)


@mcp.tool()
def search_tickets(query: str) -> list[dict]:
    """Search tickets using Freshdesk query syntax, e.g. 'status:2 AND priority:3'."""
    return fd.search_tickets(query)


if __name__ == "__main__":
    mcp.run()
