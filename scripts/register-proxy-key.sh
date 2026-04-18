#!/usr/bin/env bash
# Register the pb-proxy internal service token (secrets/mcp_auth_token.txt)
# as an API key in the `api_keys` table so pb-proxy can reach mcp-server
# with AUTH_REQUIRED=true.
#
# Called by quickstart.sh; safe to re-run — INSERT uses ON CONFLICT.

set -euo pipefail

TOKEN_FILE="${1:-secrets/mcp_auth_token.txt}"

if [ ! -f "$TOKEN_FILE" ]; then
    echo "[i] $TOKEN_FILE missing — generating a random proxy token ..." >&2
    mkdir -p "$(dirname "$TOKEN_FILE")"
    openssl rand -hex 32 | awk '{printf "pb_%s", $0}' > "$TOKEN_FILE"
    chmod 600 "$TOKEN_FILE"
fi

TOKEN="$(cat "$TOKEN_FILE")"
HASH="$(printf '%s' "$TOKEN" | sha256sum | cut -d' ' -f1)"

docker exec -i pb-postgres psql -U pb_admin -d powerbrain <<SQL >/dev/null
INSERT INTO api_keys (key_hash, agent_id, agent_role, description)
VALUES ('$HASH', 'pb-proxy', 'admin', 'pb-proxy internal service token')
ON CONFLICT (agent_id) DO UPDATE SET key_hash = EXCLUDED.key_hash, active = true;
SQL

echo "[+] pb-proxy service token registered in api_keys (agent_id=pb-proxy)"
