# Contributing to Powerbrain

Thanks for your interest in contributing! Powerbrain is an open-source context engine for AI agents, and we welcome contributions of all kinds.

## Getting Started

### Prerequisites

- Python 3.12+
- Docker + Docker Compose
- Git

### Local Development Setup

```bash
git clone https://github.com/nuetzliches/powerbrain.git
cd powerbrain
cp .env.example .env

# Start all services
docker compose up -d

# Pull embedding model
docker exec pb-ollama ollama pull nomic-embed-text

# Create Qdrant collections
for col in pb_general pb_code pb_rules; do
  curl -s -X PUT "http://localhost:6333/collections/$col" \
    -H 'Content-Type: application/json' \
    -d '{"vectors":{"size":768,"distance":"Cosine"}}'
done
```

### Running Tests

```bash
# Unit tests (same as CI)
docker run --rm -v "$(pwd):/app" -w /app python:3.12-slim bash -c "
  pip install -q -r requirements-dev.txt \
    -r mcp-server/requirements.txt \
    -r ingestion/requirements.txt \
    -r pb-proxy/requirements.txt \
    fastapi uvicorn pydantic prometheus-client pyyaml python-dotenv &&
  PYTHONPATH=.:mcp-server:ingestion:reranker:pb-proxy \
  python -m pytest -m 'not integration' --tb=short -q
"

# OPA policy tests
docker exec pb-opa /opa test /policies/pb/ -v
```

## How to Contribute

### Reporting Issues

- Use [GitHub Issues](https://github.com/nuetzliches/powerbrain/issues) for bug reports and feature requests
- Include steps to reproduce for bugs
- Check existing issues before creating a new one

### Pull Requests

1. Fork the repo and create a feature branch from `master`
2. Make your changes
3. Ensure all tests pass (unit tests + OPA tests)
4. Submit a PR against `master`

All PRs require passing CI checks (unit tests, OPA policy tests, Docker build) before merge.

## Code Conventions

- **Python 3.12+** with type hints
- **Async/await** for all I/O operations
- **Pydantic models** for request/response schemas
- **OPA policies** in `opa-policies/pb/` with data-driven configuration via `data.json`
- **Environment variables** for all configuration (no hardcoded values)
- **Graceful degradation** — every service must work when optional dependencies (reranker, Ollama) are unavailable
- **`pb` prefix** for all project-specific identifiers (containers, metrics, OPA packages, collections)

## License

By contributing, you agree that your contributions will be licensed under the [Apache License 2.0](LICENSE).
