#!/usr/bin/env bash
# Creates/updates GitHub labels for issue templates.
# Usage: bash .github/setup-labels.sh
# Requires: gh CLI authenticated with repo access

set -euo pipefail

LABELS=(
  "triage|fbca04|Needs review and prioritization"
  "comp:mcp-server|0075ca|MCP Server component"
  "comp:ingestion|0075ca|Ingestion pipeline component"
  "comp:reranker|0075ca|Reranker service component"
  "comp:proxy|0075ca|AI Provider Proxy (pb-proxy)"
  "comp:worker|0075ca|Worker service (pb-worker)"
  "comp:opa-policies|0075ca|OPA/Rego policies"
  "comp:database|0075ca|PostgreSQL / database schema"
  "comp:qdrant|0075ca|Qdrant vector search"
  "comp:shared|0075ca|Shared libraries"
  "comp:docker|0075ca|Docker / deployment"
  "comp:monitoring|0075ca|Prometheus / Grafana / Tempo"
  "comp:docs|0075ca|Documentation"
  "gdpr|e4e669|GDPR / privacy related"
  "eu-ai-act|e4e669|EU AI Act compliance"
)

for entry in "${LABELS[@]}"; do
  IFS='|' read -r name color description <<< "$entry"
  if gh label create "$name" --color "$color" --description "$description" 2>/dev/null; then
    echo "Created: $name"
  else
    gh label edit "$name" --color "$color" --description "$description"
    echo "Updated: $name"
  fi
done

echo "Done."
