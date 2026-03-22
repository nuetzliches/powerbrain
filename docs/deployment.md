# Deployment Guide

## Development Setup (Default)

No TLS, direct access to all services on their native ports.

```bash
cp .env.example .env
# Edit .env: set PG_PASSWORD

docker compose up -d

# Pull the embedding model
docker exec kb-ollama ollama pull nomic-embed-text

# Create Qdrant collections
for col in knowledge_general knowledge_code knowledge_rules; do
  curl -s -X PUT "http://localhost:6333/collections/$col" \
    -H 'Content-Type: application/json' \
    -d '{"vectors":{"size":768,"distance":"Cosine"}}' && echo " → $col ✓"
done
```

Services are available at:
- MCP Server: `http://localhost:8080/mcp`
- Ingestion API: `http://localhost:8081`
- Grafana: `http://localhost:3001`
- Prometheus: `http://localhost:9090`

## Production with Caddy (Built-in TLS)

Caddy is included as an optional Docker Compose profile. It provides automatic HTTPS with zero configuration when you set a domain name.

### Setup

1. Set your domain in `.env`:
   ```
   DOMAIN=kb.example.com
   ```

2. Start with the `tls` profile:
   ```bash
   docker compose --profile tls up -d
   ```

3. Caddy automatically obtains and renews TLS certificates via Let's Encrypt.

### What Caddy Proxies

| Path | Upstream |
|------|----------|
| `/mcp*` | mcp-server:8080 |
| `/ingest*`, `/scan*`, `/snapshots*` | ingestion:8081 |
| `/grafana*` | grafana:3000 |
| `/health` | Caddy responds directly with `200 OK` |

### Localhost Mode

If `DOMAIN` is not set (or set to `localhost`), Caddy runs with a self-signed certificate. This is useful for testing TLS without a real domain.

## Production with External Proxy

If you already have a reverse proxy (Nginx, Traefik, Caddy, HAProxy), point it at Powerbrain's internal ports. Do **not** enable the `tls` profile in this case.

### Upstream Targets

| Service | Internal Address | Purpose |
|---------|-----------------|---------|
| MCP Server | `http://localhost:8080` | Agent MCP endpoint |
| Ingestion | `http://localhost:8081` | Data ingestion API |
| Grafana | `http://localhost:3001` | Monitoring dashboards |

### Nginx Example

```nginx
upstream powerbrain_mcp {
    server 127.0.0.1:8080;
}

upstream powerbrain_ingestion {
    server 127.0.0.1:8081;
}

server {
    listen 443 ssl;
    server_name kb.example.com;

    ssl_certificate     /etc/ssl/certs/kb.example.com.pem;
    ssl_certificate_key /etc/ssl/private/kb.example.com.key;

    location /mcp {
        proxy_pass http://powerbrain_mcp;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /ingest {
        proxy_pass http://powerbrain_ingestion;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

### Traefik Example (docker-compose labels)

```yaml
services:
  mcp-server:
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.mcp.rule=Host(`kb.example.com`) && PathPrefix(`/mcp`)"
      - "traefik.http.routers.mcp.tls.certresolver=letsencrypt"
      - "traefik.http.services.mcp.loadbalancer.server.port=8080"
```

### External Caddy Example

```caddyfile
kb.example.com {
    handle /mcp* {
        reverse_proxy localhost:8080
    }
    handle /ingest* {
        reverse_proxy localhost:8081
    }
    handle /grafana* {
        reverse_proxy localhost:3001
    }
}
```

## Docker Secrets Setup

Powerbrain supports Docker Secrets for sensitive configuration values. This is the recommended approach for production deployments.

### Supported Secrets

| Secret File | Replaces Env Var | Used By |
|-------------|-----------------|---------|
| `secrets/pg_password.txt` | `PG_PASSWORD` | postgres, mcp-server, ingestion |
| `secrets/vault_hmac_secret.txt` | `VAULT_HMAC_SECRET` | mcp-server |
| `secrets/forgejo_token.txt` | `FORGEJO_TOKEN` | mcp-server, ingestion |

### How It Works

Services check for a `<ENV_VAR>_FILE` environment variable first. If the file exists, its contents are used. Otherwise, the standard env var is used as fallback.

```
FORGEJO_TOKEN_FILE=/run/secrets/forgejo_token  →  reads from file
FORGEJO_TOKEN=abc123                           →  fallback if file not found
```

This means you can migrate to Docker Secrets gradually — existing `.env` setups continue working.

### Migration from .env

1. Create secret files:
   ```bash
   # Generate a strong password
   openssl rand -base64 32 > secrets/pg_password.txt

   # Generate HMAC secret
   openssl rand -base64 32 > secrets/vault_hmac_secret.txt

   # Add Forgejo token
   echo "your-forgejo-token" > secrets/forgejo_token.txt
   ```

2. Set restrictive permissions:
   ```bash
   chmod 600 secrets/*.txt
   ```

3. Remove sensitive values from `.env` (keep only non-sensitive config like ports, model names, feature flags).

4. Restart services:
   ```bash
   docker compose down && docker compose up -d
   ```

**Important:** `secrets/*.txt` files are gitignored. Never commit secrets to the repository.

## AI Provider Proxy

The proxy is an optional service that intercepts LLM API requests and
injects Powerbrain tools transparently.

### Setup

1. **Configure LLM providers** in `pb-proxy/litellm_config.yaml`:

```yaml
model_list:
  - model_name: "gpt-4o"
    litellm_params:
      model: "openai/gpt-4o"
      api_key: "os.environ/OPENAI_API_KEY"
```

2. **Set API keys** in `.env`:

```bash
OPENAI_API_KEY=sk-...
```

3. **Start with proxy profile:**

```bash
docker compose --profile proxy up -d
```

4. **Verify:**

```bash
curl http://localhost:8090/health
```

### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `PROXY_PORT` | `8090` | Proxy port |
| `TOOL_REFRESH_INTERVAL` | `60` | Seconds between MCP tool refresh |
| `MAX_ITERATIONS` | `10` | Default max agent-loop iterations |
| `TOOL_CALL_TIMEOUT` | `30` | Timeout per tool call (seconds) |
| `REQUEST_TIMEOUT` | `120` | Total request timeout (seconds) |
| `FAIL_MODE` | `closed` | `open` or `closed` when MCP unreachable |

### Usage

**List available models** (OpenAI-compatible):

```bash
curl http://localhost:8090/v1/models
```

**Send chat requests** — Powerbrain tools are injected automatically:

```bash
curl http://localhost:8090/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4o",
    "messages": [{"role": "user", "content": "What are our data retention policies?"}]
  }'
```

Streaming is supported via `"stream": true` (SSE format).

The proxy automatically injects Powerbrain tools. If the LLM decides to
use `search_knowledge` or any other Powerbrain tool, the proxy executes
the call against the MCP server and feeds the result back to the LLM.

### Combining profiles

```bash
# Proxy + TLS
docker compose --profile proxy --profile tls up -d

# Proxy + seed data
docker compose --profile proxy --profile seed up -d
```

## Environment Variables Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `PG_PASSWORD` | `changeme_in_production` | PostgreSQL password |
| `FORGEJO_URL` | `http://forgejo.local:3000` | Forgejo server URL |
| `FORGEJO_TOKEN` | (empty) | Forgejo API token |
| `VAULT_HMAC_SECRET` | `change-me-in-production` | Vault token signing key |
| `RERANKER_MODEL` | `cross-encoder/ms-marco-MiniLM-L-6-v2` | Reranker model |
| `RERANKER_ENABLED` | `true` | Enable/disable reranker |
| `AUTH_REQUIRED` | `true` | Require API key authentication |
| `RATE_LIMIT_ENABLED` | `true` | Enable/disable rate limiting |
| `RATE_LIMIT_ANALYST` | `60` | Requests/minute for analyst role |
| `RATE_LIMIT_DEVELOPER` | `120` | Requests/minute for developer role |
| `RATE_LIMIT_ADMIN` | `300` | Requests/minute for admin role |
| `SUMMARIZATION_MODEL` | `qwen2.5:3b` | Ollama model for summarization |
| `SUMMARIZATION_ENABLED` | `true` | Enable/disable context summarization |
| `OTEL_ENABLED` | `false` | Enable OpenTelemetry tracing |
| `DOMAIN` | `localhost` | Domain for Caddy TLS (only with `--profile tls`) |

## Healthchecks

```bash
curl http://localhost:6333/healthz        # Qdrant
curl http://localhost:8181/health          # OPA
curl http://localhost:8082/health          # Reranker
curl http://localhost:11434/api/tags       # Ollama
curl http://localhost:9090/-/healthy       # Prometheus
```

With Caddy (TLS profile):
```bash
curl https://kb.example.com/health        # Caddy health endpoint
```
