# GitHub Adapter

Syncs GitHub repository contents into the Powerbrain knowledge base as a source. All content flows through the standard ingestion pipeline (PII scan → OPA → quality gate → embedding).

## Prerequisites

### Authentication Options

The adapter supports two authentication modes:

| Mode | When to Use | Credentials |
|---|---|---|
| **PAT** (Personal Access Token) | Single user, small setups, public repos | One token for all repos |
| **GitHub App** | Organizations, fine-grained access, higher rate limits | App ID + installation ID + private key |

### PAT Setup

1. Go to [GitHub Settings → Developer settings → Personal access tokens (fine-grained)](https://github.com/settings/tokens?type=beta)
2. Generate a new token with the following permissions:
   - **Repository access:** Select the repos to sync
   - **Contents:** Read-only
   - **Metadata:** Read-only (automatic)
3. Store the token as a Docker Secret:

```bash
echo "ghp_yourtokenvalue" > secrets/github_pat.txt
```

### GitHub App Setup

Preferred for organization-wide access. Higher rate limit (15,000 req/h vs 5,000 for PAT) and avoids personal token exposure.

1. Go to your organization settings → Developer settings → GitHub Apps → **New GitHub App**
2. Configure permissions: **Contents: Read-only**, **Metadata: Read-only**
3. Generate and download a **private key** (`.pem` file)
4. Install the app on the target organization, note the **Installation ID**
5. Store the private key as a Docker Secret:

```bash
cp path/to/your-app.private-key.pem secrets/github_app_key.pem
```

## Configuration

Copy the template and adjust:

```bash
cp ingestion/repos.yaml.example ingestion/repos.yaml
```

### Example Configuration

```yaml
repos:
  # Documentation repo, PAT auth
  - name: "company-docs"
    url: "https://github.com/your-org/company-docs"
    branch: "main"
    collection: "pb_general"
    project: "company-docs"
    classification: "internal"
    auth: "pat"
    include: ["docs/**", "*.md"]
    exclude: ["drafts/**"]
    poll_interval_seconds: 300

  # Code repo into the pb_code collection
  - name: "platform-api"
    url: "https://github.com/your-org/platform-api"
    branch: "main"
    collection: "pb_code"
    project: "platform"
    classification: "internal"
    auth: "pat"
    include: ["src/**", "docs/**"]
    exclude: ["src/test/**", "*.test.*"]

  # GitHub App auth, confidential policy repo
  - name: "org-policies"
    url: "https://github.com/your-org/policies"
    branch: "main"
    collection: "pb_rules"
    project: "governance"
    classification: "confidential"
    auth: "github-app"
    app_id: 12345
    installation_id: 67890
    private_key_path: "/run/secrets/github_app_key"
```

### Classification

Classification is assigned **per repo** in YAML. All documents from a repo inherit its classification level (`public`, `internal`, `confidential`, `restricted`). OPA evaluates this on every search request.

## Quick Start

```bash
# 1. Create a PAT or GitHub App (see Prerequisites)

# 2. Store credentials as Docker Secrets
echo "ghp_..." > secrets/github_pat.txt
# OR for GitHub App:
cp your-app.private-key.pem secrets/github_app_key.pem

# 3. Create the config file
cp ingestion/repos.yaml.example ingestion/repos.yaml
# Edit repos.yaml: set name, url, branch, classification, etc.

# 4. (Re)start the ingestion + worker services
docker compose up -d ingestion worker

# 5. Trigger an initial sync
curl -X POST http://localhost:8081/sync

# 6. The worker polls automatically every N minutes
#    (default: 5, configurable via REPO_SYNC_INTERVAL_MINUTES)
docker logs pb-worker --tail 20
```

### Verify Data Is Indexed

```bash
curl -s http://localhost:8080/tools/search_knowledge \
  -H 'Content-Type: application/json' \
  -d '{"query": "your search term", "source_type": "github"}'
```

### Manual Sync for a Single Repo

```bash
curl -X POST http://localhost:8081/sync/company-docs
```

Response:

```json
{
  "repo": "company-docs",
  "status": "ok",
  "sha": "a1b2c3d...",
  "ingested": 12,
  "deleted": 3,
  "elapsed_seconds": 4.21
}
```

### Webhook Integration

External tools such as [Hookaido](https://github.com/nuetzliches/hookaido) can call `POST /sync/{repo_name}` on GitHub push events for near-real-time updates.

## How It Works

### Sync Flow

```
GitHub REST API
    |
    |  Worker job (every N minutes) OR manual POST /sync
    v
Ingestion service (port 8081)
    |
    v  GitAdapter.fetch_all_files() / fetch_changed_files()
    |
    v  NormalizedDocument (source_type="github")
    |
    v  Standard pipeline: PII scan -> OPA -> Quality gate -> Embedding
    |
    v  Qdrant + PostgreSQL + Vault + Graph
```

Agents never talk to GitHub directly. They read from the local Qdrant/PostgreSQL index.

### Incremental Sync (Commit SHA Tracking)

State is tracked per repo in the PostgreSQL table `repo_sync_state`:

| Column | Purpose |
|---|---|
| `repo_name` | Primary key (name from repos.yaml) |
| `last_commit_sha` | Position of last successful sync |
| `last_synced_at` | Timestamp of last success |
| `file_count` | Number of files currently indexed |
| `status` | `pending` / `syncing` / `ok` / `error` |
| `error_message` | Last error if status=`error` |

Flow:

- **First sync** — Fetches the full tree via GitHub Tree API, ingests all matching files.
- **Subsequent sync** — Calls GitHub Compare API (`/compare/{base}...{head}`), receives only added/modified/removed files.
- **No new commits** — `current_sha == last_commit_sha` → sync is a no-op.
- **Modified files** — Old version deleted first, then re-ingested.
- **Removed files** — Cascade-deleted from Qdrant (all 3 collections), PostgreSQL `documents_meta`, vault (via FK), and knowledge graph.

### Authentication Internals

**PAT:** Static token from `secrets/github_pat.txt`, sent as `Authorization: Bearer {token}`. No automatic rotation — rotate manually before expiry.

**GitHub App:** Adapter creates a short-lived JWT (RS256, 10 min expiry) from the app's private key, exchanges it for an **installation access token** (1 h TTL) at `/app/installations/{id}/access_tokens`. The installation token is cached in memory and auto-refreshed 5 minutes before expiry.

### Filtering

Three-stage filter applied to every file in the tree:

1. **Default skip patterns** (always applied):
   - Directories: `.git/`, `node_modules/`, `vendor/`, `__pycache__/`, `.venv/`, `dist/`, `build/`, etc.
   - Lock files: `package-lock.json`, `yarn.lock`, `poetry.lock`, `Gemfile.lock`
   - Hard-binary extensions: `.png`, `.jpg`, `.zip`, `.exe`, `.pyc`, `.db`, `.mp4`, etc.
   - Office/PDF documents (`.pdf`, `.doc`, `.docx`, `.xls`, `.xlsx`, `.ppt`, `.pptx`,
     `.msg`, `.eml`, `.rtf`) are skipped **unless** `allow_documents: true` is set
     on the repo (see below).
2. **exclude** patterns from config (if matched → reject)
3. **include** patterns from config (if set and not matched → reject)

Globs use `fnmatch` syntax — `*`, `?`, `[abc]`, and `**` for recursive matching.

### Document Ingestion (opt-in)

Some repos contain authoritative prose inside Office documents or PDFs — e.g.
company handbooks, policies, RFCs. Set `allow_documents: true` in `repos.yaml`
to fetch those files as bytes and run them through the shared
`ContentExtractor` (markitdown primary, python-docx/openpyxl/python-pptx
fallbacks). Extracted text is ingested with `source_type="github-document"`
and flows through the normal pipeline (PII scan, OPA quality gate, chunking,
embedding, L0/L1/L2 layers).

```yaml
- name: "company-handbook"
  url: "https://github.com/your-org/handbook"
  branch: "main"
  collection: "pb_general"
  project: "handbook"
  classification: "internal"
  auth: "pat"
  allow_documents: true
  include: ["handbook/**", "policies/**"]
```

Default is `false` so code repos are unaffected. Hard binaries (images,
archives, executables) remain blocked regardless of this setting.

### Rate Limiting

- **PAT:** 5,000 req/hour per user
- **GitHub App:** 15,000 req/hour per installation

On HTTP 429 or 403 with "rate limit" message, the adapter waits `min(Retry-After, 120s)` and retries up to 3 times. After that, the sync fails for that repo (`status=error`, `error_message` populated) and the orchestrator continues with the next repo.

## Source Type

Documents from this adapter are tagged with `source_type="github"`. Relevant settings:

| Property | Value |
|---|---|
| `source_type` | `github` |
| Quality gate threshold | `0.3` (configurable via OPA `pb.ingestion.quality_gate`) |
| Default collection | `pb_general` (override per repo with `collection:`) |
| Chunking | 1,000 chars with 200-char overlap (standard pipeline) |

### Metadata Stored per Document

Every chunk includes the following metadata in Qdrant and `documents_meta`:

- `repo_url` — Full repo URL
- `repo_name` — Config name
- `file_path` — Relative path in the repo
- `commit_sha` — Source commit
- `branch` — Configured branch
- `owner`, `repo` — Parsed from URL
- `source_ref` — `github:{owner}/{repo}:{path}@{sha}`
- `content_type` — Detected from extension (e.g., `python`, `markdown`)
- `language` — Detected language for code files

## Polling & Scheduling

- Worker job `worker/jobs/repo_sync.py` triggers `POST /sync` every `REPO_SYNC_INTERVAL_MINUTES` minutes (default `5`).
- The `poll_interval_seconds` field in `repos.yaml` is metadata only — the worker schedule is global, not per-repo.
- For per-repo scheduling, call `POST /sync/{repo_name}` from an external scheduler or webhook.

## Troubleshooting

### Sync Status and Errors

Check the `repo_sync_state` table to see per-repo state:

```bash
docker exec pb-postgres psql -U pb_admin -d powerbrain \
  -c "SELECT repo_name, status, last_commit_sha, last_synced_at, error_message FROM repo_sync_state;"
```

### Common Issues

| Symptom | Cause | Fix |
|---|---|---|
| `401 Unauthorized` | Invalid or expired PAT | Rotate token in `secrets/github_pat.txt`, restart ingestion |
| `403 rate limit` | PAT hit 5,000 req/h ceiling | Switch to GitHub App auth, or wait for reset |
| `404 Not Found` | Wrong `url` or missing repo access | Verify PAT scope / App installation covers the repo |
| Few/no files indexed | `include` patterns too strict | Loosen globs or remove `include:` for all-files mode |
| Binary files in logs as "skipped" | Default binary filter | Expected — binaries are not ingestible |
| `error` status for a repo | Look at `error_message` in `repo_sync_state` | See the specific error; re-trigger with `POST /sync/{repo_name}` |

### Logs

Relevant logger names:

- `pb-git-adapter` — Filtering, file fetching
- `pb-github` — API calls, rate-limit backoff, auth
- `pb-sync` — Orchestration, per-repo status, deletion counts

```bash
docker logs pb-ingestion --tail 100 | grep -E "pb-git-adapter|pb-github|pb-sync"
```

### Re-sync from Scratch

To force a full re-sync of a single repo, reset its state:

```bash
docker exec pb-postgres psql -U pb_admin -d powerbrain \
  -c "UPDATE repo_sync_state SET last_commit_sha = NULL WHERE repo_name = 'company-docs';"

curl -X POST http://localhost:8081/sync/company-docs
```

## Configuration Reference

| Field | Level | Default | Description |
|---|---|---|---|
| `name` | repo | (required) | Unique identifier, used as key in `repo_sync_state` |
| `url` | repo | (required) | Full GitHub repo URL (`https://github.com/owner/repo`) |
| `branch` | repo | `main` | Branch to sync |
| `collection` | repo | `pb_general` | Qdrant collection (`pb_general`/`pb_code`/`pb_rules`) |
| `project` | repo | `""` | Project ID for OPA filtering and metadata |
| `classification` | repo | `internal` | `public`/`internal`/`confidential`/`restricted` |
| `auth` | repo | `pat` | `pat` or `github-app` |
| `include` | repo | `[]` (all) | Glob patterns; file must match at least one |
| `exclude` | repo | `[]` | Glob patterns; files matching are skipped |
| `poll_interval_seconds` | repo | `300` | Metadata only (worker uses global schedule) |
| `app_id` | repo | none | Required if `auth: github-app` |
| `installation_id` | repo | none | Required if `auth: github-app` |
| `private_key_path` | repo | none | Path to `.pem` key (typically `/run/secrets/github_app_key`) |

## Related

- Office 365 adapter: see [office365-adapter.md](office365-adapter.md)
- General architecture: see [architecture.md](architecture.md)
- Pipeline details: see [what-is-powerbrain.md](what-is-powerbrain.md)
