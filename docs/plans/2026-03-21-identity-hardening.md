# Identity, Docs & Hardening Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Harden core features, implement context summarization, add TLS/secrets support, and rewrite documentation to reflect Powerbrain's new identity as a GDPR-native context engine.

**Architecture:** Context summarization is added as an OPA-controlled policy layer on top of the existing search pipeline. Ollama is extended from embedding-only to embedding + summarization. TLS is provided via optional Caddy reverse proxy profile. Docker Secrets replace sensitive `.env` values.

**Tech Stack:** Python 3.12 (FastAPI, httpx), OPA/Rego, Docker Compose, Caddy, Ollama

---

### Task 1: Fix MCP-Server Dockerfile

**Files:**
- Modify: `mcp-server/Dockerfile:8`

**Step 1: Update COPY to include all Python files**

Replace line 8:
```dockerfile
COPY server.py graph_service.py ./
```
with:
```dockerfile
COPY *.py ./
```

This copies `server.py`, `graph_service.py`, and `manage_keys.py`.

**Step 2: Verify Dockerfile is valid**

Run: `docker compose build mcp-server --no-cache 2>&1 | tail -5`
Expected: Build succeeds

**Step 3: Commit**

```bash
git add mcp-server/Dockerfile
git commit -m "fix: copy all Python files into mcp-server Docker image

manage_keys.py was missing from the image, making key management
impossible inside the container."
```

---

### Task 2: Fix Ingestion Dockerfile

**Files:**
- Modify: `ingestion/Dockerfile:9`

**Step 1: Add English spaCy model download**

After the existing line 9 (`RUN python -m spacy download de_core_news_md`), add:
```dockerfile
RUN python -m spacy download en_core_web_lg
```

So lines 9-10 become:
```dockerfile
RUN python -m spacy download de_core_news_md
RUN python -m spacy download en_core_web_lg
```

**Step 2: Verify Dockerfile is valid**

Run: `docker compose build ingestion --no-cache 2>&1 | tail -5`
Expected: Build succeeds (note: this will take a few minutes due to model download)

**Step 3: Commit**

```bash
git add ingestion/Dockerfile
git commit -m "fix: add missing en_core_web_lg spaCy model to ingestion image

The PII scanner configures both de_core_news_md and en_core_web_lg
but the English model was not downloaded at build time."
```

---

### Task 3: OPA Summarization Policy

**Files:**
- Create: `opa-policies/kb/summarization.rego`
- Test: `opa-policies/kb/test_summarization.rego` (OPA native test)

**Step 1: Write the OPA test file**

Create `opa-policies/kb/test_summarization.rego`:

```rego
package kb.summarization_test

import rego.v1
import data.kb.summarization

# ── summarize_allowed ────────────────────────────────────────

test_summarize_allowed_for_analyst if {
    summarization.summarize_allowed with input as {
        "agent_role": "analyst",
        "classification": "internal",
    }
}

test_summarize_allowed_for_admin if {
    summarization.summarize_allowed with input as {
        "agent_role": "admin",
        "classification": "internal",
    }
}

test_summarize_denied_for_viewer if {
    not summarization.summarize_allowed with input as {
        "agent_role": "viewer",
        "classification": "internal",
    }
}

# ── summarize_required ───────────────────────────────────────

test_summarize_required_for_confidential if {
    summarization.summarize_required with input as {
        "agent_role": "analyst",
        "classification": "confidential",
    }
}

test_summarize_not_required_for_internal if {
    not summarization.summarize_required with input as {
        "agent_role": "analyst",
        "classification": "internal",
    }
}

test_summarize_not_required_for_public if {
    not summarization.summarize_required with input as {
        "agent_role": "analyst",
        "classification": "public",
    }
}

# ── summarize_detail ─────────────────────────────────────────

test_detail_brief_for_restricted if {
    summarization.summarize_detail == "brief" with input as {
        "agent_role": "admin",
        "classification": "restricted",
    }
}

test_detail_standard_for_internal if {
    summarization.summarize_detail == "standard" with input as {
        "agent_role": "analyst",
        "classification": "internal",
    }
}

test_detail_standard_for_confidential if {
    summarization.summarize_detail == "standard" with input as {
        "agent_role": "analyst",
        "classification": "confidential",
    }
}
```

**Step 2: Run OPA test to verify it fails**

Run: `docker exec kb-opa /opa test /policies/kb/ -v 2>&1 | grep -E '(PASS|FAIL|ERROR)'`
Expected: FAIL or ERROR (summarization package does not exist yet)

**Step 3: Write the policy implementation**

Create `opa-policies/kb/summarization.rego`:

```rego
# ============================================================
#  Powerbrain – Context Summarization Policies
#  Package: kb.summarization
#
#  Controls whether search results are summarized before
#  delivery to agents. Supports three modes:
#  - allowed: agent may request summaries
#  - required: only summaries, never raw chunks (privacy)
#  - detail level: brief / standard / detailed
# ============================================================

package kb.summarization

import rego.v1

# ── Summarize Allowed ────────────────────────────────────────
# All roles except viewer may request summaries.

default summarize_allowed := false

summarize_allowed if {
    input.agent_role != "viewer"
}

# ── Summarize Required ───────────────────────────────────────
# Confidential data: only summaries, never raw chunks.
# This is a privacy enhancement — the agent gets the information
# but never the original text.

default summarize_required := false

summarize_required if {
    input.classification == "confidential"
}

# ── Detail Level ─────────────────────────────────────────────
# Controls summary granularity. Restricted data gets brief only.

default summarize_detail := "standard"

summarize_detail := "brief" if {
    input.classification == "restricted"
}
```

**Step 4: Run OPA test to verify it passes**

Run: `docker exec kb-opa /opa test /policies/kb/ -v 2>&1 | grep -E '(PASS|FAIL)'`
Expected: All tests PASS

**Step 5: Commit**

```bash
git add opa-policies/kb/summarization.rego opa-policies/kb/test_summarization.rego
git commit -m "feat: add OPA summarization policy (kb.summarization)

Policy-controlled context summarization with three modes:
- summarize_allowed: all roles except viewer
- summarize_required: confidential data only as summary
- summarize_detail: restricted data gets brief summaries only"
```

---

### Task 4: MCP Server — Summarization Function

**Files:**
- Modify: `mcp-server/server.py:46-78` (config section)
- Modify: `mcp-server/server.py:309-315` (embed_text area — add summarize_text)
- Create: `mcp-server/tests/test_summarize.py`

**Step 1: Write the failing test**

Create `mcp-server/tests/test_summarize.py`:

```python
"""Tests for summarize_text and OPA summarization policy checks."""

from unittest.mock import AsyncMock, MagicMock
import pytest
import json

import server
from server import summarize_text, check_opa_summarization_policy


@pytest.fixture(autouse=True)
def _patch_http(monkeypatch):
    mock_client = AsyncMock()
    monkeypatch.setattr(server, "http", mock_client)
    return mock_client


class TestSummarizeText:
    async def test_returns_summary(self, _patch_http, monkeypatch):
        monkeypatch.setattr(server, "SUMMARIZATION_MODEL", "qwen2.5:3b")

        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {"response": "This is a summary."}
        _patch_http.post.return_value = response

        result = await summarize_text(
            chunks=["Chunk 1 content", "Chunk 2 content"],
            query="What is the topic?",
            detail="standard",
        )
        assert result == "This is a summary."

    async def test_sends_correct_payload(self, _patch_http, monkeypatch):
        monkeypatch.setattr(server, "SUMMARIZATION_MODEL", "test-model")

        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {"response": "Summary"}
        _patch_http.post.return_value = response

        await summarize_text(
            chunks=["A", "B"],
            query="test query",
            detail="brief",
        )

        call_args = _patch_http.post.call_args
        assert "/api/generate" in call_args[0][0]
        payload = call_args[1]["json"]
        assert payload["model"] == "test-model"
        assert "brief" in payload["system"].lower() or "brief" in payload["prompt"].lower()

    async def test_graceful_fallback_on_error(self, _patch_http, monkeypatch):
        monkeypatch.setattr(server, "SUMMARIZATION_MODEL", "test-model")
        _patch_http.post.side_effect = Exception("Ollama down")

        result = await summarize_text(
            chunks=["A", "B"],
            query="test",
            detail="standard",
        )
        assert result is None

    async def test_empty_chunks_returns_none(self, _patch_http, monkeypatch):
        monkeypatch.setattr(server, "SUMMARIZATION_MODEL", "test-model")

        result = await summarize_text(
            chunks=[],
            query="test",
            detail="standard",
        )
        assert result is None


class TestCheckOpaSummarizationPolicy:
    async def test_returns_policy_result(self, _patch_http):
        allowed_resp = MagicMock()
        allowed_resp.raise_for_status = MagicMock()
        allowed_resp.json.return_value = {"result": True}

        required_resp = MagicMock()
        required_resp.raise_for_status = MagicMock()
        required_resp.json.return_value = {"result": False}

        detail_resp = MagicMock()
        detail_resp.raise_for_status = MagicMock()
        detail_resp.json.return_value = {"result": "standard"}

        _patch_http.post.side_effect = [allowed_resp, required_resp, detail_resp]

        result = await check_opa_summarization_policy(
            agent_role="analyst",
            classification="internal",
        )
        assert result["allowed"] is True
        assert result["required"] is False
        assert result["detail"] == "standard"

    async def test_defaults_on_opa_failure(self, _patch_http):
        _patch_http.post.side_effect = Exception("OPA down")

        result = await check_opa_summarization_policy(
            agent_role="analyst",
            classification="internal",
        )
        assert result["allowed"] is False
        assert result["required"] is False
        assert result["detail"] == "standard"
```

**Step 2: Run test to verify it fails**

Run: `cd mcp-server && python -m pytest tests/test_summarize.py -v 2>&1 | tail -10`
Expected: ImportError — `summarize_text` and `check_opa_summarization_policy` don't exist

**Step 3: Add config and implement functions**

Add to `mcp-server/server.py` after line 62 (`OTLP_ENDPOINT` line):
```python
SUMMARIZATION_MODEL = os.getenv("SUMMARIZATION_MODEL", "qwen2.5:3b")
SUMMARIZATION_ENABLED = os.getenv("SUMMARIZATION_ENABLED", "true").lower() == "true"
```

Add after the `embed_text` function (after line 315):
```python

async def summarize_text(
    chunks: list[str],
    query: str,
    detail: str = "standard",
) -> str | None:
    """Summarize chunks using Ollama. Returns None on failure (graceful degradation)."""
    if not chunks:
        return None

    detail_instructions = {
        "brief": "Provide a very concise summary in 1-2 sentences.",
        "standard": "Provide a clear summary covering the key points.",
        "detailed": "Provide a comprehensive summary preserving important details.",
    }

    system_prompt = (
        "You are a context summarization engine. Summarize the provided text chunks "
        "to answer the user's query. Only use information from the provided chunks. "
        "Do not add information that is not in the chunks. "
        f"{detail_instructions.get(detail, detail_instructions['standard'])}"
    )

    combined = "\n\n---\n\n".join(f"Chunk {i+1}:\n{c}" for i, c in enumerate(chunks))
    prompt = f"Query: {query}\n\nText chunks to summarize:\n\n{combined}"

    with _otel_span("summarize_text"):
        try:
            resp = await http.post(f"{OLLAMA_URL}/api/generate", json={
                "model": SUMMARIZATION_MODEL,
                "system": system_prompt,
                "prompt": prompt,
                "stream": False,
            })
            resp.raise_for_status()
            return resp.json().get("response", "").strip() or None
        except Exception as e:
            log.warning(f"Summarization fehlgeschlagen, liefere Rohchunks: {e}")
            return None


async def check_opa_summarization_policy(
    agent_role: str,
    classification: str,
) -> dict:
    """Check OPA summarization policy. Returns {allowed, required, detail}."""
    input_data = {
        "agent_role": agent_role,
        "classification": classification,
    }
    try:
        allowed_resp = await http.post(
            f"{OPA_URL}/v1/data/kb/summarization/summarize_allowed",
            json={"input": input_data},
        )
        allowed_resp.raise_for_status()
        allowed = allowed_resp.json().get("result", False)

        required_resp = await http.post(
            f"{OPA_URL}/v1/data/kb/summarization/summarize_required",
            json={"input": input_data},
        )
        required_resp.raise_for_status()
        required = required_resp.json().get("result", False)

        detail_resp = await http.post(
            f"{OPA_URL}/v1/data/kb/summarization/summarize_detail",
            json={"input": input_data},
        )
        detail_resp.raise_for_status()
        detail = detail_resp.json().get("result", "standard")
    except Exception as e:
        log.warning(f"OPA summarization policy check failed: {e}")
        allowed = False
        required = False
        detail = "standard"

    return {"allowed": allowed, "required": required, "detail": detail}
```

**Step 4: Run test to verify it passes**

Run: `cd mcp-server && python -m pytest tests/test_summarize.py -v`
Expected: All tests PASS

**Step 5: Run existing tests to check for regressions**

Run: `cd mcp-server && python -m pytest tests/ -v`
Expected: All existing tests still PASS

**Step 6: Commit**

```bash
git add mcp-server/server.py mcp-server/tests/test_summarize.py
git commit -m "feat: add summarize_text and OPA summarization policy check

Implements Ollama-based context summarization with graceful
degradation and OPA policy integration for allowed/required/detail."
```

---

### Task 5: MCP Server — Integrate Summarization into Search Tools

**Files:**
- Modify: `mcp-server/server.py:616-643` (search_knowledge tool schema)
- Modify: `mcp-server/server.py:722-735` (get_code_context tool schema)
- Modify: `mcp-server/server.py:882-978` (search_knowledge handler)
- Modify: `mcp-server/server.py:1108-1155` (get_code_context handler)

**Step 1: Update search_knowledge tool schema**

In the `list_tools` function, add new parameters to the `search_knowledge` tool's `inputSchema.properties` (after `purpose` at line 639):

```python
                    "summarize": {
                        "type": "boolean",
                        "default": False,
                        "description": "Request a summary of results instead of raw chunks",
                    },
                    "summary_detail": {
                        "type": "string",
                        "enum": ["brief", "standard", "detailed"],
                        "default": "standard",
                        "description": "Summary detail level (only used when summarize=true)",
                    },
```

**Step 2: Update get_code_context tool schema**

Add the same two parameters to `get_code_context`'s `inputSchema.properties` (after `top_k` at line 732):

```python
                    "summarize": {
                        "type": "boolean",
                        "default": False,
                        "description": "Request a summary of results instead of raw chunks",
                    },
                    "summary_detail": {
                        "type": "string",
                        "enum": ["brief", "standard", "detailed"],
                        "default": "standard",
                        "description": "Summary detail level",
                    },
```

**Step 3: Integrate summarization into search_knowledge handler**

In the `_dispatch` function, in the `search_knowledge` branch, after reranking (after `mcp_search_results_count` at line 918) and before the vault resolution block, add the summarization logic:

```python
        # ── Summarization (policy-controlled) ──
        summarize_requested = arguments.get("summarize", False)
        summary_detail = arguments.get("summary_detail", "standard")
        summary = None
        summary_policy = "not_requested"

        if SUMMARIZATION_ENABLED and (summarize_requested or True):  # Always check policy
            # Determine classification from first result (or input)
            result_classification = "internal"
            if reranked:
                result_classification = reranked[0].get("metadata", {}).get("classification", "internal")

            sum_policy = await check_opa_summarization_policy(agent_role, result_classification)

            if sum_policy["required"]:
                # Policy forces summarization regardless of request
                summary_detail = sum_policy["detail"]
                chunks = [r["content"] for r in reranked if r.get("content")]
                summary = await summarize_text(chunks, query, summary_detail)
                summary_policy = "enforced"
                if summary:
                    # Remove raw chunks — agent only gets summary
                    for item in reranked:
                        item.pop("content", None)
            elif summarize_requested and sum_policy["allowed"]:
                detail = sum_policy["detail"] if sum_policy["detail"] != "standard" else summary_detail
                chunks = [r["content"] for r in reranked if r.get("content")]
                summary = await summarize_text(chunks, query, detail)
                summary_policy = "requested"
            elif summarize_requested and not sum_policy["allowed"]:
                summary_policy = "denied"
```

Then modify the response JSON (around line 977-978) to include summary fields:

```python
        response_data = {"results": reranked, "total": len(reranked)}
        if summary is not None:
            response_data["summary"] = summary
        response_data["summary_policy"] = summary_policy

        return [TextContent(type="text",
            text=json.dumps(response_data, ensure_ascii=False, indent=2))]
```

**Step 4: Integrate summarization into get_code_context handler**

Add similar logic after reranking in the `get_code_context` handler (after line 1149):

```python
        # ── Summarization ──
        summarize_requested = arguments.get("summarize", False)
        summary_detail = arguments.get("summary_detail", "standard")
        summary = None
        summary_policy = "not_requested"

        if SUMMARIZATION_ENABLED and (summarize_requested or True):
            result_classification = "internal"
            if reranked:
                result_classification = reranked[0].get("metadata", {}).get("classification", "internal")

            sum_policy = await check_opa_summarization_policy(agent_role, result_classification)

            if sum_policy["required"]:
                summary_detail = sum_policy["detail"]
                chunks = [r["content"] for r in reranked if r.get("content")]
                summary = await summarize_text(chunks, query, summary_detail)
                summary_policy = "enforced"
                if summary:
                    for item in reranked:
                        item.pop("content", None)
            elif summarize_requested and sum_policy["allowed"]:
                detail = sum_policy["detail"] if sum_policy["detail"] != "standard" else summary_detail
                chunks = [r["content"] for r in reranked if r.get("content")]
                summary = await summarize_text(chunks, query, detail)
                summary_policy = "requested"
            elif summarize_requested and not sum_policy["allowed"]:
                summary_policy = "denied"

        response_data = {"results": reranked, "total": len(reranked)}
        if summary is not None:
            response_data["summary"] = summary
        response_data["summary_policy"] = summary_policy
```

Update the response return to use `response_data` instead of inline dict.

**Step 5: Add SUMMARIZATION_MODEL and SUMMARIZATION_ENABLED to docker-compose.yml**

In `docker-compose.yml`, add to `mcp-server.environment` (after `OTLP_ENDPOINT`):
```yaml
      SUMMARIZATION_MODEL:   ${SUMMARIZATION_MODEL:-qwen2.5:3b}
      SUMMARIZATION_ENABLED: ${SUMMARIZATION_ENABLED:-true}
```

**Step 6: Add to .env.example**

Append to `.env.example`:
```
# ── Summarization ───────────────────────────────────────────
# Model for context summarization (runs on Ollama)
SUMMARIZATION_MODEL=qwen2.5:3b
SUMMARIZATION_ENABLED=true
```

**Step 7: Run all tests**

Run: `cd mcp-server && python -m pytest tests/ -v`
Expected: All tests PASS

**Step 8: Commit**

```bash
git add mcp-server/server.py docker-compose.yml .env.example
git commit -m "feat: integrate policy-controlled summarization into search tools

search_knowledge and get_code_context now support summarize and
summary_detail parameters. OPA policy can enforce, allow, or deny
summarization per classification level. Confidential data is only
returned as summaries (raw chunks stripped)."
```

---

### Task 6: Docker Secrets Migration

**Files:**
- Modify: `docker-compose.yml`
- Create: `secrets/` directory structure
- Modify: `.env.example`

**Step 1: Add secrets section to docker-compose.yml**

Add at the top level (after `volumes:` section):
```yaml
secrets:
  pg_password:
    file: ./secrets/pg_password.txt
  vault_hmac_secret:
    file: ./secrets/vault_hmac_secret.txt
  forgejo_token:
    file: ./secrets/forgejo_token.txt
```

**Step 2: Update services to use secrets**

For `postgres`:
```yaml
    secrets:
      - pg_password
    environment:
      POSTGRES_DB: knowledgebase
      POSTGRES_USER: kb_admin
      POSTGRES_PASSWORD_FILE: /run/secrets/pg_password
```

For `mcp-server`, `ingestion`, `postgres-exporter`: add `secrets: [pg_password]` and update the `POSTGRES_URL` to read from secret at runtime. Since Docker Secrets are files, services need to read them. The simplest backward-compatible approach:

Add a note in `mcp-server/server.py` config section to support reading from file:
```python
def _read_secret(env_var: str, default: str = "") -> str:
    """Read from Docker Secret file if available, else fall back to env var."""
    file_path = os.getenv(f"{env_var}_FILE")
    if file_path and os.path.isfile(file_path):
        return open(file_path).read().strip()
    return os.getenv(env_var, default)
```

**Step 3: Create secrets directory with example files**

```bash
mkdir -p secrets
echo "changeme_in_production" > secrets/pg_password.txt
echo "change-me-in-production" > secrets/vault_hmac_secret.txt
echo "" > secrets/forgejo_token.txt
```

Add `secrets/*.txt` to `.gitignore` (but keep `secrets/` dir).

**Step 4: Update .env.example with documentation**

Add comment explaining the dual approach (env var or Docker Secret).

**Step 5: Commit**

```bash
git add docker-compose.yml mcp-server/server.py secrets/.gitkeep .gitignore .env.example
git commit -m "feat: add Docker Secrets support for sensitive configuration

Services now support reading secrets from /run/secrets/ files
with .env fallback for backward compatibility. Affected values:
pg_password, vault_hmac_secret, forgejo_token."
```

---

### Task 7: Caddy TLS Profile

**Files:**
- Create: `caddy/Caddyfile`
- Modify: `docker-compose.yml` (add Caddy service with profile)

**Step 1: Create Caddyfile**

Create `caddy/Caddyfile`:
```caddyfile
{$DOMAIN:localhost} {
    # MCP Server
    handle /mcp* {
        reverse_proxy mcp-server:8080
    }

    # Ingestion API
    handle /ingest* {
        reverse_proxy ingestion:8081
    }
    handle /scan* {
        reverse_proxy ingestion:8081
    }
    handle /snapshots* {
        reverse_proxy ingestion:8081
    }

    # Grafana
    handle /grafana* {
        reverse_proxy grafana:3000
    }

    # Health endpoint
    handle /health {
        respond "ok" 200
    }
}
```

**Step 2: Add Caddy service to docker-compose.yml**

Add after the `seed` service:
```yaml
  # ── Caddy (TLS Reverse Proxy, optional) ───────────────────
  caddy:
    profiles: ["tls"]
    image: caddy:2-alpine
    container_name: kb-caddy
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./caddy/Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
      - caddy_config:/config
    environment:
      DOMAIN: ${DOMAIN:-localhost}
    networks:
      - kb-net
    restart: unless-stopped
    depends_on:
      - mcp-server
      - ingestion
```

Add `caddy_data:` and `caddy_config:` to `volumes:` section.

**Step 3: Add DOMAIN to .env.example**

```
# ── TLS (optional, activate with: docker compose --profile tls up) ──
# DOMAIN=kb.example.com
```

**Step 4: Commit**

```bash
git add caddy/Caddyfile docker-compose.yml .env.example
git commit -m "feat: add optional Caddy TLS reverse proxy profile

Activate with: docker compose --profile tls up
Auto-HTTPS when DOMAIN is set, localhost mode otherwise."
```

---

### Task 8: README.md Rewrite

**Files:**
- Modify: `README.md`

**Step 1: Rewrite README.md**

Full rewrite with new identity, casual tone, emojis. See design document section 4a for structure. Key content:

- Tagline: "AI eats context. We decide what's on the menu."
- One-liner positioning as context engine
- The Problem section (2-3 sentences)
- The Solution with architecture diagram
- 6 core features with emojis
- Quick Start (docker compose up)
- How it works (pipeline diagram)
- 3 principles
- Links to docs
- Contributing invitation

**Step 2: Review README renders correctly**

Quick visual check of markdown structure.

**Step 3: Commit**

```bash
git add README.md
git commit -m "docs: rewrite README with context engine identity

New positioning as GDPR-native context engine with casual tone,
core features, and streamlined quick start."
```

---

### Task 9: what-is-powerbrain.md Rewrite

**Files:**
- Modify: `docs/what-is-powerbrain.md`

**Step 1: Rewrite with new identity**

English, aligned with context engine positioning. Sections:
- What is Powerbrain? (context engine, not knowledge base)
- The Problem (European data sovereignty + AI adoption)
- Core Features (the 6 identity-defining features)
- Architecture Overview (updated with summarization)
- How is this different? (comparison with RAG frameworks, vector-only solutions)
- Getting Started

**Step 2: Commit**

```bash
git add docs/what-is-powerbrain.md
git commit -m "docs: rewrite what-is-powerbrain with context engine positioning

Aligned with new identity, focused on GDPR-native context delivery
and European data sovereignty angle."
```

---

### Task 10: CLAUDE.md Update

**Files:**
- Modify: `CLAUDE.md`

**Step 1: Update project description**

Change "Self-hosted knowledge base" to context engine positioning.

**Step 2: Add summarization to MCP tools list**

Add `summarize` and `summary_detail` parameters to `search_knowledge` and `get_code_context` documentation.

**Step 3: Add Caddy to components table**

Add optional Caddy service (port 80/443, TLS profile).

**Step 4: Add summarization config to key decisions**

Add `SUMMARIZATION_MODEL` env var and summarization architecture to relevant sections.

**Step 5: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md with context engine identity and summarization

Reflects new positioning, summarization feature, Caddy TLS profile,
and Docker Secrets support."
```

---

### Task 11: Deployment Guide

**Files:**
- Create: `docs/deployment.md`

**Step 1: Write deployment guide**

Sections:
- Development Setup (no TLS, default)
- Production with Caddy (`docker compose --profile tls up`)
- Production with External Proxy (Nginx, Traefik, Caddy example configs showing upstream targets)
- Docker Secrets Setup (how to create secrets, migrate from .env)
- Environment Variables Reference

**Step 2: Commit**

```bash
git add docs/deployment.md
git commit -m "docs: add deployment guide for dev, prod, and proxy scenarios

Covers TLS with Caddy profile, bring-your-own-proxy configs,
and Docker Secrets migration from .env."
```

---

## Implementation Order Summary

| Task | Component | Risk | Time Est. |
|------|-----------|------|-----------|
| 1 | MCP-Server Dockerfile | Low | 2 min |
| 2 | Ingestion Dockerfile | Low | 2 min |
| 3 | OPA Summarization Policy | Low | 10 min |
| 4 | Summarization Functions | Medium | 15 min |
| 5 | Search Tool Integration | Medium | 20 min |
| 6 | Docker Secrets | Medium | 15 min |
| 7 | Caddy TLS Profile | Low | 10 min |
| 8 | README Rewrite | Low | 15 min |
| 9 | what-is-powerbrain Rewrite | Low | 10 min |
| 10 | CLAUDE.md Update | Low | 10 min |
| 11 | Deployment Guide | Low | 10 min |
