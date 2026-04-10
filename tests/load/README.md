# Load Tests

Load tests for the Powerbrain MCP server using [Locust](https://locust.io/).

## Prerequisites

```bash
pip install locust
```

## Running

1. Start the full stack:
   ```bash
   docker compose --profile local-llm --profile local-reranker up -d
   ```

2. Run the load test:
   ```bash
   locust -f tests/load/locustfile.py --host=http://localhost:8080
   ```

3. Open the Locust web UI at http://localhost:8089

4. Configure users and spawn rate, then start the test.

## Headless Mode

```bash
locust -f tests/load/locustfile.py --host=http://localhost:8080 \
  --headless -u 10 -r 2 --run-time 60s
```

This runs 10 concurrent users, spawning 2 per second, for 60 seconds.

## What It Tests

| Task | Weight | Description |
|------|--------|-------------|
| `search_knowledge` | 3 | Standard semantic search (most common) |
| `search_with_summarization` | 1 | Search + LLM summarization (heavier) |
| `list_datasets` | 1 | Lightweight PostgreSQL query |
| `health_check` | 1 | Baseline latency measurement |
