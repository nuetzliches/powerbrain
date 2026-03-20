# Search-First MVP Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make the search path reachable and verifiable in Docker so a real MCP search request succeeds through Ollama, Qdrant, OPA, and the optional reranker.

**Architecture:** Replace the local `stdio`-only MCP startup with a network-capable transport, repair packaging and Compose integration for required runtime modules and OPA policies, then add a tiny reproducible verification path for search and reranker fallback. Keep the change set narrow and avoid expanding phase 1 into full auth, ingestion, or snapshot work.

**Tech Stack:** Python 3.12, FastAPI/MCP Python SDK, Docker Compose, OPA/Rego, Qdrant, Ollama, PostgreSQL, httpx, asyncpg, Prometheus

---

### Task 1: Inspect MCP transport support and lock the target runtime shape

**Files:**
- Modify: `mcp-server/server.py`
- Check: `mcp-server/requirements.txt`
- Check: `mcp-server/Dockerfile`

**Step 1: Verify available MCP transport APIs**

Run: `python - <<'PY'
import mcp.server
print('mcp ok')
PY`

Expected: the environment can import the MCP package or clearly reports the missing package if dependencies are not installed locally yet.

**Step 2: Write the failing transport test or probe**

Create a small temporary probe or test file such as `mcp-server/tests/test_transport_startup.py` that asserts the selected network-capable transport can be imported and that `stdio_server` is no longer the only startup path.

```python
def test_server_uses_network_transport() -> None:
    from pathlib import Path

    source = Path("mcp-server/server.py").read_text()
    assert "stdio_server" not in source
```

**Step 3: Run the test to verify it fails**

Run: `pytest mcp-server/tests/test_transport_startup.py -v`

Expected: FAIL because `server.py` still uses `stdio_server`.

**Step 4: Implement the minimal transport change**

- Replace the `stdio` startup block near `mcp-server/server.py:957` with a network-capable MCP startup.
- Keep Prometheus metrics on a separate endpoint or port from the MCP endpoint.
- Do not redesign tool handlers yet.

**Step 5: Run the targeted test again**

Run: `pytest mcp-server/tests/test_transport_startup.py -v`

Expected: PASS.

**Step 6: Commit**

```bash
git add mcp-server/server.py mcp-server/tests/test_transport_startup.py
git commit -m "feat: expose MCP server over network transport"
```

### Task 2: Repair `mcp-server` image packaging

**Files:**
- Modify: `mcp-server/Dockerfile`
- Check: `mcp-server/graph_service.py`
- Test: `mcp-server/tests/test_image_files.py`

**Step 1: Write the failing test**

```python
from pathlib import Path


def test_dockerfile_copies_graph_service() -> None:
    dockerfile = Path("mcp-server/Dockerfile").read_text()
    assert "graph_service.py" in dockerfile
```

**Step 2: Run test to verify it fails**

Run: `pytest mcp-server/tests/test_image_files.py -v`

Expected: FAIL because the Dockerfile currently copies only `server.py`.

**Step 3: Write minimal implementation**

- Update `mcp-server/Dockerfile` so runtime dependencies are copied explicitly, at minimum `server.py` and `graph_service.py`.
- Keep the image simple; do not introduce a broader packaging refactor unless needed.

**Step 4: Run test to verify it passes**

Run: `pytest mcp-server/tests/test_image_files.py -v`

Expected: PASS.

**Step 5: Build image as verification**

Run: `docker compose build mcp-server`

Expected: image builds successfully without import-related packaging mistakes.

**Step 6: Commit**

```bash
git add mcp-server/Dockerfile mcp-server/tests/test_image_files.py
git commit -m "fix: include graph service in mcp image"
```

### Task 3: Load local OPA policies in Compose

**Files:**
- Modify: `docker-compose.yml`
- Check: `opa-policies/kb/access.rego`
- Test: `tests/test_compose_opa_policy_mount.py`

**Step 1: Write the failing test**

```python
from pathlib import Path


def test_opa_service_mounts_local_policies() -> None:
    compose = Path("docker-compose.yml").read_text()
    assert "./opa-policies:/policies" in compose
    assert '"/policies"' in compose
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_compose_opa_policy_mount.py -v`

Expected: FAIL because the OPA service does not mount local policy files yet.

**Step 3: Write minimal implementation**

- Add the local policies volume to the `opa` service.
- Ensure the startup command loads `/policies`.
- Keep bundle-polling comments intact unless they conflict with local policy loading.

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_compose_opa_policy_mount.py -v`

Expected: PASS.

**Step 5: Verify runtime behavior**

Run: `docker compose up -d opa && curl -f http://localhost:8181/health`

Expected: OPA is healthy and can later evaluate the mounted policies.

**Step 6: Commit**

```bash
git add docker-compose.yml tests/test_compose_opa_policy_mount.py
git commit -m "fix: load local OPA policies in compose"
```

### Task 4: Add a minimal smoke-test path for MCP search

**Files:**
- Create: `tests/test_search_first_mvp.py`
- Modify: `README.md`
- Check: `mcp-server/server.py`

**Step 1: Write the failing smoke test**

```python
def test_search_first_mvp_smoke_placeholder() -> None:
    assert False, "replace with real compose-backed smoke test"
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_search_first_mvp.py -v`

Expected: FAIL.

**Step 3: Write minimal implementation**

- Replace the placeholder with a small smoke test or scripted verification that checks:
  - required services are healthy,
  - MCP is reachable,
  - one search request returns a valid response.
- If a full pytest integration test is too heavy for the current repository, create a deterministic verification script and document it in `README.md`.

**Step 4: Run the verification**

Run: `pytest tests/test_search_first_mvp.py -v`

Expected: PASS, or if using a script instead, the documented command exits successfully.

**Step 5: Update docs**

- Add a short `README.md` section describing how to run the MVP smoke test.
- Include exact commands and expected outcomes.

**Step 6: Commit**

```bash
git add tests/test_search_first_mvp.py README.md
git commit -m "test: add search-first MVP smoke verification"
```

### Task 5: Ensure reproducible demo data for search verification

**Files:**
- Create: `docs/plans/2026-03-20-search-seed-notes.md`
- Modify: `README.md`
- Check: `init-db/`
- Check: `ingestion/`

**Step 1: Write the failing verification note**

Create a short checklist stating that a smoke test cannot be considered complete without known searchable content.

```markdown
- [ ] one known document exists in the selected Qdrant collection
- [ ] the expected query string is documented
- [ ] the expected policy classification is documented
```

**Step 2: Verify the gap exists**

Run: `grep -n "seed\|demo data\|sample data" README.md`

Expected: no reliable documented seed path yet.

**Step 3: Write minimal implementation**

- Choose the smallest viable method:
  - documented manual seed path, or
  - tiny scripted seed path, or
  - checked-in demo fixture if appropriate.
- Document the exact collection, payload shape, classification, and example query.

**Step 4: Verify the documented seed path**

Run the documented seed command(s) and then run the smoke search.

Expected: the search returns the seeded result or a clearly documented allowed empty result if the seed was intentionally skipped.

**Step 5: Commit**

```bash
git add README.md docs/plans/2026-03-20-search-seed-notes.md
git commit -m "docs: define reproducible search seed path"
```

### Task 6: Verify reranker fallback behavior

**Files:**
- Modify: `tests/test_search_first_mvp.py`
- Check: `mcp-server/server.py:142`

**Step 1: Write the failing test**

```python
def test_search_survives_reranker_failure() -> None:
    assert False, "replace with reranker fallback verification"
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_search_first_mvp.py -v -k reranker`

Expected: FAIL.

**Step 3: Write minimal implementation**

- Exercise the search path with `reranker` stopped or unreachable.
- Assert that the MCP response is still valid.
- Optionally assert that fallback logging or metrics increment if practical.

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_search_first_mvp.py -v -k reranker`

Expected: PASS.

**Step 5: Commit**

```bash
git add tests/test_search_first_mvp.py
git commit -m "test: cover reranker fallback for MVP search"
```

### Task 7: Document deferred phase-2 work

**Files:**
- Modify: `docs/bekannte-schwachstellen.md`
- Modify: `README.md`

**Step 1: Write the failing documentation check**

Create a short checklist in your notes ensuring the MVP docs explicitly defer:

```markdown
- auth hardening
- query hardening
- ingestion API completion
- snapshot flow completion
```

**Step 2: Verify the docs need the clarification**

Read `README.md` and confirm the MVP boundary is not explicit enough.

Expected: current docs describe the full target system more than the narrow MVP.

**Step 3: Write minimal implementation**

- Add a short MVP status/boundary section to `README.md`.
- If useful, add a phase-2 note to `docs/bekannte-schwachstellen.md` so deferred items stay visible.

**Step 4: Verify docs are consistent**

Read both files and confirm the MVP boundary matches the approved design.

Expected: docs clearly separate phase 1 from later hardening and feature work.

**Step 5: Commit**

```bash
git add README.md docs/bekannte-schwachstellen.md
git commit -m "docs: clarify MVP boundary and follow-up work"
```

### Task 8: Run final MVP verification

**Files:**
- Check: `docker-compose.yml`
- Check: `README.md`
- Check: `mcp-server/server.py`

**Step 1: Start the minimal stack**

Run: `docker compose up -d postgres qdrant opa ollama mcp-server`

Expected: all required services start successfully.

**Step 2: Verify service health**

Run: `docker compose ps`

Expected: required services are healthy or running without crash loops.

**Step 3: Verify OPA decisions**

Run: `docker exec kb-opa /opa eval -d /policies/kb/access.rego -i '{"agent_role":"analyst","classification":"internal","action":"read"}' 'data.kb.access.allow'`

Expected: `true`.

**Step 4: Run the smoke search**

Run the documented MVP verification command from `README.md`.

Expected: a valid MCP search response is returned.

**Step 5: Verify reranker fallback**

Run the documented fallback test with `reranker` stopped or disabled.

Expected: search still succeeds.

**Step 6: Commit verification-related updates if needed**

```bash
git add README.md docker-compose.yml mcp-server/server.py tests
git commit -m "chore: verify search-first MVP end to end"
```
