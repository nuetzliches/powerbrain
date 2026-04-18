# pb-adapter-template

Starter repository for writing a new Powerbrain source adapter.

## Prerequisites

- Python 3.12+
- Docker and Docker Compose
- A running Powerbrain stack (`./scripts/quickstart.sh` in the main repo)

## Layout

```
ingestion/adapters/<your_source>/
├── adapter.py       # implements SourceAdapter
├── providers/       # one module per sub-source
├── requirements.txt
└── tests/
```

## Implementing a Source Adapter

Subclass `SourceAdapter` from `ingestion.adapters.base` and implement:

1. `list_documents()` — yield `NormalizedDocument` instances
2. `fetch_changes_since(state)` — incremental mode
3. `get_state()` / `set_state()` — persist sync state

All documents flow through the shared pipeline (PII scan → quality gate
→ OPA privacy → optional vault → chunk → embed → Qdrant + layers).
No adapter-specific storage code is needed.

## Running Tests

```bash
docker run --rm -v $(pwd):/app -w /app python:3.12-slim bash -c "
  pip install -q -r requirements.txt &&
  python -m pytest tests/ -v
"
```

## License

Apache 2.0. See LICENSE.
