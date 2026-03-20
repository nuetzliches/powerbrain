# Sealed Vault: Dual Storage mit Pseudonymisierung

**Datum:** 2026-03-20
**Status:** Approved
**Ansatz:** Sealed Vault Pattern (Ansatz A)

## Motivation

Die KB speichert aktuell alle Daten destruktiv maskiert (`<PERSON>`, `<EMAIL_ADDRESS>`).
Das verursacht zwei Probleme:

1. **Suchqualität:** Maskierte Platzhalter zerstören semantische Information in Embeddings.
   Pseudonymisierter Text (deterministische Hashes) erhält die Satzstruktur und liefert bessere Vektoren.
2. **Originalzugriff:** Berechtigte Rollen (Support, Legal) benötigen manchmal die echten Daten
   für Kundenanfragen, Vertragsfragen oder rechtliche Auskunft.

## Entscheidungen

| Entscheidung | Gewählt | Grund |
|---|---|---|
| Trennungsmodell | Separates PostgreSQL-Schema (`pii_vault`) mit RLS | Saubere Abgrenzung ohne Microservice-Overhead |
| Dual-Storage-Steuerung | OPA-Policy (`dual_storage_enabled` pro Klassifizierung) | Policy-driven, änderbar ohne Code-Deployment |
| Originalzugriff | Parameter an bestehende Tools + `pii_access_token` | Kein separates Tool nötig, aber explizite Elevation |
| Token-Modell | HMAC-signiert, kurzlebig (15 Min), zweckgebunden | Leichtgewichtig, unabhängig von Auth-Infrastruktur |

## 1. Architektur & Datenmodell

### Neues Schema: `pii_vault`

Das Vault-Schema lebt in derselben PostgreSQL-Instanz, ist über Row-Level Security (RLS)
und einen dedizierten DB-Benutzer (`mcp_vault_reader`) abgesichert.

**Tabellen:**

```
pii_vault.original_content
  - id (UUID, PK)
  - document_id (FK → documents_meta.id)
  - chunk_index (INT)
  - original_text (TEXT)
  - pii_entities (JSONB)          -- erkannte Entities mit Typ, Position, Confidence
  - stored_at (TIMESTAMPTZ)
  - retention_expires_at (TIMESTAMPTZ)
  - data_category (VARCHAR)

pii_vault.pseudonym_mapping
  - id (UUID, PK)
  - document_id (FK → documents_meta.id)
  - chunk_index (INT)
  - pseudonym (VARCHAR)           -- 8-Char SHA-256 Hash
  - entity_type (VARCHAR)         -- PERSON, EMAIL, etc.
  - salt (VARCHAR)                -- Projekt-Salt Referenz
  - created_at (TIMESTAMPTZ)

pii_vault.vault_access_log
  - id (UUID, PK)
  - agent_id (VARCHAR)
  - document_id (UUID)
  - purpose (VARCHAR)
  - token_hash (VARCHAR)          -- Hash des Tokens, nicht Token selbst
  - accessed_at (TIMESTAMPTZ)

pii_vault.project_salts
  - project_id (UUID, FK → projects.id)
  - salt (VARCHAR)                -- Kryptografisch zufällig generiert
  - created_at (TIMESTAMPTZ)
  - rotated_at (TIMESTAMPTZ)      -- NULL bis zur ersten Rotation
```

**Qdrant-Payload Erweiterung:**

Bestehende Qdrant-Punkte bekommen ein neues Feld:
- `vault_ref` (UUID, nullable) — Zeigt auf `pii_vault.original_content.id`, nur gesetzt wenn PII erkannt wurde.

### OPA-Policy Erweiterung

Neue Rule `dual_storage_enabled` in `kb.privacy`:

| Klassifizierung | pii_action    | dual_storage |
|-----------------|---------------|--------------|
| public          | mask          | false        |
| internal        | pseudonymize  | true         |
| confidential    | pseudonymize  | true         |
| restricted      | block         | false        |

## 2. Ingestion-Flow

```
Daten kommen rein (MCP ingest_data)
    │
    ▼
PII Scanner (Presidio)
  → PIIScanResult: contains_pii, entities, entity_locations
    │
    ▼
OPA Policy Check: kb.privacy.pii_action + kb.privacy.dual_storage_enabled
  Input: classification, contains_pii, legal_basis
  → pii_action: mask | pseudonymize | block
  → dual_storage: true | false
    │
    ├── "block" → Reject, log reason
    │
    ├── "mask" + dual_storage=false
    │   → Bisheriger Flow: mask → embed → Qdrant (kein Vault)
    │
    └── "pseudonymize" + dual_storage=true
        │
        1. pseudonymize_text(text, project_salt)
           → pseudonymized_text + mapping{original→pseudonym}
        2. Embed pseudonymized_text via Ollama → 768d Vektor
        3. PostgreSQL-Transaktion:
           - pii_vault.original_content INSERT (original_text, pii_entities)
           - pii_vault.pseudonym_mapping INSERT (je ein Eintrag pro Entity)
           - pii_scan_log INSERT (action_taken="pseudonymize")
        4. Qdrant upsert:
           - text = pseudonymized_text
           - embedding = Vektor
           - vault_ref = UUID aus Schritt 3
           - contains_pii = true
        5. Bei Qdrant-Fehler: Vault-Eintrag als "orphaned" markieren
```

**Salt-Management:** Jedes Projekt bekommt einen eigenen Salt in `pii_vault.project_salts`.
Gleicher Name + gleicher Salt = gleiches Pseudonym (konsistent innerhalb eines Projekts),
aber projektübergreifend nicht korrelierbar.

**Bug-Fix:** Die bestehende `pseudonymize_text()` hat einen Bug — wenn mehrere Entities
gleichen Typs vorkommen, wird nur das letzte Pseudonym für alle verwendet. Wird behoben:
je Entity ein eigener Mapping-Eintrag mit individueller Pseudonymisierung.

**Transaktionalität:** Vault zuerst schreiben (PostgreSQL-Transaktion), dann Qdrant.
Keine verteilte Transaktion. Cleanup-Job räumt verwaiste Vault-Einträge auf.

## 3. Zugriffs- und Token-Mechanismus

### Standard-Suchpfad (ohne Token)

Unverändert: Qdrant-Suche → OPA access check → Reranker → pseudonymisierter Text.
`vault_ref` wird **nicht** zurückgegeben.

### Elevated Access (mit Token)

```
pii_access_token = {
    "agent_id": "support-agent-42",
    "role": "analyst",
    "purpose": "support",
    "scope": ["document_id_xyz"],       # Optional: Einschränkung
    "issued_at": "2026-03-20T10:00:00Z",
    "expires_at": "2026-03-20T10:15:00Z",  # 15 Min
    "issued_by": "admin"
}
```

HMAC-signiert (Shared Secret). Kein JWT/OAuth nötig.

**Validierungskette:**

1. Token-Signatur prüfen (HMAC)
2. Token-Ablauf prüfen
3. OPA `kb.privacy.vault_access`:
   - Purpose in `allowed_purposes` der data_category?
   - Rolle hat Zugriff auf diese Klassifizierung?
4. Feld-Redaktion via OPA `kb.privacy.fields_to_redact`:
   - z.B. purpose=reporting → redact: email, iban, birthdate
   - Agent sieht nur die für den Zweck erlaubten Felder
5. `vault_access_log` INSERT (agent_id, document_id, purpose, token_hash)
6. Ergebnis mit (teilweise redaktiertem) Originaltext

**Datenminimierung:** Auch bei Originalzugriff liefert OPA `fields_to_redact`.
Nicht alle PII-Felder werden offengelegt — nur die für den Zweck erlaubten.

## 4. Art. 17 Löschung & Retention

### Lösch-Strategie (2 Stufen)

**Stufe 1: Restrict (Art. 18 — Aufbewahrungspflicht besteht)**

- `pii_vault.original_content` → gelöscht
- `pii_vault.pseudonym_mapping` → gelöscht
- Qdrant-Punkte bleiben (pseudonymisierter Text)
- Pseudonym ist jetzt irreversibel = de facto anonym
- `contains_pii` → false setzen
- `deletion_requests.status` → "completed"

Vorteil: Suchindex bleibt erhalten, Daten sind aber nicht mehr personenbezogen.

**Stufe 2: Delete (keine Aufbewahrungspflicht)**

- Alles aus Stufe 1, plus:
- Qdrant-Punkte löschen
- `dataset_rows` mit PII-Bezug löschen
- `agent_access_log` anonymisieren (data_subject_ref → NULL)

### Retention-Cleanup Erweiterung

Der bestehende `retention_cleanup.py` wird erweitert:

1. Query: `pii_vault.original_content WHERE retention_expires_at < NOW()`
2. Mapping löschen, Original löschen
3. Qdrant-Payload: `vault_ref → NULL`
4. Log in `pii_scan_log`

**Invariante:** Vault-Einträge haben immer eine kürzere oder gleiche Retention
wie die zugehörigen Qdrant-Punkte. Das Original verfällt nie später als die
pseudonymisierte Version.

## Bestehende Lücken die mitgeschlossen werden

Diese Implementierung schließt mehrere der in `docs/bekannte-schwachstellen.md`
dokumentierten Lücken:

| Lücke | Wie geschlossen |
|---|---|
| OPA `kb.privacy.pii_action` wird nie aufgerufen | Ingestion ruft OPA Policy auf |
| `pseudonymize_text()` wird nie aufgerufen | Dual Storage Path nutzt Pseudonymisierung |
| `pii_scan_log` wird nie beschrieben | Ingestion loggt jeden Scan |
| `data_subjects` wird nie befüllt | Pseudonym-Mapping referenziert data_subjects |
| `datasets.pseudonymized` wird nie gesetzt | Ingestion setzt Flag korrekt |
| `fields_to_redact` wird nie angewendet | Vault-Access wendet Feld-Redaktion an |
| Bug in `pseudonymize_text()` (Entity-Überschreibung) | Fix: individuelle Pseudonyme pro Entity |

## Nicht im Scope

- **Authentifizierung (P1-1):** Token-Mechanismus ist unabhängig von Auth.
  Echte Auth kommt separat.
- **SQL/Cypher Injection Fixes (P1-2, P1-3):** Separate Arbeit.
- **Encrypt-and-Store:** Die OPA-Policy sieht es vor, wird aber auf Pseudonymisierung
  für `confidential` umgestellt. Verschlüsselung kann später ergänzt werden.
- **Key Rotation für Salts:** Schema unterstützt `rotated_at`, aber Rotation-Logik
  ist nicht Teil der ersten Iteration.
