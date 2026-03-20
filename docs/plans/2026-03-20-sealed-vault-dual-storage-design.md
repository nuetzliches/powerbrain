# Sealed Vault: Dual Storage with Pseudonymization

**Date:** 2026-03-20
**Status:** Approved
**Approach:** Sealed Vault Pattern (Approach A)

## Motivation

The KB currently stores all data with destructive masking (`<PERSON>`, `<EMAIL_ADDRESS>`).
This causes two problems:

1. **Search quality:** Masked placeholders destroy semantic information in embeddings.
   Pseudonymized text (deterministic hashes) preserves sentence structure and produces better vectors.
2. **Original access:** Authorized roles (Support, Legal) sometimes need the real data
   for customer inquiries, contract questions, or legal disclosure requests.

## Decisions

| Decision | Chosen | Reason |
|---|---|---|
| Separation model | Separate PostgreSQL schema (`pii_vault`) with RLS | Clean separation without microservice overhead |
| Dual storage control | OPA policy (`dual_storage_enabled` per classification) | Policy-driven, changeable without code deployment |
| Original access | Parameter on existing tools + `pii_access_token` | No separate tool needed, but explicit elevation |
| Token model | HMAC-signed, short-lived (15 min), purpose-bound | Lightweight, independent of auth infrastructure |

## 1. Architecture & Data Model

### New Schema: `pii_vault`

The vault schema lives in the same PostgreSQL instance and is secured via Row-Level Security (RLS)
and a dedicated DB user (`mcp_vault_reader`).

**Tables:**

```
pii_vault.original_content
  - id (UUID, PK)
  - document_id (FK → documents_meta.id)
  - chunk_index (INT)
  - original_text (TEXT)
  - pii_entities (JSONB)          -- detected entities with type, position, confidence
  - stored_at (TIMESTAMPTZ)
  - retention_expires_at (TIMESTAMPTZ)
  - data_category (VARCHAR)

pii_vault.pseudonym_mapping
  - id (UUID, PK)
  - document_id (FK → documents_meta.id)
  - chunk_index (INT)
  - pseudonym (VARCHAR)           -- 8-char SHA-256 hash
  - entity_type (VARCHAR)         -- PERSON, EMAIL, etc.
  - salt (VARCHAR)                -- project salt reference
  - created_at (TIMESTAMPTZ)

pii_vault.vault_access_log
  - id (UUID, PK)
  - agent_id (VARCHAR)
  - document_id (UUID)
  - purpose (VARCHAR)
  - token_hash (VARCHAR)          -- hash of the token, not the token itself
  - accessed_at (TIMESTAMPTZ)

pii_vault.project_salts
  - project_id (UUID, FK → projects.id)
  - salt (VARCHAR)                -- cryptographically randomly generated
  - created_at (TIMESTAMPTZ)
  - rotated_at (TIMESTAMPTZ)      -- NULL until first rotation
```

**Qdrant Payload Extension:**

Existing Qdrant points get a new field:
- `vault_ref` (UUID, nullable) — Points to `pii_vault.original_content.id`, only set when PII was detected.

### OPA Policy Extension

New rule `dual_storage_enabled` in `kb.privacy`:

| Classification | pii_action    | dual_storage |
|----------------|---------------|--------------|
| public         | mask          | false        |
| internal       | pseudonymize  | true         |
| confidential   | pseudonymize  | true         |
| restricted     | block         | false        |

## 2. Ingestion Flow

```
Data arrives (MCP ingest_data)
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
    │   → Previous flow: mask → embed → Qdrant (no vault)
    │
    └── "pseudonymize" + dual_storage=true
        │
        1. pseudonymize_text(text, project_salt)
           → pseudonymized_text + mapping{original→pseudonym}
        2. Embed pseudonymized_text via Ollama → 768d vector
        3. PostgreSQL transaction:
           - pii_vault.original_content INSERT (original_text, pii_entities)
           - pii_vault.pseudonym_mapping INSERT (one entry per entity)
           - pii_scan_log INSERT (action_taken="pseudonymize")
        4. Qdrant upsert:
           - text = pseudonymized_text
           - embedding = vector
           - vault_ref = UUID from step 3
           - contains_pii = true
        5. On Qdrant error: mark vault entry as "orphaned"
```

**Salt Management:** Each project gets its own salt in `pii_vault.project_salts`.
Same name + same salt = same pseudonym (consistent within a project),
but not correlatable across projects.

**Bug Fix:** The existing `pseudonymize_text()` has a bug — when multiple entities
of the same type occur, only the last pseudonym is used for all of them. This will be fixed:
one individual mapping entry with individual pseudonymization per entity.

**Transactionality:** Write to vault first (PostgreSQL transaction), then Qdrant.
No distributed transaction. A cleanup job removes orphaned vault entries.

## 3. Access and Token Mechanism

### Standard Search Path (without token)

Unchanged: Qdrant search → OPA access check → Reranker → pseudonymized text.
`vault_ref` is **not** returned.

### Elevated Access (with token)

```
pii_access_token = {
    "agent_id": "support-agent-42",
    "role": "analyst",
    "purpose": "support",
    "scope": ["document_id_xyz"],       # Optional: restriction
    "issued_at": "2026-03-20T10:00:00Z",
    "expires_at": "2026-03-20T10:15:00Z",  # 15 min
    "issued_by": "admin"
}
```

HMAC-signed (shared secret). No JWT/OAuth required.

**Validation chain:**

1. Verify token signature (HMAC)
2. Check token expiration
3. OPA `kb.privacy.vault_access`:
   - Purpose in `allowed_purposes` of the data_category?
   - Role has access to this classification?
4. Field redaction via OPA `kb.privacy.fields_to_redact`:
   - e.g. purpose=reporting → redact: email, iban, birthdate
   - Agent only sees fields permitted for the purpose
5. `vault_access_log` INSERT (agent_id, document_id, purpose, token_hash)
6. Result with (partially redacted) original text

**Data minimization:** Even with original access, OPA provides `fields_to_redact`.
Not all PII fields are disclosed — only those permitted for the purpose.

## 4. Art. 17 Deletion & Retention

### Deletion Strategy (2 Levels)

**Level 1: Restrict (Art. 18 — retention obligation exists)**

- `pii_vault.original_content` → deleted
- `pii_vault.pseudonym_mapping` → deleted
- Qdrant points remain (pseudonymized text)
- Pseudonym is now irreversible = de facto anonymous
- `contains_pii` → set to false
- `deletion_requests.status` → "completed"

Advantage: Search index is preserved, but data is no longer personally identifiable.

**Level 2: Delete (no retention obligation)**

- Everything from Level 1, plus:
- Delete Qdrant points
- Delete `dataset_rows` with PII reference
- Anonymize `agent_access_log` (data_subject_ref → NULL)

### Retention Cleanup Extension

The existing `retention_cleanup.py` is extended:

1. Query: `pii_vault.original_content WHERE retention_expires_at < NOW()`
2. Delete mapping, delete original
3. Qdrant payload: `vault_ref → NULL`
4. Log in `pii_scan_log`

**Invariant:** Vault entries always have a shorter or equal retention
compared to the associated Qdrant points. The original never expires later than the
pseudonymized version.

## Existing Gaps Closed by This Implementation

This implementation closes several of the gaps documented in `docs/bekannte-schwachstellen.md`:

| Gap | How closed |
|---|---|
| OPA `kb.privacy.pii_action` is never called | Ingestion calls OPA policy |
| `pseudonymize_text()` is never called | Dual storage path uses pseudonymization |
| `pii_scan_log` is never written to | Ingestion logs every scan |
| `data_subjects` is never populated | Pseudonym mapping references data_subjects |
| `datasets.pseudonymized` is never set | Ingestion sets flag correctly |
| `fields_to_redact` is never applied | Vault access applies field redaction |
| Bug in `pseudonymize_text()` (entity overwriting) | Fix: individual pseudonyms per entity |

## Not in Scope

- **Authentication (P1-1):** Token mechanism is independent of auth.
  Real auth comes separately.
- **SQL/Cypher Injection Fixes (P1-2, P1-3):** Separate work.
- **Encrypt-and-Store:** The OPA policy provides for it, but will be switched to pseudonymization
  for `confidential`. Encryption can be added later.
- **Key Rotation for Salts:** Schema supports `rotated_at`, but rotation logic
  is not part of the first iteration.
