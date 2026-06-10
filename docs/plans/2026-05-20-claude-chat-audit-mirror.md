# Claude Chat Audit-Mirror — Konzept

**Date:** 2026-05-20 (refresh 2026-05-22 für Drei-Tab-Struktur)
**Status:** Draft (Konzept zur Diskussion)
**Scope:** Detektivische PII-Audits über Claude-Pro/Max-Konversationen (alle drei
Tabs: Chat, Cowork, Code), die am `pb-proxy` vorbeilaufen.
**Companion-Spec:** [Claude Desktop Coverage Strategy](../specs/2026-05-22-claude-desktop-coverage-strategy.md) — dieser Plan ist Komponente **B (Audit Mirror)** in der dort formulierten Vier-Komponenten-Verteidigung.

---

## 1. Problem

Claude Pro/Max (Abo) authentifiziert sich per OAuth gegen `claude.ai`.
`ANTHROPIC_BASE_URL` ist effektiv hartkodiert — Traffic kann **nicht** durch
`pb-proxy` umgeleitet werden (siehe [editions.md](../editions.md) +
[compliance-claude-desktop.md](../compliance-claude-desktop.md)). Folge: alle
drei Claude-Desktop-Tabs (**Chat**, **Cowork**, **Code**) leiten Inhalte am
Powerbrain-Proxy vorbei direkt zu Anthropic.

Anthropics 2026er Policy-Bewegungen verstärken die Notwendigkeit eines
detektivischen Pfads zusätzlich:

- **2026-02 Authentication and Credential Use Policy** — OAuth-Credentials sind
  "intended exclusively for Claude Code and claude.ai". Drittclients mit
  Pro/Max-Token sind explizit policy-widrig. Damit fällt jeder Wrapper-/
  Proxy-Ansatz für Pro/Max aus.
- **2026-04-04 Third-Party-Harness-Enforcement** — technische Sperre für
  programmatische Pro/Max-Nutzung. Verhindert pragmatische Workarounds.

Eine echte Realtime-Prävention von PII-Leaks ist im Abo-Modus mit
Powerbrain-Mitteln **nicht erreichbar**. Wirtschaftlich auf API-Tier zu
wechseln ist für Solo-/Kleinteam-Setups oft nicht attraktiv (Pro-Abo deckt
95% der Coding-Sessions ab), und für Heavy-Coding-Agent-Use auf Org-Ebene
ist API-Pricing prohibitiv (5–20× der Abo-Kosten).

Der Pragmatismus heißt: **detektivisch statt präventiv**. Wir akzeptieren,
dass Inhalte aus allen drei Tabs ungescannt zu Anthropic gehen, schaffen
aber einen Audit-Pfad, der retroaktiv zeigt, *welche* personenbezogenen
Daten in welcher Konversation gelandet sind — und in welcher Tab-Kategorie.

## 2. Ziel

Drei messbare Ergebnisse, jeweils tab-übergreifend (Chat/Cowork/Code):

1. **Vollständigkeit** — jede Conversation einer kontrollierten Anthropic-
   Identität wird mindestens einmal gegen `pb-ingestion /scan` geprüft. Code-Tab-
   Konversationen umfassen zusätzlich File-Edit-Diffs und Terminal-Command-
   Logs aus der Session-History.
2. **Auditierbarkeit** — Treffer (PII-Entitäten + Score + Tab + Quelle) landen
   tamper-evident im Powerbrain Audit-Log (Art. 12, `audit_log_entries`).
3. **Reaktion** — bei Treffern existiert ein definierter Workflow: Conversation
   in claude.ai löschen + Anthropic-Löschanforderung (Art. 17) + interne
   Incident-Notiz (`privacy_incidents`).

Explizit **kein** Ziel: Live-Tampering von Outgoing-Messages. Das geht im
Abo-Modus technisch nicht ohne TLS-MITM (siehe Abschnitt 4D) und ist nach
2026-04 Anthropic-Enforcement auch policy-widrig.

## 3. Begriffsklärung — "Mirror" und Abgrenzung zu Pre-flight

Drei Interpretationen, die im Gespräch durcheinandergehen:

| Begriff | Was es heißt | Wird hier behandelt? |
|---|---|---|
| **Mirror** (Audit) | Conversations werden kopiert + nachträglich gescannt | ✅ Ja, das ist der Kern dieses Plans |
| **Inline-Scrubbing** | Outgoing-Text wird *vor* Anthropic mutiert | ❌ Nein (für Claude Desktop nicht möglich; für claude.ai-Web theoretisch via Extension/Pre-flight, siehe unten) |
| **Realtime-Warning** | UI zeigt PII-Warnung *vor* dem Submit | Teilweise — Abschnitt 4B für claude.ai-Web; via separatem Tool [Pre-flight](../specs/2026-05-22-claude-desktop-coverage-strategy.md#d-pre-flight-new-component--this-specs-only-new-build) für Desktop-Workflows |

Dieser Plan adressiert primär den Audit-Pfad (Komponente B in der Coverage
Strategy). Browser-Extension (4B) und Pre-flight (separater Spec) sind die
präventiven Pendants:

- **Browser-Extension** — claude.ai-Web spezifisch, intercepted Composer-DOM,
  zeigt Warnung vor Submit. Funktioniert nur im Browser-Tab, nicht in Claude
  Desktop.
- **Pre-flight (separater Spec)** — Tauri-App, hilft dem User PII zu pseudonymi-
  sieren *bevor* er in Claude Desktop pastet. Klipboard-basiert, ToS-clean, weil
  sie Claude Desktop nicht anfasst.

Die Komponenten sind komplementär: Audit-Mirror fängt detektivisch auf, was
Pre-flight präventiv verpasst.

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
- **Tab-übergreifend:** Export liefert Chat-, Cowork- und Code-Tab-Sessions im
  selben Dump (jeweils mit `tab`-Metadatum), inklusive Code-Tab-spezifischer
  Artefakte (File-Edit-Diffs, Terminal-Commands, Computer-Use-Events).
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

*Scope-Hinweis: deckt **nur** claude.ai-Web ab (Chat-Tab im Browser). Für
Claude Desktop nicht anwendbar — siehe Pre-flight-Spec in der Coverage
Strategy für Desktop-Workflows.*

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
LevelDB/IndexedDB-Format. Ein Watcher könnte periodisch auslesen — pro Tab
liegen die Session-Daten in unterschiedlichen Sub-Stores (Chat / Cowork / Code).

**Verworfen für die primäre Pipeline — Begründung:**
- Undokumentiertes Storage-Format, pro Tab unterschiedliches Schema.
- Verschlüsselung-at-rest in neueren Versionen.
- Schema kann sich mit jedem Desktop-Update ändern → permanenter Wartungsbedarf.
- Liefert dieselben Daten wie Variante A (Server-Authority), nur fragiler.
- Code-Tab speichert Datei-Inhalte teilweise als Referenz auf den User-Filesystem-
  Pfad statt embedded — Inspektion müsste dem Pfad folgen und Datei-Inhalt
  nachladen.

**Bleibt als Fallback dokumentiert** für Cases, in denen Anthropic-Export-Endpoint
unverfügbar oder gedrosselt ist (Stand 2026-05-22 unproblematisch, aber API-Wandel
ist eine reale Sorge). In dem Fall braucht es einen Per-Tab-Schema-Adapter ähnlich
dem in der Coverage Strategy (Open Question §2) skizzierten DOM-Adapter-Modell.

### D) Lokaler MITM-Proxy

mitmproxy + selbstsigniertes CA-Cert im OS-Trust-Store. Fängt
`api.anthropic.com`-Traffic ab, leitet 1:1 weiter, kopiert Bodies in eine
Audit-Queue.

**Verworfen — Begründung:**
- Eingriff ins OS-Trust-Modell (Risikoexposition für *alle* HTTPS-Apps).
- Cert-Pinning in Claude Desktop wahrscheinlich → Connect-Fail.
- OAuth-Tokens an Hostname + Cert gebunden → hohes Brick-Risiko.
- Aufwand für sauberen Betrieb (CA-Rotation, Cert-Refresh, Auto-Start) erheblich.
- **2026-02 Anthropic Authentication Policy:** OAuth-Credentials sind
  ausschließlich für Anthropic-eigene Clients freigegeben. Ein MITM-Proxy ist
  per Definition kein Anthropic-Client und damit policy-widrig — selbst wenn
  technisch betriebsfähig.

Damit ist D nicht mehr als "Enterprise-Fallback" haltbar; für Enterprise-Cases
verweist die [Coverage Strategy](../specs/2026-05-22-claude-desktop-coverage-strategy.md)
auf Endpoint-DLP (Komponente C) + API-Key-Mode-Workloads als getrennten Pfad.

## 5. Empfohlene Architektur

**Stack: A (Pflicht, alle Tabs) + B (Quick-Win, claude.ai-Web nur)**
**Komplementär: Pre-flight aus der [Coverage Strategy](../specs/2026-05-22-claude-desktop-coverage-strategy.md) für präventive Desktop-Workflows.**

```
┌──────────────────────────────────────────────────────────────────────┐
│  Claude-Konsum (Pro/Max-Abo)                                          │
│                                                                       │
│  ┌─────────────┐    ┌────────────────────────────────────────────┐   │
│  │ claude.ai   │    │ Claude Desktop                              │   │
│  │ (Browser)   │    │ ┌──────┐ ┌────────┐ ┌────────────────────┐ │   │
│  │             │    │ │ Chat │ │ Cowork │ │ Code               │ │   │
│  └──────┬──────┘    │ │      │ │        │ │ (file edits,       │ │   │
│         │           │ │      │ │        │ │  terminal, CU)     │ │   │
│         │           │ └──────┘ └────────┘ └────────────────────┘ │   │
│         │           └─────────────────┬───────────────────────────┘   │
│         ▼ (B: live)                   │                               │
│  ┌─────────────┐                      │                               │
│  │ pb-guardian │                      │                               │
│  │ Extension   │                      │                               │
│  └──────┬──────┘                      │                               │
└─────────┼─────────────────────────────┼───────────────────────────────┘
          │                             │
          │                ▼ (alles, alle Tabs)
          │         ┌─────────────────────────────┐
          │         │  Anthropic Cloud             │
          │         │  Conversations + Code-Sessions│
          │         │  (Server-side storage)        │
          │         └─────────────┬────────────────┘
          │                       │
          │           ┌───────────┴──────────┐
          │           │  A: Periodischer     │
          │           │  Export-Worker       │
          │           │  (cron, pb-worker)   │
          │           └───────────┬──────────┘
          │                       │
          └───────────┬───────────┘
                      ▼
            ┌──────────────────┐
            │ pb-ingestion     │
            │  POST /scan      │
            │  (text + diffs +  │
            │   commands)       │
            └────────┬─────────┘
                     │
                     ▼
            ┌──────────────────────┐
            │ pb-mcp-server        │
            │  audit_log_entries   │  ← findings tagged {tab, source}
            │  privacy_incidents   │
            │  chat_audit_findings │
            └──────────────────────┘
```

A liefert **Vollständigkeit** über alle Clients und alle Tabs (Chat/Cowork/Code).
B liefert **Realtime-Prävention** für den häufigsten Einzelfall (claude.ai Web).
Pre-flight (separater Spec) ergänzt B für Desktop-Workflows ohne in Claude
Desktop einzugreifen.

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
   Export umfasst alle drei Tabs; pro Conversation ist das Feld `tab` (oder
   ein äquivalenter Discriminator) auszuwerten.
2. **Diff** — Zustand des letzten Runs (gespeichert in
   `chat_audit_state.last_conversation_ts`) bestimmt, welche Conversations neu
   oder geändert sind.
3. **Scan — pro Tab unterschiedliche Payload-Typen:**
   - **Chat-Tab:** je Message → `POST /scan` mit `text`, `language: "de"`.
     Antwort enthält `pii_entities[]`.
   - **Cowork-Tab:** je Task-Phase + jeder Reportabschnitt → `POST /scan`. Längere
     Texte, ggf. Chunking auf ~2KB-Stücke vor dem Scan.
   - **Code-Tab:** zusätzlich zur Chat-History sind zu scannen:
     - **File-Edit-Diffs** — pro Edit der Diff-Text (`+` und `-` Zeilen). PII
       in Kommentaren / Strings / Variablennamen ist hier real.
     - **Terminal-Commands** — Command-Line + stdout/stderr-Capture. Häufige
       Treffer: Pfade mit User-Home, hostname, IP-Adressen, Tokens in env-Dumps.
     - **Computer-Use-Actions** — UI-Actions (click, type) protokollieren
       getippten Text. Falls vorhanden im Session-Log: scannen.
4. **Persist** — Bei Treffer: Insert in `audit_log_entries`
   (`action=chat_audit`, `metadata={conversation_id, message_id, tab,
   artifact_type, entities, max_score}`). Hash-Chain stellt Manipulation
   fest. Bei `score >= 0.8` oder sensiblen Typen (IBAN, PASSPORT):
   zusätzlich `privacy_incidents`-Row mit `status=detected`,
   `category=external_disclosure`, `data_category` abgeleitet aus `tab`.
5. **Report** — Aggregierter Report (HTML oder Markdown) per Mail/Webhook,
   gruppiert nach Tab und Artifact-Type.

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
    tab             TEXT NOT NULL,       -- 'chat' | 'cowork' | 'code'
    artifact_type   TEXT NOT NULL,       -- 'message' | 'file_diff' | 'terminal_command' | 'cu_action' | 'cowork_phase'
    conversation_id TEXT NOT NULL,
    message_id      TEXT NOT NULL,       -- für Code-Tab: edit-id / command-id
    role            TEXT NOT NULL,       -- 'user' | 'assistant' | 'tool'
    entities        JSONB NOT NULL,      -- [{type, score, start, end}]
    max_score       REAL NOT NULL,
    audit_log_id    UUID REFERENCES audit_log_entries(id),
    incident_id     UUID REFERENCES privacy_incidents(id)
);
CREATE INDEX idx_chat_audit_findings_conversation ON chat_audit_findings(conversation_id);
CREATE INDEX idx_chat_audit_findings_detected_at ON chat_audit_findings(detected_at DESC);
CREATE INDEX idx_chat_audit_findings_tab ON chat_audit_findings(tab, detected_at DESC);
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

Aligned mit der [Coverage Strategy](../specs/2026-05-22-claude-desktop-coverage-strategy.md#roll-out)
Phasen-Nummerierung:

| Phase | Coverage-Strategy-Mapping | Inhalt | Aufwand | Wert |
|---|---|---|---|---|
| **A.0** | (Voraussetzung) | `POST /provider/scan` Endpoint im pb-proxy (Voraussetzung für B) | 0.5 d | unlocks B |
| **A.1** | Phase 2 "Audit Mirror MVP" — macOS | A — Export-Worker als pb-worker-Job, Migration 021, Mail-Report, **Chat-Tab only**, Schema mit `tab`/`artifact_type` aber populated nur für Chat | 1.5 d | ✅ Tab-`chat` vollständig auditiert |
| **A.2** | Phase 2 Erweiterung | A erweitert auf **Cowork**-Tab (Task-Phase-Chunking) | 0.5 d | ✅ Tab-`cowork` auditiert |
| **A.3** | Phase 2 Erweiterung | A erweitert auf **Code**-Tab (File-Diffs, Terminal-Commands, Computer-Use-Actions) | 2 d | ✅ Tab-`code` auditiert — der größte Coverage-Sprung |
| **B.1** | Phase 2 Defence-in-Depth | B — pb-guardian Extension, Chrome-Store-Submit (claude.ai-Web nur, Chat-Composer) | 2 d | ⚠️ Live-Prävention für den Browser-Use-Case |
| **C.1** | Phase 1 "Document + position" | Dashboard in Grafana: "Chat PII Findings per Tab per Week" + Auto-Incident-Tile | 0.5 d | Sichtbarkeit |
| **C.2** | (laufend) | OPA-Policy-Tuning auf Basis erster Treffer (false-positive-Rate, per Tab) | iterativ | Qualität |

Phasen A.1 / B.1 / C.1 können parallel laufen (verschiedene Skill-Sets).
A.3 ist der höchste Aufwand, hat aber auch den höchsten Compliance-Wert —
Code-Tab ist im Kunden-Use-Case der Hauptkanal.

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
7. **Code-Tab — File-Inhalte vs. Diffs.** Der Server-Export liefert die
   Diffs (Edit-Operationen), nicht zwingend den vollständigen File-Inhalt
   nach jeder Edit. Wenn PII nur in unveränderten File-Teilen existiert,
   die der Coding-Agent gelesen aber nicht geändert hat, ist die im Diff
   nicht sichtbar. Mitigation: Pre-flight (separater Spec) `resolve file`-
   Mode kann ergänzend ganze Files gegen den Vault scannen.
8. **Code-Tab — Computer-Use-Captures.** Ob Anthropic-Export die Computer-
   Use-Actions (incl. getippter Text in andere Apps) lückenlos protokolliert,
   ist nicht vollständig dokumentiert. Falls nicht: Audit-Lücke für diesen
   Modus. Endpoint-DLP (Coverage Strategy Komponente C) ist hier die
   ergänzende Antwort.
9. **Cowork-Tab — Long-Context-Chunking.** Cowork-Tasks können sehr lange
   Texte enthalten. Naïves `/scan` läuft in Presidio-Timeouts. Chunking auf
   ~2KB ist nötig, mit Aufmerksamkeit auf Entity-Splits über Chunk-Grenzen
   (Mitigation: 200-Byte-Overlap zwischen Chunks).

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

Wenn das Konzept (und die übergeordnete [Coverage Strategy](../specs/2026-05-22-claude-desktop-coverage-strategy.md))
grundsätzlich abgenommen sind:

1. Spec für Phase A.0 (`/provider/scan`-Endpoint im pb-proxy) schreiben → `docs/specs/`.
2. Spec für Phase A.1 (Export-Worker, Chat-Tab) schreiben → `docs/specs/`.
3. Tab-Adapter-Spezifikation für A.2 (Cowork) + A.3 (Code) als
   Folge-Spec — die Adapter folgen demselben Selector-Adapter-Muster, das in
   der Coverage Strategy für Pre-flight skizziert ist.
4. Browser-Extension-Repo aufsetzen (`nuts/pb-guardian`) — Scope explizit
   auf claude.ai-Web halten, Desktop-Workflows decken durch Pre-flight ab.
5. OPA-Policy-Section + Migration 021 als kleinen Vorlauf-PR auf
   `nuts/powerbrain` einreichen.

---

**Cross-References:**

- [editions.md](../editions.md) — Edition-Boundary, drei Datenpfade (ingest /
  tool-calls / chat-content)
- [compliance-claude-desktop.md](../compliance-claude-desktop.md) — Drei-Tier-
  Mitigation-Modell, hier ist Tier 2 (detective chat-history ingest)
  spezifiziert
- [Coverage Strategy](../specs/2026-05-22-claude-desktop-coverage-strategy.md)
  — übergeordneter Spec, in dem dieser Plan als Komponente B verankert ist;
  enthält das Vier-Komponenten-Modell (MCP Connector / Audit Mirror /
  Endpoint DLP / Pre-flight) und das Decision-Record gegen Wrapper-Ansätze
- [gdpr-external-ai-services.md](../gdpr-external-ai-services.md) — DPA-
  Matrix, AVV-Status pro Anthropic-Plan
- [risk-management.md](../risk-management.md) — Risikoregister-Eintrag für
  "Chat-Leak im Abo-Modus"
