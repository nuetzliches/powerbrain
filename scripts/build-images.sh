#!/usr/bin/env bash
# ============================================================
#  build-images.sh -- Build & push Docker images to Forgejo Registry
#  Called by Forgejo Actions after mirror-sync from GitHub.
#
#  Images are tagged with :latest, :sha-<short-hash>, and (when HEAD
#  is on a release tag) :<version> (e.g. :0.9.1 from tag v0.9.1).
#  Registry: ${REGISTRY}/${ORG}/${REPO}/<service>
# ============================================================
set -euo pipefail

log()  { printf '\033[1;34m[build]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[  ok ]\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m[error]\033[0m %s\n' >&2 "$*"; }

# ── Config ──────────────────────────────────────────────────
REGISTRY="${REGISTRY:?Set REGISTRY env var (e.g. ghcr.io)}"
ORG="${ORG:-powerbrain}"
REPO="${REPO:-powerbrain}"
REPO_DIR="${REPO_DIR:-$(pwd)}"

cd "$REPO_DIR"

SHORT_SHA=$(git rev-parse --short HEAD)

# ── Release-Tag Detection ───────────────────────────────────
# Priority 1: RELEASE_TAG env (workflow passes github.ref_name on
#             tag pushes — works even when HEAD is detached).
# Priority 2: git describe --exact-match (works locally + when
#             checkout has HEAD on the tag's commit).
RELEASE_TAG="${RELEASE_TAG:-}"
if [ -z "$RELEASE_TAG" ]; then
  RELEASE_TAG=$(git describe --exact-match --tags HEAD 2>/dev/null || true)
fi
VERSION=""
if [ -n "$RELEASE_TAG" ]; then
  VERSION="${RELEASE_TAG#v}"
  log "Release tag detected: ${RELEASE_TAG} (version: ${VERSION})"
fi

# ── Image List ──────────────────────────────────────────────
# Each entry: "service-name:Dockerfile:build-context"
# mcp-server/ingestion/worker need repo root for shared/ access
IMAGES=(
  "mcp-server:mcp-server/Dockerfile:."
  "ingestion:ingestion/Dockerfile:."
  "reranker:reranker/Dockerfile:."
  "pb-proxy:pb-proxy/Dockerfile:."
  "worker:worker/Dockerfile:."
)

# ── Detect changes ──────────────────────────────────────────
# Force rebuild all on workflow_dispatch or if diff fails
if [ "${FORCE_BUILD:-false}" = "true" ]; then
  changed="ALL"
  log "FORCE_BUILD=true, rebuilding all images."
else
  BEFORE="${BEFORE_SHA:-}"
  if [ -z "$BEFORE" ] || [ "$BEFORE" = "0000000000000000000000000000000000000000" ]; then
    BEFORE="HEAD~1"
  fi
  changed=$(git diff --name-only "$BEFORE" HEAD 2>/dev/null || echo "ALL")
fi

# If nothing service-relevant changed, rebuild all (safety net)
rebuild_all=false
has_service_change=false
for svc in mcp-server ingestion reranker pb-proxy worker shared init-db opa-policies; do
  if echo "$changed" | grep -q "^${svc}/"; then
    has_service_change=true
    break
  fi
done

if [ "$changed" = "ALL" ] || [ "$has_service_change" = false ] || [ -n "$VERSION" ]; then
  rebuild_all=true
  log "Rebuilding all images."
fi

if echo "$changed" | grep -qE '^(shared/|init-db/|opa-policies/)'; then
  rebuild_all=true
fi

# ── Build & Push ────────────────────────────────────────────
built=0
for entry in "${IMAGES[@]}"; do
  service="${entry%%:*}"
  rest="${entry#*:}"
  dockerfile="${rest%%:*}"

  # Skip if not changed (unless rebuild_all)
  if [ "$rebuild_all" = false ]; then
    service_dir="${service}/"
    if ! echo "$changed" | grep -q "^${service_dir}"; then
      continue
    fi
  fi

  image="${REGISTRY}/${ORG}/${REPO}/${service}"
  context="${rest#*:}"
  log "Building ${image} (context: ${context})..."

  build_tags=("-t" "${image}:latest" "-t" "${image}:sha-${SHORT_SHA}")
  [ -n "$VERSION" ] && build_tags+=("-t" "${image}:${VERSION}")

  docker build -f "$dockerfile" "${build_tags[@]}" "$context"

  log "Pushing ${image}..."
  docker push "${image}:latest"
  docker push "${image}:sha-${SHORT_SHA}"
  if [ -n "$VERSION" ]; then
    docker push "${image}:${VERSION}"
    ok "${service} -> ${image}:${VERSION} (also :latest, :sha-${SHORT_SHA})"
  else
    ok "${service} -> ${image}:sha-${SHORT_SHA} (also :latest)"
  fi
  built=$((built + 1))
done

if [ "$built" -eq 0 ]; then
  log "No image changes detected."
else
  ok "Built and pushed ${built} image(s)."
fi
