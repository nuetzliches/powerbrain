# Office 365 Adapter

Syncs SharePoint, OneDrive, Outlook Mail, Teams Messages, and OneNote into the Powerbrain knowledge base via Microsoft Graph API.

## Prerequisites

### Azure AD App Registration

1. Go to [Azure Portal](https://portal.azure.com) > Azure Active Directory > App registrations
2. Create a new registration (e.g., "Powerbrain O365 Adapter")
3. Note the **Application (client) ID** and **Directory (tenant) ID**
4. Create a client secret under Certificates & secrets

### Required API Permissions (Application)

| Permission | Type | Purpose |
|---|---|---|
| `Sites.Read.All` | Application | SharePoint sites & document libraries |
| `Files.Read.All` | Application | OneDrive / SharePoint files |
| `Mail.Read` | Application | Outlook mailboxes |
| `ChannelMessage.Read.All` | Application | Teams channel messages |
| `Team.ReadBasic.All` | Application | Enumerate teams |
| `Group.Read.All` | Application | Microsoft 365 groups |

### OneNote (Delegated Auth)

OneNote does **not** support application permissions (deprecated March 2025). You need:

1. Add delegated permissions: `Notes.Read.All`, `offline_access`
2. Create a service account in Azure AD
3. Perform an interactive OAuth2 login once to obtain a refresh token
4. Store the refresh token as a Docker Secret

```bash
# Obtain refresh token (one-time, interactive)
# Use the OAuth2 authorization code flow with your app's client_id
# Store the resulting refresh_token in:
echo "your_refresh_token" > secrets/azure_onenote_refresh_token.txt
```

The refresh token is automatically rotated by Azure AD. It expires after 90 days of inactivity.

## Configuration

Copy the example and adjust:

```bash
cp ingestion/office365.yaml.example ingestion/office365.yaml
```

### Docker Secrets

```bash
# Client secret for app registration
echo "your_client_secret" > secrets/azure_client_secret.txt

# OneNote refresh token (only if using OneNote)
echo "your_refresh_token" > secrets/azure_onenote_refresh_token.txt
```

### Example Configuration

```yaml
defaults:
  poll_interval_minutes: 15
  max_file_size_mb: 50
  collection: "pb_general"

sources:
  - name: "corporate-docs"
    tenant_id: "your-tenant-id"
    client_id: "your-app-client-id"
    project: "corporate"
    sites:
      - url: "https://corp.sharepoint.com/sites/legal"
        classification: "confidential"
        include: ["Shared Documents/**/*.docx", "Shared Documents/**/*.pdf"]
      - url: "https://corp.sharepoint.com/sites/wiki"
        classification: "internal"
    onenote:
      - notebook: "Team Wiki"
        site: "https://corp.sharepoint.com/sites/wiki"
        classification: "internal"
    mailboxes:
      - user: "support@corp.com"
        folders: ["Inbox", "Customer Requests"]
        classification: "confidential"
    teams:
      - name: "Engineering"
        channels: ["General", "Architecture"]
        classification: "internal"
    poll_interval_minutes: 30
```

### Classification

Classification is assigned **per site / mailbox / team / notebook** in YAML. All documents from a source inherit its classification level. This is the recommended approach because:

- It maps naturally to SharePoint's organizational structure
- It's auditable (admin sees what's classified where)
- It avoids expensive per-document permission queries

## Quick Start

```bash
# 1. Azure AD: Register app, grant permissions, admin-consent
#    Note your tenant_id and client_id

# 2. Store the client secret as Docker Secret
echo "your_client_secret" > secrets/azure_client_secret.txt

# 3. Create the config file
cp ingestion/office365.yaml.example ingestion/office365.yaml
# Edit office365.yaml: set tenant_id, client_id, sites, classification, etc.

# 4. Run the DB migration (adds delta_links column)
docker exec pb-postgres psql -U pb_admin -d powerbrain \
  -f /docker-entrypoint-initdb.d/019_sync_state_delta.sql

# 5. Rebuild the ingestion container (installs Office 365 dependencies)
docker compose build ingestion
docker compose up -d ingestion

# 6. Trigger a manual sync to verify
curl -X POST http://localhost:8081/sync
# Response includes both Git and Office 365 sources

# 7. The worker job syncs automatically every N minutes (default: 5)
#    Check logs:
docker logs pb-worker --tail 20
```

### Verify Data Is Indexed

```bash
# Search for content from Office 365
curl -s http://localhost:8080/tools/search_knowledge \
  -H 'Content-Type: application/json' \
  -d '{"query": "your search term", "source_type": "office365"}'
```

### Manual Sync for a Single Source

The `/sync` endpoint syncs all sources. To sync only Office 365 sources, trigger via the unified endpoint and check the response for the source name.

## How It Works

### Sync Flow

```
Microsoft Graph API
    |
    |  Worker job (every N minutes)
    v
POST /sync (ingestion service)
    |
    v  Office365Adapter.fetch_all_files() / fetch_changed_files()
    |
    v  NormalizedDocument
    |
    v  Standard pipeline: PII scan -> OPA -> Quality gate -> Embedding
    |
    v  Qdrant + PostgreSQL
```

Agents never access the Graph API directly. They read from the local Qdrant/PostgreSQL index.

### Delta Queries (Incremental Sync)

SharePoint, Outlook, and Teams support Microsoft Graph Delta Queries:
- First sync: full enumeration, receives a `deltaLink` token
- Subsequent syncs: only changed/deleted items since last `deltaLink`
- Delta links are stored in `repo_sync_state.delta_links` (JSONB)

OneNote has no delta support. It polls via `lastModifiedDateTime` comparison.

### Content Extraction

Office documents are downloaded and converted locally:
- **Primary:** Microsoft `markitdown` (DOCX, PPTX, XLSX, PDF, MSG -> Markdown)
- **Fallback:** `python-docx`, `openpyxl`, `python-pptx`
- **HTML:** BeautifulSoup (OneNote pages, email bodies)

### Teams Deduplication

File attachments in Teams messages are SharePoint references. The adapter stores attachment names in metadata but does **not** re-index the file content. Files are indexed only via the SharePoint provider.

### Rate Limiting

SharePoint uses a Resource Unit model (not simple request counts):
- Delta query: 1 RU (discounted)
- File download: 1 RU
- List query: 2 RU
- Permission query: 5 RU

The Graph Client tracks RU consumption and pre-throttles at 80% budget. All endpoints respect `Retry-After` headers.

## Source Types

Documents are tagged with source-specific types for quality gate thresholds:

| Source | `source_type` | Quality Threshold |
|---|---|---|
| SharePoint/OneDrive | `office365` | 0.5 |
| Outlook Mail | `email` | 0.5 |
| Teams Messages | `teams` | 0.4 |
| OneNote Pages | `onenote` | 0.4 |

## Troubleshooting

### OneNote Sync Stops Working

The refresh token expires after 90 days of inactivity. Monitor the worker logs for authentication errors and re-authenticate if needed.

### Large Tenant Performance

For tenants with millions of documents:
- Use `include` patterns to limit scope
- Set `max_file_size_mb` to skip large files
- Increase `poll_interval_minutes` for less critical sources
- The adapter uses `$select` on delta queries to minimize payload

### Teams Rate Limits

Teams API has stricter rate limits than SharePoint. If you see frequent 429 errors, increase the `poll_interval_minutes` for Teams sources.

### Supported File Formats

| Format | Extraction Method | Notes |
|---|---|---|
| DOCX, DOC | markitdown / python-docx | Paragraphs + tables |
| XLSX, XLS | markitdown / openpyxl | Sheet-wise, tables as Markdown |
| PPTX, PPT | markitdown / python-pptx | Slide-wise text |
| PDF | markitdown | Text extraction |
| MSG, EML | markitdown | Subject + body + metadata |
| MD, TXT, CSV, JSON, YAML | Direct UTF-8 decode | No conversion needed |
| Python, JS, TS, Go, etc. | Direct UTF-8 decode | Code files |
| HTML | BeautifulSoup / regex | Script/style stripped |
| PNG, ZIP, EXE, etc. | Skipped | Binary files ignored |

### Configuration Reference

| Field | Level | Default | Description |
|---|---|---|---|
| `name` | source | (required) | Unique source identifier |
| `tenant_id` | source | (required) | Azure AD tenant ID |
| `client_id` | source | (required) | App registration client ID |
| `project` | source | source name | Project ID for OPA filtering |
| `collection` | source | `pb_general` | Qdrant collection |
| `poll_interval_minutes` | source/default | `15` | Sync frequency |
| `max_file_size_mb` | source/default | `50` | Skip files larger than this |
| `ru_budget_per_minute` | source | `1250` | SharePoint Resource Unit budget |
| `sites[].url` | site | (required) | SharePoint site URL |
| `sites[].classification` | site | `internal` | Data classification level |
| `sites[].include` | site | all files | Glob patterns (supports `**`) |
| `sites[].exclude` | site | none | Glob patterns to skip |
| `mailboxes[].user` | mailbox | (required) | UPN or email address |
| `mailboxes[].folders` | mailbox | (required) | Mail folder names |
| `mailboxes[].classification` | mailbox | `internal` | Data classification level |
| `mailboxes[].max_age_months` | mailbox | `12` | Only sync recent mail |
| `teams[].name` | team | (required) | Team display name |
| `teams[].channels` | team | (required) | Channel names or `["*"]` |
| `teams[].classification` | team | `internal` | Data classification level |
| `onenote[].notebook` | notebook | (required) | Notebook display name |
| `onenote[].site` | notebook | none | SharePoint site (for site-scoped notebooks) |
| `onenote[].classification` | notebook | `internal` | Data classification level |

## Related

- GitHub adapter: see [github-adapter.md](github-adapter.md)
- General architecture: see [architecture.md](architecture.md)
- Pipeline details: see [what-is-powerbrain.md](what-is-powerbrain.md)
