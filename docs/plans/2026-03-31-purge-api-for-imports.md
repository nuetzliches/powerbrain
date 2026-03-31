# Backlog: Purge-API für Import-Workflows

**Status:** Backlog
**Erstellt:** 2026-03-31
**Kontext:** timecockpit-mcp Import-Script benötigt die Möglichkeit, vor einem Neuimport alle Daten eines bestimmten `source_type` (z.B. `timesheet`, `git-commit`) oder alle Daten komplett zu löschen.

## Anforderung

### MCP-Tool: `delete_documents`

Neues MCP-Tool im Powerbrain-Server, das Dokumente nach Filter-Kriterien löscht:

```
Tool: delete_documents
Parameter:
  - source_type: string (optional) — z.B. "timesheet", "git-commit", "github-issue"
  - project: string (optional) — z.B. "PROJ-A"
  - confirm: boolean (required) — Sicherheits-Flag, muss true sein
  - delete_all: boolean (optional) — wenn true, ignoriert source_type/project-Filter
```

### Erwartetes Verhalten

1. **Qdrant:** Alle Vektoren mit passendem Payload-Filter (`source_type`, `project`) löschen
2. **PostgreSQL:** Zugehörige Einträge in `documents_meta` entlöschen
3. **Graph:** Zugehörige Nodes (Timesheet, Commit) und deren Relationships entfernen
4. **Response:** Anzahl gelöschter Dokumente/Vektoren/Nodes zurückgeben

### Anwendungsfälle im Import-Script

```bash
# Nur Timesheets löschen (für sauberen Reimport)
npx tsx scripts/import-from-timecockpit.ts --purge

# Alles löschen (Timesheets + Commits + Issues + Graph-Nodes)
npx tsx scripts/import-from-timecockpit.ts --purge-all
```

### Bestehende Infrastruktur

- `ingestion/retention_cleanup.py` hat bereits `delete_dataset()` — löscht einzelne Datensätze per ID
- Muster kann auf Bulk-Löschung per `source_type` erweitert werden
- Qdrant-Filter auf `source_type` ist bereits im Payload vorhanden

## Abhängigkeit

Das `--purge` / `--purge-all` Flag im timecockpit-mcp Import-Script wird implementiert, sobald dieses Tool verfügbar ist.
