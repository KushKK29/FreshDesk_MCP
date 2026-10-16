"""Provider adapter contract (PRD §6.2) + a flat registry, one entry per
provider module. Every provider (Freshdesk, Asana, HubSpot, Salesforce, Zoho,
Outreach, SalesLoft) implements `ProviderAdapter` and registers itself into
`ADAPTERS` on import — adding a provider means writing one adapter module and
importing it once (see `register_all_adapters()` below), no change to the MCP
layer or the OAuth server (PRD Goal 3).
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

# Single source of truth for the MCP server's top-level `instructions` field
# (FastMCP(instructions=...) in mcp_http_server.py) — also rendered on
# provider_detail.py so a hub user can see, in the UI, the same context an
# agent gets over MCP, without importing mcp_http_server.py's heavier SDK deps.
MCP_INSTRUCTIONS = (
    "Connector hub for CRM and ticketing tools. Tools are namespaced by provider "
    "(e.g. freshdesk_list_tickets, asana_list_tasks) and only appear for providers you've connected "
    "from the Connected accounts page. Destructive, send, or bulk actions return a preview and a "
    "confirm_token first; call the same tool again with that confirm_token to execute."
)


class AuthMode(Enum):
    OAUTH2 = "oauth2"
    API_KEY = "api_key"


@dataclass
class Connection:
    """One connected account: hub user + provider + decrypted credentials.
    `credentials` is already-decrypted (by connections.get_connection) by the
    time an adapter sees it — adapters never touch credentials_enc directly."""
    user_id: str
    provider: str
    auth_type: str
    credentials: dict  # tokens / api_key / instance_url / dc, provider-specific shape
    scopes: list[str] = field(default_factory=list)
    expires_at: float | None = None


@dataclass
class ToolSpec:
    name: str  # namespaced, e.g. "freshdesk_list_tickets" — stops cross-provider name collisions
    description: str
    input_schema: dict  # JSON Schema for the tool's arguments
    read_only_hint: bool = True
    destructive_hint: bool = False
    idempotent_hint: bool = True
    open_world_hint: bool = True
    requires_confirm: bool = False  # True for delete/send/bulk actions — preview-then-confirm gate (Task 16)


class ProviderAdapter(Protocol):
    key: str
    display_name: str
    auth_modes: set[AuthMode]
    scopes: list[str]

    def tools(self) -> list[ToolSpec]: ...

    def call(self, tool: str, args: dict, conn: Connection) -> dict: ...

    def refresh(self, conn: Connection) -> Connection: ...

    def verify(self, conn: Connection) -> None:
        """Raises on bad/expired credentials. No return value on success."""
        ...


ADAPTERS: dict[str, ProviderAdapter] = {}


def register(adapter: ProviderAdapter) -> ProviderAdapter:
    ADAPTERS[adapter.key] = adapter
    return adapter


_PROVIDER_MODULES = ["freshdesk", "asana", "hubspot", "salesforce", "zoho", "outreach", "salesloft"]


def register_all_adapters() -> None:
    """Imports every adapter module so each registers itself into ADAPTERS.
    Call once at process startup (mcp_http_server.py). New provider = add one
    name to _PROVIDER_MODULES, nothing else changes."""
    import importlib
    for name in _PROVIDER_MODULES:
        try:
            importlib.import_module(f".{name}", __name__)
        except ImportError:
            importlib.import_module(f"src.adapters.{name}")
