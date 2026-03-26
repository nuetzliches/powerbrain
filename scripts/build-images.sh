#!/usr/bin/env bash
# ============================================================
#  build-images.sh -- Build & push Docker images to Forgejo Registry
#  Called by Forgejo Actions after mirror-sync from GitHub.
#
#  Images are tagged with :latest and :sha-<short-hash>.
#  Registry: git.nuetzliche.it/nuts/powerbrain/<service>
# ============================================================
set -euo pipefail

log()  { printf '\033[1;34m[build]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[  ok ]\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m[error]\033[0m %s\n' >&2 "$*"; }

# ── Config ──────────────────────────────────────────────────
REGISTRY="${REGISTRY:-git.nuetzliche.it}"
ORG="${ORG:-nuts}"
REPO="${REPO:-powerbrain}"
REPO_DIR="${REPO_DIR:-$(pwd)}"

cd "$REPO_DIR"

SHORT_SHA=$(git rev-parse --short HEAD)

# ── Image Map ───────────────────────────────────────────────
# service-name -> Dockerfile path (relative to repo root)
# All images use repo root as build context (for shared/ access)
declare -A IMAGES=(
  [mcp-server]="mcp-server/Dockerfile"
  [ingestion]="ingestion/Dockerfile"
  [reranker]="reranker/Dockerfile"
  [pb-proxy]="pb-proxy/Dockerfile"
)

# ── Detect changes ──────────────────────────────────────────
# Force rebuild all on workflow_dispatch or if diff fails
if [ "${FORCE_BUILD:-false}" = "true" ]; then
  changed="ALL"
  log "FORCE_BUILD=true, rebuilding all images."
else
  changed=$(git diff --name-only HEAD~1 HEAD 2>/dev/null || echo "ALL")
fi

# If shared/ or init-db/ or opa-policies/ changed, rebuild all
rebuild_all=false
if echo "$changed" | grep -qE '^(shared/|init-db/|opa-policies/)' || [ "$changed" = "ALL" ]; then
  rebuild_all=true
fi

# ── Build & Push ────────────────────────────────────────────
built=0
for service in "${!IMAGES[@]}"; do
  dockerfile="${IMAGES[$service]}"

  # Skip if not changed (unless rebuild_all)
  if [ "$rebuild_all" = false ]; then
    service_dir="${service}/"
    if ! echo "$changed" | grep -q "^${service_dir}"; then
      continue
    fi
  fi

  image="${REGISTRY}/${ORG}/${REPO}/${service}"
  log "Building ${image}..."

  docker build \
    -f "$dockerfile" \
    -t "${image}:latest" \
    -t "${image}:sha-${SHORT_SHA}" \
    .

  log "Pushing ${image}..."
  docker push "${image}:latest"
  docker push "${image}:sha-${SHORT_SHA}"

  ok "${service} -> ${image}:sha-${SHORT_SHA}"
  built=$((built + 1))
done

if [ "$built" -eq 0 ]; then
  log "No image changes detected."
else
  ok "Built and pushed ${built} image(s)."
fi
