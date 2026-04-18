#!/usr/bin/env bash
# Seed orchestrator — runs document seed and (optionally) graph seed.
# Controlled via env vars:
#   SEED_INCLUDE_PII=1    → include testdata/documents_pii.json
#   SEED_INCLUDE_GRAPH=1  → also run scripts/seed_graph.py
#
# Exit semantics: we do NOT abort on document-seed failure. Individual
# document ingest errors (e.g. embedding model still warming up) should
# not prevent the graph seed from running — the sales-demo UI has three
# independent tabs, so partial success beats complete failure.
# The overall exit code aggregates the worst result.

set -uo pipefail
cd /seed

doc_rc=0
graph_rc=0

seed_args=()
if [ "${SEED_INCLUDE_PII:-0}" = "1" ]; then
    seed_args+=(--include-pii)
fi

echo "== Document seed =="
python3 seed.py "${seed_args[@]}" || doc_rc=$?
if [ "$doc_rc" -ne 0 ]; then
    echo "!! Document seed exited with code $doc_rc — continuing anyway." >&2
fi

if [ "${SEED_INCLUDE_GRAPH:-0}" = "1" ]; then
    echo
    echo "== Graph seed =="
    python3 seed_graph.py || graph_rc=$?
    if [ "$graph_rc" -ne 0 ]; then
        echo "!! Graph seed exited with code $graph_rc." >&2
    fi
fi

# Propagate the worst failure; 0 if everything worked.
if [ "$doc_rc" -ne 0 ] || [ "$graph_rc" -ne 0 ]; then
    exit 1
fi
exit 0
