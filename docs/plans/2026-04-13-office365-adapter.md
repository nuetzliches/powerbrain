# Office 365 Adapter — Aufwandschätzung & Architekturplan (v2, nach Ökosystem-Review)

## Context

Powerbrain hat aktuell einen GitHub-Adapter als einzigen Source-Adapter. Ein Office 365 Adapter würde SharePoint, OneDrive, Word, Excel, PowerPoint, OneNote, Outlook-Mails und Teams-Messages als Datenquellen erschließen.

**v2-Änderungen:** Plan nach Review gegen Open-Source-Ökosystem (Onyx/Danswer, LlamaIndex, LangChain, Unstructured.io, Haystack, Ragflow, Elastic) und Microsoft Graph API Best Practices aktualisiert.

---

## 1. Architektur-Überblick

```
Microsoft Graph API (OAuth2 Client Credentials + Delegated für OneNote)
         │
         ▼
┌──────────────────────────────────┐
│  Office365Adapter                │  ← Separates Package: ingestion/adapters/office365/
│  (implements SourceAdapter)      │
│  ├─ SharePointProvider           │  ← Sites, Document Libraries (Delta Query)
│  ├─ OneDriveProvider             │  ← User/Shared Drives (Delta Query)
│  ├─ OneNoteProvider              │  ← Notebooks, Pages (Delegated Auth, kein Delta!)
│  ├─ OutlookProvider              │  ← Mailboxes, Messages + Attachments (Delta Query)
│  ├─ TeamsProvider                │  ← Channels, Messages, Replies (Delta Query)
│  ├─ ContentExtractor             │  ← markitdown (Microsoft) oder python-docx/pptx/openpyxl
│  └─ GraphClient                  │  ← Auth, Rate-Limit, $batch, Retry-After
└──────────────────────────────────┘
         │
         ▼  NormalizedDocument (wie bei GitHub)
┌──────────────────────────────────┐
│  Bestehende Pipeline             │  ← Unverändert
│  PII → OPA → Quality → Embed    │
└──────────────────────────────────┘
```

### Package-Struktur (separates Package, selbes Repo)

```
ingestion/adapters/office365/          ← Separates Package
├── __init__.py
├── adapter.py                         ← Office365Adapter(SourceAdapter)
├── graph_client.py                    ← Auth, $batch, RU-Tracking
├── content.py                         ← markitdown Wrapper
├── requirements.txt                   ← markitdown, msgraph-sdk, msal, beautifulsoup4
├── providers/
│   ├── sharepoint.py                  ← SharePoint/OneDrive (Delta Query)
│   ├── outlook.py                     ← Mail (Delta Query)
│   ├── teams.py                       ← Teams Messages (Delta Query + Dedup)
│   └── onenote.py                     ← OneNote (Delegated Auth, kein Delta)
└── tests/
    ├── test_adapter.py
    ├── test_graph_client.py
    ├── test_content.py
    └── test_providers.py
```

Einziger Import aus Powerbrain-Core: `SourceAdapter` und `NormalizedDocument` aus `ingestion.adapters.base`.

---

## 2. Erkenntnisse aus dem Ökosystem-Review

### Was andere Projekte tun (und was nicht)

| Thema | Ökosystem-Status | Implikation für Powerbrain |
|-------|-----------------|---------------------------|
| **Content Extraction** | Alle parsen lokal (python-docx etc.), keiner nutzt Graph `?format=pdf`. Haystack nutzt Microsofts `markitdown`. Unstructured.io ist am umfassendsten. | `markitdown` als primäre Lib (Office→Markdown), Fallback auf python-docx/pptx/openpyxl |
| **Permission Mapping** | Nur Onyx/Danswer hat echte Permission-Sync (per-Document ACLs via Group Expansion). Alle anderen ignorieren Permissions. | Site-Level Mapping bestätigt als pragmatischer Ansatz. Per-Doc ACLs wäre Phase 2. |
| **Incremental Sync** | Elastic nutzt Delta Queries. Onyx pollt via `lastModifiedDateTime`. LlamaIndex/LangChain haben kein Incremental. | Delta Queries (wie Elastic) für SharePoint/Outlook/Teams. `lastModifiedDateTime` nur für OneNote. |
| **OneNote** | Nur LangChain hat einen Loader (HTML via BeautifulSoup). **App-only Auth seit März 2025 abgeschafft!** | Delegated Auth mit Service Account nötig. Erhöht Komplexität erheblich. |
| **Teams** | Nur Onyx hat einen Connector (basic, bekannter Pagination-Bug). Keiner löst SharePoint-Deduplizierung. | Wir müssen Deduplizierung selbst bauen (source_ref Matching). |
| **Rate Limiting** | Kein Projekt nutzt `$batch` API oder Resource-Unit-Tracking. Alle nur einfaches Retry-After. | `$batch` API nutzen (20 Requests/Call), Resource-Unit-Budget tracken. |
| **Email PII** | Kein Projekt hat spezielle Email-PII-Behandlung. Unstructured extrahiert Metadata strukturiert. | Presidio + Metadata-Extraktion (sender, recipients) → mandatory Pseudonymisierung. |

### Kritische Erkenntnisse

**1. OneNote: Delegated Auth Only (Blocker für Background-Sync)**
- Microsoft hat App-only Permissions für OneNote API seit **31. März 2025** abgeschafft
- Background-Sync braucht: Service Account → OAuth2 Authorization Code Flow → Refresh Token speichern → automatisch erneuern
- Refresh Tokens laufen nach 90 Tagen Inaktivität ab → muss überwacht werden
- **Alternative:** OneNote-Support als optionales Feature (Phase 3) behandeln

**2. Microsoft `markitdown` statt eigener Extraction**
- Offizielle Microsoft-Bibliothek, konvertiert DOCX/PPTX/XLSX/PDF/MSG → Markdown
- Von Haystack bereits produktiv integriert (`MarkItDownConverter`)
- Vorteil: Eine Dependency statt 4 (python-docx + python-pptx + openpyxl + pymupdf)
- Nachteil: Weniger Kontrolle über Extraction-Details als Unstructured.io

**3. SharePoint Resource-Unit-Modell**
- SharePoint nutzt KEIN einfaches Request-Count-Limit, sondern **Resource Units**
- Delta mit Token: 1 RU, Permissions-Abfrage: 5 RU, List-Query: 2 RU
- Budget: 1.250–6.250 RU/Minute je nach Tenant-Größe
- `$batch` API: bis zu 20 Requests pro HTTP-Call (jeder einzeln gezählt für RU)

**4. Teams-SharePoint-Deduplizierung**
- Teams-Datei-Attachments sind SharePoint-Referenzen (Channel "Files" Tab = SharePoint Doc Library)
- Kein Projekt im Ökosystem löst das
- Lösung: Bei Teams-Attachments prüfen ob `source_ref` bereits via SharePoint-Sync existiert → skip

---

## 3. Überarbeitete Komponenten & Aufwand

### A. Graph Client + Auth (~3-4 Tage) ↑ erhöht
- OAuth2 Client Credentials Flow (App-Permissions)
- **Separater** OAuth2 Authorization Code Flow für OneNote (Delegated, Service Account)
- Token-Caching + automatischer Refresh (inkl. Refresh-Token-Monitoring)
- Rate-Limit-Tracking mit Resource-Unit-Budget (nicht nur 429-Retry)
- `$batch` Request-Batching (bis 20 Calls/Batch)
- Docker Secret: `azure_client_secret.txt`, `azure_onenote_refresh_token.txt`
- User-Agent Header: `ISV|Powerbrain|Office365Adapter/1.0` (bekommt Priorität bei Microsoft)

### B. SharePoint/OneDrive Provider (~3-4 Tage) — unverändert
- Delta Queries für Incremental Sync (`/drive/root/delta`)
- `deltaLink` statt SHA in `repo_sync_state`
- `cTag`-Property nutzen um echte Content-Änderungen von Metadata-Änderungen zu unterscheiden
- `$select` auf Delta um nur nötige Properties zu laden
- Rekursive Ordner-Traversierung mit Include/Exclude-Patterns

### C. OneNote Provider (~3-4 Tage) — Phase 3 empfohlen ↓
- **Kein Delta, kein Webhook, kein App-only Auth**
- Delegated Auth mit Service Account + Refresh Token Storage
- Polling via `lastModifiedDateTime` (alle Pages enumerieren, nur geänderte fetchen)
- HTML-Content → Markdown (via BeautifulSoup, wie LangChain)
- **Empfehlung: In Phase 3 verschieben** wegen Auth-Komplexität

### D. Content Extraction (~2 Tage) ↓ reduziert
- **Primär: Microsoft `markitdown`** (DOCX, PPTX, XLSX, PDF, MSG → Markdown)
- Fallback: `python-docx`, `openpyxl`, `python-pptx` wenn markitdown scheitert
- Email-spezifisch: Metadata-Extraktion (sender, recipients, CC/BCC, subject) als separate Felder
- Max-Dateigröße-Check vor Download (konfigurierbar, Default 50MB)

### E. Outlook Mail Provider (~3-4 Tage) — unverändert
- Delta Query: `GET /users/{id}/mailFolders/{folder-id}/messages/delta`
- Email-Metadata → `NormalizedDocument.metadata` (sender, recipients → PII-Scan)
- Attachments → ContentExtractor (DOCX/PDF/XLSX), Bilder/ZIP skippen
- Zeitfenster-Filter: nur Mails der letzten N Monate (konfigurierbar)

### F. Teams Channel Provider (~4-5 Tage) ↑ erhöht (Deduplizierung)
- Delta Query: `GET /teams/{id}/channels/{id}/messages/delta`
- `$expand=replies` für Thread-Kontext (ein Dokument pro Thread)
- **Deduplizierung:** Attachments gegen SharePoint-Index prüfen (`source_ref`-Matching)
- Nur Text-Content von Messages indexieren, File-Attachments verweisen auf SharePoint
- Reactions/Edits: `lastEditedDateTime` tracken, gelöschte Messages entfernen

### G. Adapter-Integration (~2 Tage) — unverändert

### H. OPA Policy & Quality Gate (~1 Tag) — unverändert
- `data.json`: `min_quality_score.office365: 0.5`, `min_quality_score.teams: 0.4`, `min_quality_score.email: 0.5`

### I. Tests (~3-4 Tage) ↑ erhöht
- Unit-Tests mit gemockter Graph API
- Deduplizierungs-Tests (Teams↔SharePoint)
- Rate-Limit-Budget-Tests
- `markitdown` Extraction-Tests pro Dokumenttyp

### J. Dokumentation (~1 Tag) — unverändert

**Gesamt ohne OneNote: ~22-28 Arbeitstage** (1 Entwickler, 5-6 Wochen)
**Gesamt mit OneNote (Phase 3): +4-5 Tage** (Delegated Auth + Polling)

---

## 4. Freigabe / Klassifizierung

### Ansatz: Site-Level Mapping (bestätigt durch Ökosystem-Review)

Alle reviewten Projekte außer Onyx ignorieren Permissions komplett. Onyx macht per-Document ACLs via Group Expansion — das ist die aufwändigste aber genaueste Variante.

**Unser Ansatz (Site-Level) ist der pragmatische Mittelweg:**

```yaml
sources:
  - name: "corporate-docs"
    tenant_id: "abc-123"
    client_id: "def-456"
    project: "corporate"
    sites:
      - url: "https://corp.sharepoint.com/sites/legal"
        classification: "confidential"
      - url: "https://corp.sharepoint.com/sites/wiki"
        classification: "internal"
    mailboxes:
      - user: "support@corp.com"
        folders: ["Inbox", "Customer Requests"]
        classification: "confidential"
    teams:
      - name: "Engineering"
        channels: ["General", "Architecture"]
        classification: "internal"
      - name: "Legal"
        channels: ["*"]
        classification: "confidential"
```

**Warum nicht per-Document ACLs (Onyx-Ansatz)?**
- Braucht `GroupMember.Read.All` Permission (sensitiv)
- 5 Resource Units pro Permission-Abfrage (teuer bei großen Tenants)
- Permissions ändern sich häufig → eigener Sync-Job nötig
- Powerbrain hat 4 Stufen, SharePoint hat N Gruppen → Mapping ist lossy
- **Kann als Phase 2 nachgerüstet werden** (Permissions in Metadata speichern, OPA-Regel erweitern)

---

## 5. Datenfluss: Cache, kein Live-Zugriff

Powerbrain ist ein **Index/Cache**, kein Proxy:

```
Office 365 (Graph API)
    │
    │  Sync-Job (alle N Minuten, Delta Queries)
    ▼
Ingestion Pipeline (PII → OPA → Quality → Embedding)
    ▼
Qdrant + PostgreSQL  ← Agenten lesen NUR von hier
```

- Graph API wird nur vom Sync-Job kontaktiert
- Fällt Office 365 aus → Agenten arbeiten mit letztem Stand weiter
- Delta Queries sind sehr effizient (nur 1 Resource Unit, nur Änderungen)

### Sync-State-Erweiterung

`repo_sync_state` braucht ein neues Feld für Delta-Links:

```sql
ALTER TABLE repo_sync_state ADD COLUMN delta_link TEXT;
-- deltaLink statt last_commit_sha für Office 365 Quellen
-- last_commit_sha bleibt für Git-Adapter
```

### Konfigurierbarkeit

| Setting | Pro Quelle | Pro Site/Mailbox/Team | Global Default |
|---------|-----------|----------------------|----------------|
| Klassifizierung | — | ✅ | — |
| Sync-Intervall | ✅ | — | 15min |
| Include/Exclude | — | ✅ | — |
| Collection (Qdrant) | ✅ | — | `pb_general` |
| Projekt | ✅ | — | — |
| Max. Dateigröße | ✅ (override) | — | 50MB |
| Mail-Zeitfenster | ✅ | — | 12 Monate |
| Auth (Tenant/App) | ✅ | — | — |

---

## 6. SPOF-Analyse

| Komponente | SPOF? | Bei Ausfall | Mitigation |
|-----------|-------|------------|-----------|
| Graph API | Nein | Sync pausiert, Agenten lesen Cache | Retry + Backoff, Daten bleiben |
| Azure AD | Nein | Kein Token → Sync pausiert | Token-Caching (1h), Alert |
| OneNote Refresh Token | Ja (nur OneNote) | OneNote-Sync stoppt nach 90 Tagen | Monitoring + Alert, Admin-Eingriff |
| Ingestion Service | Ja | Kein Sync | Healthcheck + Restart |
| markitdown | Nein | Einzelne Formate scheitern | Fallback auf python-docx etc. |

---

## 7. Risiken & Mitigations (aktualisiert)

| Risiko | Schwere | Mitigation |
|--------|---------|-----------|
| OneNote Delegated Auth (kein App-only seit 03/2025) | **Hoch** | Phase 3 verschieben; oder Service Account + Refresh Token Monitoring |
| Teams-SharePoint Deduplizierung | Mittel | `source_ref`-Matching beim Indexieren; Teams speichert nur Message-Text, File-Refs verweisen auf SP |
| SharePoint Resource-Unit-Budget | Mittel | RU-Tracking pro Tenant, `$batch` nutzen, Delta bevorzugen (1 RU statt 2) |
| `markitdown` scheitert bei komplexen Docs | Niedrig | Fallback auf python-docx/pptx/openpyxl; Graceful Skip + Logging |
| Große Tenants (Millionen Dokumente) | Mittel | Microsoft-Pattern: Discover → Initial Crawl → Subscribe → Delta; `$select` + `cTag` |
| Email-PII (Signaturen, CC-Listen) | Mittel | Presidio scannt Content + Metadata; sender/recipients → mandatory Pseudonymisierung |
| Mail-Volumen explodiert | Mittel | Zeitfenster-Filter (Default 12 Monate), Ordner-Whitelist |
| Teams-API strengere Rate Limits | Niedrig | Separates RU-Budget, größere Sync-Intervalle für Teams |

---

## 8. Empfohlener Phasenplan

| Phase | Scope | Aufwand | Ergebnis |
|-------|-------|---------|----------|
| **Phase 1** | SharePoint + OneDrive + Content Extraction | ~12-14 Tage | Dokumente aus SharePoint Sites im Index |
| **Phase 2** | Outlook Mail + Teams Messages | ~8-10 Tage | Mails + Teams-Konversationen im Index |
| **Phase 3** | OneNote (Delegated Auth) | ~4-5 Tage | OneNote-Seiten im Index |
| **Phase 4** (optional) | Per-Document ACLs (Onyx-Ansatz) | ~5-7 Tage | Granulare Permissions statt Site-Level |

**Gesamt Phase 1-3: ~24-29 Arbeitstage** (5-6 Wochen)
**Gesamt Phase 1-4: ~29-36 Arbeitstage** (6-7 Wochen)

---

## 9. Zusammenfassung

| Aspekt | Bewertung |
|--------|-----------|
| **Aufwand (Phase 1-3)** | ~24-29 Arbeitstage (1 Entwickler, 5-6 Wochen) |
| **Komplexität** | Hoch (Graph API + Delta Sync + OneNote-Auth-Sonderweg + Deduplizierung) |
| **Pipeline-Änderungen** | Minimal — `repo_sync_state.delta_link` Feld + Quality-Gate-Config |
| **Klassifizierung** | Site-Level Mapping in YAML (bestätigt durch Ökosystem-Review) |
| **Content Extraction** | `markitdown` (Microsoft) als primäre Lib, Fallback auf python-docx etc. |
| **Größtes Risiko** | OneNote Delegated Auth (Phase 3, required) |
| **Größter Aufwand** | Teams-SharePoint-Deduplizierung (von keinem Projekt gelöst) |
| **Dependencies** | `markitdown`, `msgraph-sdk`, `beautifulsoup4` (OneNote), `msal` (Auth) |
| **Ökosystem-Validierung** | Ansatz konsistent mit Elastic (Delta), Haystack (markitdown), Onyx (Permissions-Phase-2) |
