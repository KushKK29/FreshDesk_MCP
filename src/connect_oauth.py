"""Generic upstream-OAuth connect flow (PRD M1): one module, provider-
parametrized — not one file per provider, since the state/PKCE/callback
shape is identical across OAuth providers (Asana now; HubSpot/Salesforce/
Zoho/Outreach/SalesLoft reuse this unchanged in M2/M3).

Mirrors oauth_server.py's own pending-authorization pattern: a short-lived,
TTL-indexed Mongo record created at /start, consumed exactly once at
/callback via find_one_and_delete (single-use, same anti-replay reasoning).
"""
import base64
import hashlib
import logging
import os
import secrets
import time
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

logger = logging.getLogger(__name__)

try:
    import auth
    import connections
    import db as _dbmod
    from adapters import ADAPTERS, Connection
    from login_pages import SESSION_COOKIE_NAME
    from oauth_server import BASE_URL, _error_page
except ImportError:
    from src import auth
    from src import connections
    from src import db as _dbmod
    from src.adapters import ADAPTERS, Connection
    from src.login_pages import SESSION_COOKIE_NAME
    from src.oauth_server import BASE_URL, _error_page

router = APIRouter()

PENDING_CONNECT_TTL_SECONDS = 600

_db = _dbmod.db
_pending_connects = _db["pending_oauth_connects"]
_pending_connects.create_index("created_at", expireAfterSeconds=PENDING_CONNECT_TTL_SECONDS)

# One entry per OAuth provider: client_id/secret from env vars (PRD §7.3 —
# never in source), authorize/token URLs, scopes, and whether the provider
# supports PKCE (all the ones here do; some legacy OAuth2 providers don't).
PROVIDER_OAUTH_CONFIG: dict[str, dict] = {
    "asana": {
        "client_id_env": "ASANA_CLIENT_ID",
        "client_secret_env": "ASANA_CLIENT_SECRET",
        "authorize_url": "https://app.asana.com/-/oauth_authorize",
        "token_url": "https://app.asana.com/-/oauth_token",
        "scopes": ["default"],
        "pkce": True,
    },
    "hubspot": {
        "client_id_env": "HUBSPOT_CLIENT_ID",
        "client_secret_env": "HUBSPOT_CLIENT_SECRET",
        "authorize_url": "https://app.hubspot.com/oauth/authorize",
        "token_url": "https://api.hubapi.com/oauth/v1/token",
        "scopes": ["crm.objects.contacts.read", "crm.objects.contacts.write", "crm.objects.deals.read", "crm.objects.deals.write"],
        "pkce": False,  # HubSpot's OAuth app flow doesn't support PKCE — confidential client (server-side secret) is the control
    },
    "salesforce": {
        "client_id_env": "SALESFORCE_CLIENT_ID",
        "client_secret_env": "SALESFORCE_CLIENT_SECRET",
        "authorize_url": "https://login.salesforce.com/services/oauth2/authorize",
        "token_url": "https://login.salesforce.com/services/oauth2/token",
        "scopes": ["api", "refresh_token"],
        "pkce": True,  # this Connected App has "Require PKCE" forced on (not optional in its Security settings) — Salesforce rejects /authorize with "missing required code challenge" otherwise
        "instance_url_from_token_response": True,  # Salesforce returns the org's own instance_url in the token body — captured into credentials below (PRD §7.5 SSRF guard: never from user input)
    },
    "zoho": {
        "client_id_env": "ZOHO_CLIENT_ID",
        "client_secret_env": "ZOHO_CLIENT_SECRET",
        "authorize_url": "https://accounts.zoho.com/oauth/v2/auth",  # default DC; multi-DC login landing still resolves to the user's own accounts server after auth
        "token_url": "https://accounts.zoho.com/oauth/v2/token",
        "scopes": ["ZohoCRM.modules.ALL", "ZohoCRM.coql.READ"],
        "pkce": False,
        "dc_from_token_response": True,  # Zoho's token response includes "api_domain", from which the DC is derived — never from user input (SSRF guard)
        "extra_authorize_params": {"access_type": "offline"},  # without this Zoho never issues a refresh_token, so every connection silently degrades to needs_reauth once the access token expires
    },
    "outreach": {
        "client_id_env": "OUTREACH_CLIENT_ID",
        "client_secret_env": "OUTREACH_CLIENT_SECRET",
        "authorize_url": "https://api.outreach.io/oauth/authorize",
        "token_url": "https://api.outreach.io/oauth/token",
        "scopes": ["prospects.all", "sequences.all", "sequenceStates.all", "accounts.all", "calls.all", "mailboxes.all"],
        "pkce": False,
    },
    "salesloft": {
        "client_id_env": "SALESLOFT_CLIENT_ID",
        "client_secret_env": "SALESLOFT_CLIENT_SECRET",
        "authorize_url": "https://accounts.salesloft.com/oauth/authorize",
        "token_url": "https://accounts.salesloft.com/oauth/token",
        "scopes": [],
        "pkce": False,
    },
}

_ZOHO_DC_BY_API_DOMAIN = {
    "www.zohoapis.com": "us", "www.zohoapis.eu": "eu", "www.zohoapis.in": "in",
    "www.zohoapis.com.au": "au", "www.zohoapis.jp": "jp", "www.zohoapis.uk": "uk",
}


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url(provider: str, config: dict, client_id: str, user_id: str, ui: bool = False) -> str:
    """Records a pending connect and returns the provider's authorize URL.
    ui=True marks a flow started from the standalone Ops UI by bearer token:
    its callback lands on this backend with no hub cookie, so the callback
    skips the cookie check for it (the single-use state is still bound to
    the starting user_id)."""
    state = secrets.token_urlsafe(24)
    verifier, challenge = _pkce_pair() if config["pkce"] else (None, None)
    redirect_uri = f"{BASE_URL}/connect/{provider}/callback"

    _pending_connects.insert_one({
        "state": state,
        "provider": provider,
        "hub_user_id": user_id,
        "code_verifier": verifier,
        "redirect_uri": redirect_uri,
        "created_at": time.time(),
        "ui": ui,
    })

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "state": state,
        "scope": " ".join(config["scopes"]),
    }
    if config["pkce"]:
        params["code_challenge"] = challenge
        params["code_challenge_method"] = "S256"
    params.update(config.get("extra_authorize_params", {}))
    return f"{config['authorize_url']}?{urlencode(params)}"


@router.get("/connect/{provider}/start")
def connect_start(provider: str, request: Request):
    user = auth.resolve_session(request.cookies.get(SESSION_COOKIE_NAME))
    if not user:
        return RedirectResponse(f"/login?{urlencode({'next': str(request.url)})}", status_code=302)

    config = PROVIDER_OAUTH_CONFIG.get(provider)
    if not config:
        return HTMLResponse(_error_page("Unknown provider", f"'{provider}' has no OAuth connect flow configured."), status_code=404)

    client_id = os.environ.get(config["client_id_env"])
    if not client_id:
        return HTMLResponse(_error_page("Provider not configured", f"{provider} OAuth app is not registered yet ({config['client_id_env']} is unset). Register an app and set the env var first."), status_code=503)

    return RedirectResponse(authorize_url(provider, config, client_id, user.user_id), status_code=302)


@router.get("/connect/{provider}/callback")
def connect_callback(provider: str, request: Request, code: str | None = Query(None), state: str | None = Query(None), error: str | None = Query(None)):
    if provider not in PROVIDER_OAUTH_CONFIG:
        return HTMLResponse(_error_page("Unknown provider", "That provider has no OAuth connect flow configured."), status_code=404)

    if error:
        return HTMLResponse(_error_page("Connection declined", f"{provider} authorization was not granted."), status_code=400)

    pending = _pending_connects.find_one_and_delete({"state": state, "provider": provider})
    if not pending or time.time() - pending["created_at"] > PENDING_CONNECT_TTL_SECONDS:
        return HTMLResponse(_error_page("Invalid or expired connection attempt", "Start the connection again from Connected accounts."), status_code=400)

    # Account-linking CSRF guard: without this, an attacker could start their
    # own OAuth flow, capture the resulting callback URL (with a valid state
    # for THEIR provider account), then get a victim's browser to open it —
    # the victim's hub session would otherwise get the attacker's provider
    # account linked to it. The callback must be completed by the SAME hub
    # session that started it, not just hold a valid state.
    user = auth.resolve_session(request.cookies.get(SESSION_COOKIE_NAME))
    if not pending.get("ui") and (not user or user.user_id != pending["hub_user_id"]):
        return HTMLResponse(_error_page("Invalid connection attempt", "This connection must be completed in the same browser session that started it. Start again from Connected accounts."), status_code=403)

    config = PROVIDER_OAUTH_CONFIG[provider]
    client_id = os.environ.get(config["client_id_env"])
    client_secret = os.environ.get(config["client_secret_env"])

    token_data = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": pending["redirect_uri"],
        "code": code,
    }
    if pending.get("code_verifier"):
        token_data["code_verifier"] = pending["code_verifier"]

    resp = httpx.post(config["token_url"], data=token_data, timeout=15)
    if resp.status_code >= 400:
        # Full upstream body (may echo request params) goes server-side only —
        # never into the public error page.
        logger.warning("token exchange failed for %s: status=%s body=%s", provider, resp.status_code, resp.text)
        return HTMLResponse(_error_page("Connection failed", f"{provider} rejected the token exchange (status {resp.status_code})."), status_code=502)
    token_body = resp.json()

    credentials = {
        "access_token": token_body["access_token"],
        "refresh_token": token_body.get("refresh_token"),
    }
    if config.get("instance_url_from_token_response"):
        # Salesforce only: instance_url comes from its own token response,
        # never from user input (SSRF guard, PRD §7.5).
        credentials["instance_url"] = token_body["instance_url"]

    dc = None
    if config.get("dc_from_token_response"):
        # Zoho only: DC is derived from the token response's api_domain
        # against a fixed allowlist, never from user input (SSRF guard).
        api_domain = token_body.get("api_domain", "")
        host = api_domain.split("://")[-1]
        dc = _ZOHO_DC_BY_API_DOMAIN.get(host)
        if not dc:
            return HTMLResponse(_error_page("Connection failed", "Zoho returned an unrecognized data center."), status_code=502)
        credentials["dc"] = dc
    # ponytail: fall back to a conservative fixed window instead of None when
    # a provider's token response omits expires_in (Salesforce always does;
    # a safety net here for any other provider too) — without this,
    # mcp_http_server.py's proactive-refresh check (`conn.expires_at and ...`)
    # never fires because None is falsy, and the connection silently relies
    # on reactive-retry-on-401 only until that too fails and it's needs_reauth.
    expires_at = time.time() + token_body.get("expires_in", 7200)

    adapter = ADAPTERS.get(provider)
    if adapter:
        try:
            adapter.verify(Connection(
                user_id=pending["hub_user_id"], provider=provider, auth_type="oauth2",
                credentials=credentials, expires_at=expires_at,
            ))
        except Exception:
            return HTMLResponse(_error_page("Connection failed", f"Could not verify the {provider} connection."), status_code=502)

    connections.create_connection(
        user_id=pending["hub_user_id"], provider=provider, auth_type="oauth2",
        credentials=credentials, scopes=config["scopes"], expires_at=expires_at, dc=dc,
    )
    ui_origin = os.environ.get("OPS_UI_ORIGIN")
    if pending.get("ui") and ui_origin and ui_origin != "*":
        return RedirectResponse(f"{ui_origin.rstrip('/')}/#/accounts", status_code=302)
    return RedirectResponse("/connected-accounts", status_code=302)
