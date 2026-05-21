# Claude Chat Audit-Mirror — Konzept

**Date:** 2026-05-20
**Status:** Draft (Konzept zur Diskussion)
**Scope:** Detektivische PII-Audits über Claude-Pro/Max-Konversationen, die am
`pb-proxy` vorbeilaufen.

---

## 1. Problem

Claude Pro/Max (Abo) authentifiziert sich per OAuth gegen `claude.ai`.
`ANTHROPIC_BASE_URL` ist effektiv hartkodiert — Traffic kann **nicht** durch
`pb-proxy` umgeleitet werden (siehe [editions.md](../editions.md) +
[compliance-claude-desktop.md](../compliance-claude-desktop.md)). Folge: der
Chat-Kanal ist im Abo-Modus für Powerbrain unsichtbar. Eine echte Realtime-
Prävention von PII-Leaks ist über den Proxy nicht erreichbar.

Wirtschaftlich auf API-Tier zu wechseln ist für Solo-/Kleinteam-Setups oft
nicht attraktiv (Pro-Abo deckt 95 % der Coding-Sessions ab). Der Pragmatismus
heißt: **detektivisch statt präventiv**. Wir akzeptieren, dass Chat-Inhalte
ungescannt zu Anthropic gehen, schaffen aber einen Audit-Pfad, der retroaktiv
zeigt, *welche* personenbezogenen Daten in welcher Konversation gelandet sind.

## 2. Ziel

Drei messbare Ergebnisse:

1. **Vollständigkeit** — jede Conversation einer kontrollierten Anthropic-
   Identität wird mindestens einmal gegen `pb-ingestion /scan` geprüft.
2. **Auditierbarkeit** — Treffer (PII-Entitäten + Score + Quelle) landen
   tamper-evident im Powerbrain Audit-Log (Art. 12, `audit_log_entries`).
3. **Reaktion** — bei Treffern existiert ein definierter Workflow: Conversation
   in claude.ai löschen + Anthropic-Löschanforderung (Art. 17) + interne
   Incident-Notiz (`privacy_incidents`).

Explizit **kein** Ziel: Live-Tampering von Outgoing-Messages. Das geht im
Abo-Modus technisch nicht ohne TLS-MITM (siehe Abschnitt 4D).

## 3. Begriffsklärung — "Mirror"

Drei Interpretationen, die im Gespräch durcheinandergehen:

| Begriff | Was es heißt | Wird hier behandelt? |
|---|---|---|
| **Mirror** (Audit) | Conversations werden kopiert + nachträglich gescannt | ✅ Ja |
| **Inline-Scrubbing** | Outgoing-Text wird *vor* Anthropic mutiert | ❌ Nein (nur via Proxy oder Browser-Extension möglich) |
| **Realtime-Warning** | UI zeigt PII-Warnung *vor* dem Submit | Teilweise — Abschnitt 4B |

Dieser Plan adressiert primär den Audit-Pfad. Browser-Extension wird als
nahezu kostenlose Erweiterung mitgeführt.

## 4. Optionsraum

### A) Anthropic Data Export + Batch-Scan ⭐

Anthropic stellt per Art. 15/20 ein Export-ZIP aller Conversations als JSON
bereit (UI: `claude.ai → Settings → Privacy → Export data`, gibt's auch als
Endpoint, aber undokumentiert und sessionbasiert).

**Pipeline:**

```
[Cron, wöchentlich]
   │
   ▼
[exporter] ─── (Anthropic API / UI-Scrape) ──► ZIP
   │
   ▼
[scan-worker] ─── POST /scan ──► pb-ingestion
   │                                │
   │                                ▼
   │                          PII-Entitäten + Scores
   │
   ▼
[audit-writer] ── INSERT ──► audit_log_entries
                              (action=chat_audit, hash-chain)
   │
   ├─ bei Treffer: privacy_incidents (status=detected)
   └─ Mail/Slack-Report an Owner
```

**Vorteile:**
- Heute machbar, keine Anthropic-seitige Mitwirkung nötig.
- Funktioniert für Web + Desktop + Mobile (alle Clients schreiben in dieselbe
  serverseitige Conversation-Liste).
- Vollständig seit Account-Start.

**Nachteile:**
- Reaktiv. Daten waren mindestens 1 Woche (oder gewähltes Cron-Intervall) bei
  Anthropic, bevor sie erkannt werden.
- Kein offizieller Export-Endpoint — UI-getriebener Download. Automatisierung
  via Session-Cookie + Playwright-Headless oder via Pro-User-Token (sofern
  vorhanden).
- Token-/Cookie-Rotation kann den Job brechen.

**Aufwand:** ~1 Tag (Skript + Cron + Audit-Insert + Report-Mail).

### B) Browser-Extension für claude.ai

Chrome/Firefox-Extension, die das Composer-`<div contenteditable>` von claude.ai
beobachtet. Bei jedem Submit:

1. Greift Text aus DOM ab.
2. `POST /provider/scan` (siehe parallel laufender Vorschlag B aus Chat — neuer
   Proxy-Endpoint).
3. Treffer → Banner einblenden, Submit pausieren, User entscheidet:
   `senden / abbrechen / Pseudonyme einfügen`.
4. Entscheidung + Treffer → lokales Audit-Log + Powerbrain-Audit-Log.

**Vorteile:**
- Realtime, präventiv (für `claude.ai` Web).
- Funktioniert parallel zum Abo-OAuth (kein Eingriff in Submit-URL).
- Geringer Maintenance-Aufwand — DOM-Selektoren sind stabil seit Q1/2025.

**Nachteile:**
- Funktioniert **nicht** für Claude Desktop und Mobile.
- Browser-spezifisch (Chrome + Firefox separate Builds, aber identische Logik).
- User muss Extension manuell installieren + aktivieren.

**Aufwand:** ~2 Tage (Manifest V3, Content-Script, Background-Worker, Settings-
UI, Packaging).

### C) Claude Desktop lokale Cache-Inspektion

Claude Desktop (Electron) cached Conversations lokal:
`%APPDATA%\Claude\` (Windows), `~/Library/Application Support/Claude/` (macOS).
LevelDB/IndexedDB-Format. Ein Watcher könnte periodisch auslesen.

**Verworfen — Begründung:**
- Undokumentiertes Storage-Format.
- Verschlüsselung-at-rest in neueren Versionen.
- Schema kann sich mit jedem Desktop-Update ändern → permanenter Wartungsbedarf.
- Liefert dieselben Daten wie Variante A (Server-Authority), nur fragiler.

### D) Lokaler MITM-Proxy

mitmproxy + selbstsigniertes CA-Cert im OS-Trust-Store. Fängt
`api.anthropic.com`-Traffic ab, leitet 1:1 weiter, kopiert Bodies in eine
Audit-Queue.

**Verworfen für jetzt — Begründung:**
- Eingriff ins OS-Trust-Modell (Risikoexposition für *alle* HTTPS-Apps).
- Cert-Pinning in Claude Desktop wahrscheinlich → Connect-Fail.
- OAuth-Tokens an Hostname + Cert gebunden → hohes Brick-Risiko.
- Aufwand für sauberen Betrieb (CA-Rotation, Cert-Refresh, Auto-Start) erheblich.

Wird als Fallback für Anthropic-Enterprise-Cases dokumentiert, falls A nicht
vollständig genug ist.

## 5. Empfohlene Architektur

**Stack: A (Pflicht) + B (Quick-Win)**

```
┌──────────────────────────────────────────────────────────────┐
│  Claude-Konsum (Abo)                                          │
│  ┌─────────────┐  ┌──────────────┐  ┌──────────────┐         │
│  │ claude.ai   │  │ Claude       │  │ Claude       │         │
│  │ (Browser)   │  │ Desktop      │  │ Mobile       │         │
│  └──────┬──────┘  └──────┬───────┘  └──────┬───────┘         │
│         │                │                  │                │
│         ▼ (B: live)      │                  │                │
│  ┌─────────────┐         │                  │                │
│  │ pb-guardian │         │                  │                │
│  │ Extension   │         │                  │                │
│  └──────┬──────┘         │                  │                │
└─────────┼────────────────┼──────────────────┼────────────────┘
          │                │                  │
          │                ▼ (alles)          │
          │         ┌─────────────────────────┴─┐
          │         │  Anthropic Cloud           │
          │         │  (Conversations gespeichert)│
          │         └─────────────┬──────────────┘
          │                       │
          │           ┌───────────┴──────────┐
          │           │  A: Wöchentlicher    │
          │           │  Export-Worker       │
          │           │  (cron, int-baumeister)│
          │           └───────────┬──────────┘
          │                       │
          └───────────┬───────────┘
                      ▼
            ┌──────────────────┐
            │ pb-ingestion     │
            │  POST /scan      │
            └────────┬─────────┘
                     │
                     ▼
            ┌──────────────────┐
            │ pb-mcp-server    │
            │  audit_log_entries│
            │  privacy_incidents│
            └──────────────────┘
```

A liefert **Vollständigkeit** über alle Clients. B liefert **Realtime-
Prävention** für den häufigsten Einzelfall (claude.ai Web).

## 6. Komponenten

### 6.1 Export-Worker (Variante A)

**Name:** `pb-chat-auditor`
**Deployment:** neuer Container im `int-baumeister/services/powerbrain` Stack,
oder als pb-worker-Job. Bevorzugt: pb-worker-Job (vermeidet neuen Container).

**Konfiguration** (`worker/jobs/chat_audit.py`):

```python
CHAT_AUDIT_ENABLED: bool         # default: false
CHAT_AUDIT_SCHEDULE: str         # cron, default "0 3 * * 1" (Mo 3 Uhr)
CHAT_AUDIT_ANTHROPIC_COOKIE: secret  # claude.ai Session-Cookie
CHAT_AUDIT_USER_LABEL: str       # zur Zuordnung im Audit-Log
CHAT_AUDIT_REPORT_EMAIL: str     # Empfänger für Trefferreport
CHAT_AUDIT_REPORT_WEBHOOK: str   # optional, Slack/Teams
```

**Ablauf je Run:**

1. **Fetch** — Headless-Browser (Playwright) loggt sich via Cookie ein,
   triggert Export, wartet auf Download. Fallback: REST-Polling der internen
   `claude.ai/api/organizations/{org}/chat_conversations` (Pro-Account).
2. **Diff** — Zustand des letzten Runs (gespeichert in
   `chat_audit_state.last_conversation_ts`) bestimmt, welche Conversations neu
   oder geändert sind.
3. **Scan** — Für jede neue/geänderte Message: `POST /scan` mit `text`,
   `language: "de"`. Antwort enthält `pii_entities[]`.
4. **Persist** — Bei Treffer: Insert in `audit_log_entries`
   (`action=chat_audit`, `metadata={conversation_id, message_id, entities,
   max_score}`). Hash-Chain stellt Manipulation fest. Bei `score >= 0.8` oder
   sensiblen Typen (IBAN, PASSPORT): zusätzlich `privacy_incidents`-Row mit
   `status=detected`, `category=external_disclosure`.
5. **Report** — Aggregierter Report (HTML oder Markdown) per Mail/Webhook.

**Neue Tabellen:**

```sql
-- init-db/021_chat_audit.sql
CREATE TABLE chat_audit_state (
    source         TEXT PRIMARY KEY,    -- 'anthropic'
    last_export_at TIMESTAMPTZ,
    last_message_id TEXT,
    metadata       JSONB
);

CREATE TABLE chat_audit_findings (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    detected_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    source          TEXT NOT NULL,       -- 'anthropic'
    conversation_id TEXT NOT NULL,
    message_id      TEXT NOT NULL,
    role            TEXT NOT NULL,       -- 'user' | 'assistant'
    entities        JSONB NOT NULL,      -- [{type, score, start, end}]
    max_score       REAL NOT NULL,
    audit_log_id    UUID REFERENCES audit_log_entries(id),
    incident_id     UUID REFERENCES privacy_incidents(id)
);
CREATE INDEX idx_chat_audit_findings_conversation ON chat_audit_findings(conversation_id);
CREATE INDEX idx_chat_audit_findings_detected_at ON chat_audit_findings(detected_at DESC);
```

### 6.2 Browser-Extension (Variante B)

**Name:** `pb-guardian`
**Repo:** neues Sub-Verzeichnis `browser-extension/` im powerbrain-Repo, oder
eigenes Repo unter `nuts/pb-guardian`. Bevorzugt: separates Repo, weil
unabhängige Release-Kadenz und Chrome-Store-Publishing.

**Manifest V3, Permissions:**

```json
{
  "permissions": ["storage", "activeTab"],
  "host_permissions": [
    "https://claude.ai/*",
    "https://ai.nuetzliche.it/*"
  ]
}
```

**Komponenten:**

- `content-script.js` — DOM-Hook auf Composer-Element, intercept submit, scan,
  render Banner.
- `background.js` — Service-Worker, hält `pb_…`-Key in `chrome.storage.local`,
  proxied scan-Calls (vermeidet CORS-Probleme).
- `options.html` — Settings-Page: Endpoint-URL, API-Key, Sensitivität (`block`
  bei Score ≥ X / `warn-only`), Whitelist (z.B. eigene Mail-Adresse).

**Aufrufpfad:**

```
User tippt → DOM-Mutation-Observer →
  scan() debounced 800ms →
  POST /provider/scan + Bearer pb_… →
  ScanResponse {entities, scores} →
  Banner-Render falls Treffer →
  bei Submit-Event: preventDefault wenn block-Modus + Treffer
```

Erfordert den parallel laufenden Vorschlag **B aus dem Chat**: neuer
`POST /provider/scan` Endpoint im `pb-proxy`. Solange der nicht existiert, kann
die Extension den `INGESTION_AUTH_TOKEN` nicht direkt verwenden (Service-
Secret, nicht für Client-Distribution).

### 6.3 OPA-Policy

Neue Policy-Section `pb.chat_audit` in `opa-policies/pb/data.json`:

```json
{
  "chat_audit": {
    "enabled": true,
    "block_score_threshold": 0.85,
    "warn_score_threshold": 0.6,
    "auto_incident_types": ["IBAN", "PASSPORT", "CREDIT_CARD", "MEDICAL"],
    "whitelist_patterns": ["philipp@nuetzliche.it"],
    "report_recipients": ["dpo@nuetzliche.it"]
  }
}
```

Admin kann Schwellwerte und Auto-Incident-Trigger ohne Restart über
`manage_policies`-MCP-Tool ändern.

## 7. Rollout

| Phase | Inhalt | Aufwand | Wert |
|---|---|---|---|
| **0** | `POST /provider/scan` Endpoint im pb-proxy (Voraussetzung für B) | 0.5 d | unlocks B |
| **1** | A — Export-Worker als pb-worker-Job, Migration 021, Mail-Report | 1.5 d | ✅ Vollständige Auditierung |
| **2** | B — pb-guardian Extension, Chrome-Store-Submit | 2 d | ⚠️ Live-Prävention claude.ai Web |
| **3** | Dashboard in Grafana: "Chat PII Findings per Week" + Auto-Incident-Tile | 0.5 d | Sichtbarkeit |
| **4** | OPA-Policy-Tuning auf Basis erster Treffer (false-positive-Rate) | iterativ | Qualität |

Phasen 1 und 2 können parallel laufen (verschiedene Skill-Sets).

## 8. Compliance-Wirkung

Was der Mirror-Pfad bringt — präzise formuliert, damit keine
Überversprechen entstehen:

| Aspekt | Vorher (Abo + nichts) | Nachher (A + B) |
|---|---|---|
| **DSGVO Art. 32** (TOM) | Nur Disclaimer | Detektive Kontrolle, dokumentiert |
| **DSGVO Art. 12** (Audit) | — | Hash-Chain, tamper-evident |
| **DSGVO Art. 33** (Meldepflicht) | manuell, ohne Datenbasis | Auto-Trigger bei kritischen Entitäten |
| **DSGVO Art. 17** (Löschung) | manuell, ohne Liste | Worker liefert Conversation-IDs zum gezielten Löschen |
| **EU-AI-Act Art. 12** (Logging) | — | Vollständiger Audit-Stream über externes AI-System |
| **Schrems II / Transfer** | unverändert problematisch | unverändert — der Mirror löst kein Transfer-Problem, dokumentiert es nur |

**Wichtig:** Der Mirror ersetzt **nicht** den AVV/DPA mit Anthropic. Wer mit
Kundendaten arbeitet, braucht weiterhin Claude for Work / Enterprise (DPA +
Zero Retention) oder API-Tier über `pb-proxy`. Der Mirror ist die Brücke für
Solo-User mit Pro-Abo, die zumindest **wissen wollen**, was sie geleakt
haben — und schnell reagieren können.

## 9. Bekannte Limitationen

1. **Anthropic-Export-Endpoint ist undokumentiert.** Bricht potentiell mit
   UI-Refreshs. Mitigation: Playwright statt API, regelmäßiges Smoke-Test.
2. **Session-Cookies haben TTL** (~ 30 Tage). Worker braucht Cookie-Refresh-
   Mechanismus oder manuelle Rotation. Alternative: Anthropic-Account-Token
   (falls Pro-API verfügbar).
3. **Anthropic löscht Conversations nach 30 Tagen retention.** Wenn der Cron
   später als 30 Tage läuft, fehlen Daten. Cron sollte ≤ 7 Tage takten.
4. **Multi-User-Scenario nicht abgedeckt.** Konzept ist Solo-Use-Case. Für
   Teams müssten Cookies pro User verwaltet werden — Phase 5+.
5. **Mobile-Submits in B nicht abgedeckt.** Extension läuft nur im Browser. A
   fängt das durch Server-seitigen Export aber auf.
6. **Self-Surveillance-Aspekt.** Der Owner des Cookies kann eigene
   Conversations lesen — das ist gewollt, aber sollte in der internen Policy
   transparent gemacht werden (Mitarbeiterinformation falls jemals
   Mehr-User-Setup).

## 10. Offene Fragen

1. **Cookie-Beschaffung** — manueller Login + Cookie-Extraction (Browser-
   DevTools) oder Playwright mit gespeicherten Credentials? Letzteres bedeutet
   Passwort-Speicherung auf int-baumeister.
2. **Reporting-Format** — HTML-Mail vs. Slack-Webhook vs. Grafana-Dashboard?
   Vermutlich alle drei, aber Prio 1?
3. **Browser-Extension Veröffentlichung** — public im Chrome Web Store oder
   privat per `.crx`-Side-Load? Public hat Review-Cycle (~ 1 Woche), privat
   ist Self-Update-fähig erst mit eigener Update-URL.
4. **Lösch-Workflow** — nach Treffer auto-delete in claude.ai (via Cookie-
   API) oder nur Hinweis an User? Auto-delete ist riskanter, aber konformer.
5. **Findings-Aufbewahrung** — wie lange `chat_audit_findings` selbst halten?
   Vorschlag: 1 Jahr, dann Aggregation auf Monatszahlen.

## 11. Nächste Schritte

Wenn das Konzept grundsätzlich abgenommen ist:

1. Spec für Phase 0 (`/provider/scan`-Endpoint) schreiben → `docs/specs/`.
2. Spec für Phase 1 (Export-Worker) schreiben → `docs/specs/`.
3. Browser-Extension-Repo aufsetzen (`nuts/pb-guardian`).
4. OPA-Policy-Section + Migration 021 als kleinen Vorlauf-PR auf
   `nuts/powerbrain` einreichen.

---

**Cross-References:**

- [editions.md](../editions.md) — Edition-Boundary, drei Datenpfade
- [compliance-claude-desktop.md](../compliance-claude-desktop.md) — Drei-Tier-
  Mitigation-Modell, hier ist Tier 2 (detective chat-history ingest)
  spezifiziert
- [gdpr-external-ai-services.md](../gdpr-external-ai-services.md) — DPA-
  Matrix, AVV-Status pro Anthropic-Plan
- [risk-management.md](../risk-management.md) — Risikoregister-Eintrag für
  "Chat-Leak im Abo-Modus"
