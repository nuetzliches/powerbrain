#!/usr/bin/env bash
# ============================================================
#  Powerbrain Quick Start
#  Sets up a local Powerbrain instance from scratch.
#  Usage: ./scripts/quickstart.sh
# ============================================================
set -euo pipefail

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${GREEN}[+]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
error() { echo -e "${RED}[x]${NC} $*"; exit 1; }

# ── Prerequisites ───────────────────────────────────────────
command -v docker >/dev/null 2>&1 || error "Docker is not installed. See https://docs.docker.com/get-docker/"
docker info >/dev/null 2>&1     || error "Docker daemon is not running."

# ── .env setup ──────────────────────────────────────────────
if [ ! -f .env ]; then
    info "Creating .env from .env.example ..."
    cp .env.example .env
    warn "Edit .env to set PG_PASSWORD before proceeding."
    warn "  Recommended: also create secrets/pg_password.txt"
    echo ""
    read -rp "Press Enter when .env is ready, or Ctrl+C to abort ..."
fi

# ── Secrets directory ───────────────────────────────────────
if [ ! -f secrets/pg_password.txt ]; then
    warn "secrets/pg_password.txt not found."
    warn "Generating a random password ..."
    mkdir -p secrets
    openssl rand -base64 24 > secrets/pg_password.txt
    info "Password written to secrets/pg_password.txt"
fi

if [ ! -f secrets/vault_hmac_secret.txt ]; then
    mkdir -p secrets
    openssl rand -base64 32 > secrets/vault_hmac_secret.txt
    info "HMAC secret written to secrets/vault_hmac_secret.txt"
fi

# ── Start services ──────────────────────────────────────────
info "Starting Powerbrain services ..."
docker compose --profile local-llm --profile local-reranker up -d

# ── Wait for healthchecks ───────────────────────────────────
info "Waiting for services to become healthy ..."

wait_for() {
    local name=$1 url=$2 max_attempts=${3:-30}
    for i in $(seq 1 "$max_attempts"); do
        if curl -sf "$url" >/dev/null 2>&1; then
            info "$name is ready."
            return 0
        fi
        sleep 2
    done
    error "$name did not become healthy after $((max_attempts * 2))s"
}

wait_for "PostgreSQL"  "http://localhost:8080/health" 60  # mcp-server depends on PG
wait_for "Qdrant"      "http://localhost:6333/healthz"
wait_for "OPA"         "http://localhost:8181/health"
wait_for "Reranker"    "http://localhost:8082/health"

# ── Pull embedding model ────────────────────────────────────
info "Pulling embedding model (nomic-embed-text) ..."
docker exec pb-ollama ollama pull nomic-embed-text

# ── Create Qdrant collections ──────────────────────────────
info "Creating Qdrant collections ..."
for col in pb_general pb_code pb_rules; do
    status=$(curl -sf -o /dev/null -w "%{http_code}" \
        "http://localhost:6333/collections/$col")
    if [ "$status" = "200" ]; then
        info "  $col already exists, skipping."
    else
        curl -sf -X PUT "http://localhost:6333/collections/$col" \
            -H 'Content-Type: application/json' \
            -d '{"vectors":{"size":768,"distance":"Cosine"}}' >/dev/null
        info "  $col created."
    fi
done

# ── Seed demo data (optional) ───────────────────────────────
if [ "${SKIP_SEED:-}" != "1" ]; then
    info "Seeding demo data (20 documents) ..."
    docker compose --profile seed up seed --exit-code-from seed 2>/dev/null || {
        warn "Seed container not available or failed. Skipping demo data."
        warn "You can seed later: docker compose --profile seed up seed"
    }
fi

# ── Verify ──────────────────────────────────────────────────
info "Verifying setup ..."
echo ""

check_service() {
    local name=$1 url=$2
    if curl -sf "$url" >/dev/null 2>&1; then
        echo -e "  ${GREEN}OK${NC}  $name"
    else
        echo -e "  ${RED}FAIL${NC}  $name ($url)"
    fi
}

check_service "MCP Server"  "http://localhost:8080/health"
check_service "Qdrant"      "http://localhost:6333/healthz"
check_service "OPA"         "http://localhost:8181/health"
check_service "Reranker"    "http://localhost:8082/health"
check_service "Ollama"      "http://localhost:11434/api/tags"

echo ""
info "Powerbrain is ready!"
echo ""
echo "  Connect your MCP agent to: http://localhost:8080/mcp"
echo ""
echo "  Next steps:"
echo "    - Read the Getting Started guide: docs/getting-started.md"
echo "    - See all MCP tools:              docs/mcp-tools.md"
echo "    - Deploy with TLS:                docs/deployment.md"
echo ""
echo "  To skip demo data seeding: SKIP_SEED=1 ./scripts/quickstart.sh"
echo ""
