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
