#!/usr/bin/env bash
# Exercises the full OAuth 2.1 flow against a running oauth_server.py instance:
# register -> PKCE authorize -> consent (real Freshdesk creds) -> token exchange
# -> authenticated ticket fetch -> single-use code rejection -> refresh rotation.
#
# Usage:
#   uvicorn oauth_server:app --app-dir src --port 8004 &
#   FRESHDESK_DOMAIN=... FRESHDESK_API_KEY=... tests/test_oauth_flow.sh
#
# Exits non-zero on the first failed assertion.
set -euo pipefail

BASE_URL="${OAUTH_BASE_URL:-http://localhost:8004}"
FRESHDESK_DOMAIN="${FRESHDESK_DOMAIN:?set FRESHDESK_DOMAIN to a real Freshdesk domain}"
FRESHDESK_API_KEY="${FRESHDESK_API_KEY:?set FRESHDESK_API_KEY to a real Freshdesk API key}"

fail() { echo "FAIL: $1" >&2; exit 1; }
pass() { echo "PASS: $1"; }

# 1. Dynamic client registration
REG=$(curl -sf -X POST "$BASE_URL/oauth/register" \
  -H "Content-Type: application/json" \
  -d '{"client_name": "oauth-flow-test", "redirect_uris": ["http://localhost:9999/callback"]}')
CLIENT_ID=$(echo "$REG" | python3 -c "import sys,json; print(json.load(sys.stdin)['client_id'])")
[ -n "$CLIENT_ID" ] || fail "client registration returned no client_id"
pass "client registered: $CLIENT_ID"

# 2. PKCE verifier/challenge
VERIFIER=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
CHALLENGE=$(python3 -c "
import hashlib, base64
print(base64.urlsafe_b64encode(hashlib.sha256('$VERIFIER'.encode()).digest()).rstrip(b'=').decode())
")

# 3. Authorize -> consent page, extract session_id
AUTH_HTML=$(curl -sf -G "$BASE_URL/oauth/authorize" \
  --data-urlencode "client_id=$CLIENT_ID" \
  --data-urlencode "redirect_uri=http://localhost:9999/callback" \
  --data-urlencode "response_type=code" \
  --data-urlencode "code_challenge=$CHALLENGE" \
  --data-urlencode "code_challenge_method=S256" \
  --data-urlencode "state=test-state")
SESSION_ID=$(echo "$AUTH_HTML" | grep -o 'name="session_id" value="[^"]*"' | sed 's/.*value="\(.*\)"/\1/')
[ -n "$SESSION_ID" ] || fail "authorize did not return a consent session"
pass "consent page rendered, session: $SESSION_ID"

# 4. Submit consent with real Freshdesk credentials -> expect redirect with code
DECISION_HEADERS=$(curl -sf -D - -o /dev/null -X POST "$BASE_URL/oauth/authorize/decision" \
  -d "session_id=$SESSION_ID" \
  -d "decision=approve" \
  -d "freshdesk_domain=$FRESHDESK_DOMAIN" \
  --data-urlencode "freshdesk_api_key=$FRESHDESK_API_KEY")
LOCATION=$(echo "$DECISION_HEADERS" | grep -i "^location:" | tr -d '\r')
CODE=$(echo "$LOCATION" | grep -o 'code=[^&]*' | cut -d= -f2)
[ -n "$CODE" ] || fail "consent approval did not issue a code (check Freshdesk creds)"
pass "authorization code issued: $CODE"

# 5. Exchange code for tokens
TOKEN_RESP=$(curl -sf -X POST "$BASE_URL/oauth/token" \
  -d "grant_type=authorization_code" \
  -d "code=$CODE" \
  --data-urlencode "code_verifier=$VERIFIER" \
  --data-urlencode "redirect_uri=http://localhost:9999/callback" \
  -d "client_id=$CLIENT_ID")
ACCESS_TOKEN=$(echo "$TOKEN_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
REFRESH_TOKEN=$(echo "$TOKEN_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['refresh_token'])")
[ -n "$ACCESS_TOKEN" ] || fail "token exchange did not return an access_token"
pass "tokens issued"

# 6. Use the access token to fetch real tickets
TICKETS=$(curl -sf "$BASE_URL/tickets" -H "Authorization: Bearer $ACCESS_TOKEN")
echo "$TICKETS" | python3 -c "import sys,json; data=json.load(sys.stdin); assert isinstance(data, list)" \
  || fail "tickets endpoint did not return a list"
pass "fetched $(echo "$TICKETS" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))") real ticket(s) using the OAuth token"

# 7. Reusing the same code must fail (single-use)
REUSE_STATUS=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE_URL/oauth/token" \
  -d "grant_type=authorization_code" \
  -d "code=$CODE" \
  --data-urlencode "code_verifier=$VERIFIER" \
  --data-urlencode "redirect_uri=http://localhost:9999/callback" \
  -d "client_id=$CLIENT_ID")
[ "$REUSE_STATUS" = "400" ] || fail "reused authorization code should return 400, got $REUSE_STATUS"
pass "single-use code correctly rejected on reuse"

# 8. Refresh token rotation
REFRESH_RESP=$(curl -sf -X POST "$BASE_URL/oauth/token" \
  -d "grant_type=refresh_token" \
  -d "refresh_token=$REFRESH_TOKEN" \
  -d "client_id=$CLIENT_ID")
NEW_ACCESS=$(echo "$REFRESH_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
[ -n "$NEW_ACCESS" ] && [ "$NEW_ACCESS" != "$ACCESS_TOKEN" ] || fail "refresh did not rotate to a new access token"
pass "refresh token rotation works"

echo ""
echo "All OAuth flow checks passed."
