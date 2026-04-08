# Backlog: Purge API for Import Workflows

**Status:** Done (implemented in commit 9895515)
**Created:** 2026-03-31
**Context:** The timecockpit-mcp import script needs the ability to delete all data of a specific `source_type` (e.g. `timesheet`, `git-commit`) or all data entirely before a re-import.

## Requirement

### MCP Tool: `delete_documents`

New MCP tool in the Powerbrain server that deletes documents by filter criteria:

```
Tool: delete_documents
Parameter:
  - source_type: string (optional) — e.g. "timesheet", "git-commit", "github-issue"
  - project: string (optional) — e.g. "PROJ-A"
  - confirm: boolean (required) — safety flag, must be true
  - delete_all: boolean (optional) — if true, ignores source_type/project filters
```

### Expected Behavior

1. **Qdrant:** Delete all vectors matching the payload filter (`source_type`, `project`)
2. **PostgreSQL:** Delete associated entries in `documents_meta`
3. **Graph:** Remove associated nodes (Timesheet, Commit) and their relationships
4. **Response:** Return the number of deleted documents/vectors/nodes

### Use Cases in the Import Script

```bash
# Delete only timesheets (for a clean reimport)
npx tsx scripts/import-from-timecockpit.ts --purge

# Delete everything (timesheets + commits + issues + graph nodes)
npx tsx scripts/import-from-timecockpit.ts --purge-all
```

### Existing Infrastructure

- `ingestion/retention_cleanup.py` already has `delete_dataset()` — deletes individual datasets by ID
- The pattern can be extended to bulk deletion by `source_type`
- Qdrant filter on `source_type` is already present in the payload

## Dependency

The `--purge` / `--purge-all` flag in the timecockpit-mcp import script will be implemented once this tool is available.
