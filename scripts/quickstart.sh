#!/usr/bin/env bash
# ============================================================
#  Powerbrain Quick Start
#  Sets up a local Powerbrain instance from scratch.
#
#  Usage:
#    ./scripts/quickstart.sh                 # default: local-llm + local-reranker, no seed
#    ./scripts/quickstart.sh --seed          # + seed base testdata
#    ./scripts/quickstart.sh --demo          # + seed + PII fixtures + graph + demo UI
#
#  Equivalent env-var form (for CI etc.):
#    SKIP_SEED=1          # disable auto-seed that is on by default when --seed is given
#    ENABLE_DEMO=1        # same as --demo
# ============================================================
set -euo pipefail

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${GREEN}[+]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
error() { echo -e "${RED}[x]${NC} $*"; exit 1; }

# ── Flags ──────────────────────────────────────────────────
enable_seed="${ENABLE_SEED:-0}"
enable_demo="${ENABLE_DEMO:-0}"

for arg in "$@"; do
    case "$arg" in
        --seed)  enable_seed=1 ;;
        --demo)  enable_demo=1; enable_seed=1 ;;
        -h|--help)
            grep '^#' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) warn "Unknown argument: $arg (ignored)" ;;
    esac
done

# ── Prerequisites ───────────────────────────────────────────
command -v docker >/dev/null 2>&1 || error "Docker is not installed. See https://docs.docker.com/get-docker/"
docker info >/dev/null 2>&1       || error "Docker daemon is not running."

# ── .env setup ──────────────────────────────────────────────
if [ ! -f .env ]; then
    info "Creating .env from .env.example ..."
    cp .env.example .env
    info "PG_PASSWORD will be provided via secrets/pg_password.txt (Docker Secrets)."
fi

# ── Secrets directory ───────────────────────────────────────
mkdir -p secrets

if [ ! -f secrets/pg_password.txt ]; then
    info "Generating random PostgreSQL password ..."
    openssl rand -base64 24 > secrets/pg_password.txt
    chmod 600 secrets/pg_password.txt
    info "  secrets/pg_password.txt"
fi

if [ ! -f secrets/vault_hmac_secret.txt ]; then
    openssl rand -base64 32 > secrets/vault_hmac_secret.txt
    chmod 600 secrets/vault_hmac_secret.txt
    info "  secrets/vault_hmac_secret.txt"
fi

# pb-proxy uses a separate API key to reach mcp-server under AUTH_REQUIRED.
# The file needs to exist even when the proxy isn't in this profile so that
# Compose can mount the secret later without prompting for a rebuild.
if [ ! -f secrets/mcp_auth_token.txt ]; then
    openssl rand -hex 32 | awk '{printf "pb_%s", $0}' > secrets/mcp_auth_token.txt
    chmod 600 secrets/mcp_auth_token.txt
    info "  secrets/mcp_auth_token.txt"
fi

# Service-to-service token for the ingestion API (B-50). All callers
# (mcp-server, pb-proxy, pb-worker, pb-demo, pb-seed) read this same
# secret file and present it as Bearer; the ingestion middleware
# rejects everything else.
if [ ! -f secrets/ingestion_auth_token.txt ]; then
    openssl rand -hex 32 > secrets/ingestion_auth_token.txt
    chmod 600 secrets/ingestion_auth_token.txt
    info "  secrets/ingestion_auth_token.txt"
fi

# ── Start services ──────────────────────────────────────────
compose_profiles=(--profile local-llm --profile local-reranker)
if [ "$enable_seed" = "1" ]; then
    compose_profiles+=(--profile seed)
fi
if [ "$enable_demo" = "1" ]; then
    compose_profiles+=(--profile demo)
    export SEED_INCLUDE_PII=1
    export SEED_INCLUDE_GRAPH=1
fi

info "Starting Powerbrain services ..."
docker compose "${compose_profiles[@]}" up -d

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

# pb-proxy needs an api_keys row for its internal token. Register it
# now (idempotent) so the proxy's lifespan tool-refresh can reach
# mcp-server when AUTH_REQUIRED=true.
if [ "$enable_demo" = "1" ]; then
    ./scripts/register-proxy-key.sh secrets/mcp_auth_token.txt || {
        warn "Could not register pb-proxy service token. Tab D will not work."
    }
fi

# ── Pull embedding model ────────────────────────────────────
info "Pulling embedding model (nomic-embed-text) ..."
docker exec pb-ollama ollama pull nomic-embed-text

# ── Pull summarization model (for the --demo profile) ──────
# The summarization pipeline (pb.summarization policy) only fires when a
# chat model is actually reachable. For the demo profile we pull qwen2.5:3b;
# outside --demo the pull is skipped to keep `quickstart.sh` fast.
if [ "$enable_demo" = "1" ]; then
    llm_model="${LLM_MODEL:-qwen2.5:3b}"
    info "Pulling summarization model ($llm_model) — this may take a minute ..."
    docker exec pb-ollama ollama pull "$llm_model" || {
        warn "Failed to pull $llm_model. Summarization will gracefully"
        warn "fall back to raw chunks — content is still returned."
    }
fi

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
if [ "$enable_seed" = "1" ] && [ "${SKIP_SEED:-}" != "1" ]; then
    label="demo data"
    [ "$enable_demo" = "1" ] && label="demo data (incl. PII fixtures + graph)"
    info "Seeding $label ..."
    docker compose --profile seed up seed --exit-code-from seed 2>/dev/null || {
        warn "Seed container failed. Continuing without demo data."
        warn "  Inspect with: docker compose --profile seed logs seed"
    }

    info "Verifying seed — running a smoke query ..."
    smoke_response=$(curl -sf -X POST "http://localhost:8080/mcp" \
        -H "Authorization: Bearer pb_dev_localonly_do_not_use_in_production" \
        -H "Content-Type: application/json" \
        -H "Accept: application/json, text/event-stream" \
        -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"search_knowledge","arguments":{"query":"Onboarding","top_k":1}}}' \
        2>/dev/null || true)
    if echo "$smoke_response" | grep -q '"total"[[:space:]]*:[[:space:]]*[1-9]'; then
        info "  Smoke query returned results — seed looks good."
    else
        warn "  Smoke query returned 0 results. Check: docker compose --profile seed logs seed"
    fi
fi

# ── Verify ──────────────────────────────────────────────────
info "Verifying setup ..."
echo ""

check_service() {
    local name=$1 url=$2
    if curl -sf "$url" >/dev/null 2>&1; then
        echo -e "  ${GREEN}OK${NC}    $name"
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
echo "  Endpoints:"
echo "    MCP agent:   http://localhost:8080/mcp   (Bearer pb_dev_localonly_do_not_use_in_production)"
echo "    Qdrant UI:   http://localhost:6333/dashboard"
echo "    Grafana:     http://localhost:3001      (admin / admin)"
if [ "$enable_demo" = "1" ]; then
    echo -e "    ${GREEN}Demo UI:     http://localhost:8095${NC}"
    echo    "    AI proxy:    http://localhost:8090       (enterprise edition, Tab D)"
fi
echo ""
echo "  Next steps:"
echo "    - Read the Getting Started guide:   docs/getting-started.md"
echo "    - See all MCP tools:                docs/mcp-tools.md"
echo "    - Deploy with TLS:                  docs/deployment.md"
if [ "$enable_demo" = "1" ]; then
    echo "    - Sales-demo playbook:              docs/playbook-sales-demo.md"
else
    echo "    - Run a sales demo (PII + graph):   ./scripts/quickstart.sh --demo"
fi
echo ""
