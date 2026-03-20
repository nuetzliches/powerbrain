# Sealed Vault: Dual Storage Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Implement the Sealed Vault pattern — store PII data both pseudonymized (for search/embeddings) and as originals (in a secured vault), with policy-driven access control and GDPR-compliant deletion.

**Architecture:** New PostgreSQL schema `pii_vault` with RLS stores original PII data. Qdrant stores only pseudonymized text. OPA policies control which classifications get dual storage and who can access originals via short-lived HMAC tokens. Deletion of vault mapping renders pseudonyms irreversible.

**Tech Stack:** PostgreSQL 16 (RLS, schemas), OPA/Rego, Python 3.12+ (asyncpg, httpx, hmac), Presidio, Qdrant

**Design Doc:** `docs/plans/2026-03-20-sealed-vault-dual-storage-design.md`

---

## Task 1: SQL Migration — pii_vault Schema

**Files:**
- Create: `init-db/007_pii_vault.sql`
- Test: `tests/test_pii_vault_schema.py`

**Step 1: Write the schema test**

```python
# tests/test_pii_vault_schema.py
"""Validates pii_vault SQL migration structure."""
import unittest
import pathlib

SQL_FILE = pathlib.Path(__file__).resolve().parent.parent / "init-db" / "007_pii_vault.sql"


class TestPiiVaultSchema(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.sql = SQL_FILE.read_text(encoding="utf-8").lower()

    def test_file_exists(self):
        self.assertTrue(SQL_FILE.exists(), "007_pii_vault.sql must exist")

    def test_creates_schema(self):
        self.assertIn("create schema", self.sql)
        self.assertIn("pii_vault", self.sql)

    def test_original_content_table(self):
        self.assertIn("pii_vault.original_content", self.sql)
        self.assertIn("original_text", self.sql)
        self.assertIn("pii_entities", self.sql)
        self.assertIn("retention_expires_at", self.sql)

    def test_pseudonym_mapping_table(self):
        self.assertIn("pii_vault.pseudonym_mapping", self.sql)
        self.assertIn("entity_type", self.sql)
        self.assertIn("salt", self.sql)

    def test_vault_access_log_table(self):
        self.assertIn("pii_vault.vault_access_log", self.sql)
        self.assertIn("token_hash", self.sql)
        self.assertIn("purpose", self.sql)

    def test_project_salts_table(self):
        self.assertIn("pii_vault.project_salts", self.sql)

    def test_rls_enabled(self):
        self.assertIn("row level security", self.sql)

    def test_vault_reader_role(self):
        self.assertIn("mcp_vault_reader", self.sql)


if __name__ == "__main__":
    unittest.main()
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_pii_vault_schema.py -v`
Expected: FAIL — file does not exist

**Step 3: Write the SQL migration**

```sql
-- init-db/007_pii_vault.sql
-- Sealed Vault: Secure storage of original PII data
-- Separate schema with Row-Level Security

-- ── Schema + Role ─────────────────────────────────────────
CREATE SCHEMA IF NOT EXISTS pii_vault;

-- DB role for vault access (only the MCP server may assume this role)
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'mcp_vault_reader') THEN
        CREATE ROLE mcp_vault_reader NOLOGIN;
    END IF;
END
$$;

-- Grant access to kb_admin (the application user)
GRANT USAGE ON SCHEMA pii_vault TO mcp_vault_reader;

-- ── Tables ───────────────────────────────────────────────

-- Original content (plain text + detected PII entities)
CREATE TABLE pii_vault.original_content (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id     UUID NOT NULL REFERENCES documents_meta(id) ON DELETE CASCADE,
    chunk_index     INT NOT NULL,
    original_text   TEXT NOT NULL,
    pii_entities    JSONB NOT NULL DEFAULT '[]',
    stored_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    retention_expires_at TIMESTAMPTZ NOT NULL,
    data_category   VARCHAR(50) REFERENCES data_categories(id),
    UNIQUE (document_id, chunk_index)
);

CREATE INDEX idx_vault_content_retention
    ON pii_vault.original_content(retention_expires_at);
CREATE INDEX idx_vault_content_document
    ON pii_vault.original_content(document_id);

-- Pseudonym mapping (for traceability + Art. 17 GDPR deletion)
CREATE TABLE pii_vault.pseudonym_mapping (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id     UUID NOT NULL REFERENCES documents_meta(id) ON DELETE CASCADE,
    chunk_index     INT NOT NULL,
    pseudonym       VARCHAR(20) NOT NULL,
    entity_type     VARCHAR(50) NOT NULL,
    salt            VARCHAR(100) NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_vault_mapping_document
    ON pii_vault.pseudonym_mapping(document_id);
CREATE INDEX idx_vault_mapping_pseudonym
    ON pii_vault.pseudonym_mapping(pseudonym);

-- Separate audit log only for vault access
CREATE TABLE pii_vault.vault_access_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id        VARCHAR(200) NOT NULL,
    document_id     UUID NOT NULL,
    chunk_index     INT,
    purpose         VARCHAR(100) NOT NULL,
    token_hash      VARCHAR(64) NOT NULL,
    accessed_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_vault_access_agent
    ON pii_vault.vault_access_log(agent_id, accessed_at);
CREATE INDEX idx_vault_access_document
    ON pii_vault.vault_access_log(document_id);

-- Project salts (deterministic per project)
CREATE TABLE pii_vault.project_salts (
    project_id      VARCHAR(100) PRIMARY KEY REFERENCES projects(id),
    salt            VARCHAR(200) NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    rotated_at      TIMESTAMPTZ
);

-- ── Row-Level Security ─────────────────────────────────────

ALTER TABLE pii_vault.original_content ENABLE ROW LEVEL SECURITY;
ALTER TABLE pii_vault.pseudonym_mapping ENABLE ROW LEVEL SECURITY;
ALTER TABLE pii_vault.vault_access_log  ENABLE ROW LEVEL SECURITY;
ALTER TABLE pii_vault.project_salts     ENABLE ROW LEVEL SECURITY;

-- Policy: Only mcp_vault_reader and the DB owner (kb_admin) may read
CREATE POLICY vault_content_read ON pii_vault.original_content
    FOR SELECT TO mcp_vault_reader USING (true);

CREATE POLICY vault_content_insert ON pii_vault.original_content
    FOR INSERT TO mcp_vault_reader WITH CHECK (true);

CREATE POLICY vault_mapping_read ON pii_vault.pseudonym_mapping
    FOR SELECT TO mcp_vault_reader USING (true);

CREATE POLICY vault_mapping_insert ON pii_vault.pseudonym_mapping
    FOR INSERT TO mcp_vault_reader WITH CHECK (true);

CREATE POLICY vault_mapping_delete ON pii_vault.pseudonym_mapping
    FOR DELETE TO mcp_vault_reader USING (true);

CREATE POLICY vault_content_delete ON pii_vault.original_content
    FOR DELETE TO mcp_vault_reader USING (true);

CREATE POLICY vault_access_log_insert ON pii_vault.vault_access_log
    FOR INSERT TO mcp_vault_reader WITH CHECK (true);

CREATE POLICY vault_access_log_read ON pii_vault.vault_access_log
    FOR SELECT TO mcp_vault_reader USING (true);

CREATE POLICY vault_salts_all ON pii_vault.project_salts
    TO mcp_vault_reader USING (true) WITH CHECK (true);

-- Grant permissions
GRANT SELECT, INSERT, DELETE ON pii_vault.original_content TO mcp_vault_reader;
GRANT SELECT, INSERT, DELETE ON pii_vault.pseudonym_mapping TO mcp_vault_reader;
GRANT SELECT, INSERT ON pii_vault.vault_access_log TO mcp_vault_reader;
GRANT ALL ON pii_vault.project_salts TO mcp_vault_reader;

-- ── View: Orphaned vault entries ─────────────────────────
CREATE OR REPLACE VIEW pii_vault.v_orphaned_content AS
SELECT oc.id, oc.document_id, oc.chunk_index, oc.stored_at
FROM pii_vault.original_content oc
LEFT JOIN documents_meta dm ON dm.id = oc.document_id
WHERE dm.id IS NULL;

-- ── View: Expired vault entries ───────────────────────
CREATE OR REPLACE VIEW pii_vault.v_expired_vault_content AS
SELECT id, document_id, chunk_index, retention_expires_at, data_category
FROM pii_vault.original_content
WHERE retention_expires_at <= now();
```

**Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_pii_vault_schema.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add init-db/007_pii_vault.sql tests/test_pii_vault_schema.py
git commit -m "feat: add pii_vault schema migration with RLS"
```

---

## Task 2: OPA Policy Extensions — dual_storage + vault_access

**Files:**
- Modify: `opa-policies/kb/privacy.rego` (append after line 146)
- Test: `tests/test_opa_privacy_extensions.py`

**Step 1: Write the policy test**

```python
# tests/test_opa_privacy_extensions.py
"""Validates OPA privacy.rego extensions for dual storage and vault access."""
import unittest
import pathlib

REGO_FILE = pathlib.Path(__file__).resolve().parent.parent / "opa-policies" / "kb" / "privacy.rego"


class TestOpaPrivacyExtensions(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.rego = REGO_FILE.read_text(encoding="utf-8")

    def test_dual_storage_default(self):
        self.assertIn("default dual_storage_enabled := false", self.rego)

    def test_dual_storage_rules_exist(self):
        self.assertIn("dual_storage_enabled", self.rego)
        # Should reference classification levels
        self.assertIn('"internal"', self.rego)

    def test_vault_access_default(self):
        self.assertIn("default vault_access_allowed := false", self.rego)

    def test_vault_access_checks_purpose(self):
        # Must validate purpose against allowed_purposes
        self.assertIn("vault_access_allowed", self.rego)
        self.assertIn("token_valid", self.rego)

    def test_vault_fields_to_redact(self):
        # Must have field redaction for vault access
        self.assertIn("vault_fields_to_redact", self.rego)


if __name__ == "__main__":
    unittest.main()
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_opa_privacy_extensions.py -v`
Expected: FAIL — rules not found in rego file

**Step 3: Add policy rules to privacy.rego**

Append the following after line 146 (after the existing `deletion_response` rule) in `opa-policies/kb/privacy.rego`:

```rego
# ── Dual Storage Policy ─────────────────────────────────────
# Determines per classification whether original + pseudonym are stored.
# Changeable without code deployment.

default dual_storage_enabled := false

dual_storage_enabled if {
    input.classification == "internal"
    input.contains_pii == true
}

dual_storage_enabled if {
    input.classification == "confidential"
    input.contains_pii == true
}

# ── Vault Access Policy ─────────────────────────────────────
# Checks whether an agent is allowed to access original data in the vault.
# Requires valid token + purpose binding.

default vault_access_allowed := false

vault_access_allowed if {
    input.token_valid == true
    input.token_expired == false
    purpose_allowed_for_vault
    role_allowed_for_classification
}

purpose_allowed_for_vault if {
    some allowed_purpose in allowed_purposes[input.data_category]
    input.purpose == allowed_purpose
}

role_allowed_for_classification if {
    input.classification == "internal"
    input.agent_role in {"analyst", "admin", "developer"}
}

role_allowed_for_classification if {
    input.classification == "confidential"
    input.agent_role == "admin"
}

# ── Vault Field Redaction ───────────────────────────────────
# Which fields in the original are redacted, depending on the purpose.
# Uses the same logic as fields_to_redact, but explicitly for the vault.

default vault_fields_to_redact := set()

vault_fields_to_redact := {"email", "phone", "iban", "birthdate", "address"} if {
    input.purpose == "reporting"
}

vault_fields_to_redact := {"iban", "birthdate", "address"} if {
    input.purpose == "support"
}

vault_fields_to_redact := {"email", "phone", "iban", "birthdate", "address"} if {
    input.purpose == "product_improvement"
}

vault_fields_to_redact := {"birthdate"} if {
    input.purpose == "billing"
}

vault_fields_to_redact := set() if {
    input.purpose == "contract_fulfillment"
}
```

**Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_opa_privacy_extensions.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add opa-policies/kb/privacy.rego tests/test_opa_privacy_extensions.py
git commit -m "feat: add OPA policies for dual storage and vault access control"
```

---

## Task 3: Fix pseudonymize_text() Bug

**Files:**
- Modify: `ingestion/pii_scanner.py:188-217`
- Test: `tests/test_pseudonymize_fix.py`

**Step 1: Write the failing test**

```python
# tests/test_pseudonymize_fix.py
"""Tests that pseudonymize_text handles multiple entities of the same type correctly."""
import unittest
import sys
import pathlib

# Add ingestion dir to path
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "ingestion"))


class TestPseudonymizeFix(unittest.TestCase):
    """Test pseudonymize_text produces unique pseudonyms per entity."""

    def setUp(self):
        """Create scanner — requires presidio + spacy model installed."""
        try:
            from pii_scanner import PIIScanner
            self.scanner = PIIScanner()
            self.available = True
        except Exception:
            self.available = False

    @unittest.skipUnless(
        # Only run if presidio is installed
        True,
        "Requires presidio + spacy model"
    )
    def test_two_persons_get_different_pseudonyms(self):
        if not self.available:
            self.skipTest("Presidio not available")
        text = "Max Mustermann traf Anna Schmidt im Büro."
        salt = "test-salt"
        result = self.scanner.pseudonymize_text(text, salt)
        # The two names should NOT be replaced with the same pseudonym
        # Count unique pseudonym-like patterns (8 hex chars)
        import re
        pseudonyms = re.findall(r'\b[0-9a-f]{8}\b', result)
        if len(pseudonyms) >= 2:
            self.assertNotEqual(
                pseudonyms[0], pseudonyms[1],
                f"Two different persons got the same pseudonym: {result}"
            )

    def test_pseudonymize_text_returns_mapping(self):
        """After fix, pseudonymize_text should return (text, mapping) tuple."""
        if not self.available:
            self.skipTest("Presidio not available")
        text = "Max Mustermann hat die Email max@example.com"
        salt = "test-salt"
        result = self.scanner.pseudonymize_text(text, salt)
        # After fix: returns tuple (pseudonymized_text, mapping_dict)
        self.assertIsInstance(result, tuple, "pseudonymize_text should return (text, mapping) tuple")
        pseudo_text, mapping = result
        self.assertIsInstance(pseudo_text, str)
        self.assertIsInstance(mapping, dict)
        self.assertGreater(len(mapping), 0, "Mapping should contain at least one entry")


if __name__ == "__main__":
    unittest.main()
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_pseudonymize_fix.py -v`
Expected: FAIL — `pseudonymize_text` returns `str` not `tuple`

**Step 3: Fix pseudonymize_text in pii_scanner.py**

Replace lines 188-217 in `ingestion/pii_scanner.py`:

```python
    def pseudonymize_text(
        self, text: str, salt: str, language: str = "de"
    ) -> tuple[str, dict[str, str]]:
        """
        Replaces PII with deterministic pseudonyms.
        Same input + salt → same pseudonym (for linkability).

        Returns:
            Tuple of (pseudonymized text, mapping {original → pseudonym})
        """
        results = self.analyzer.analyze(
            text=text,
            language=language,
            entities=PII_ENTITY_TYPES,
            score_threshold=MIN_CONFIDENCE,
        )

        def make_pseudonym(entity_text: str) -> str:
            h = hashlib.sha256(f"{salt}:{entity_text}".encode()).hexdigest()[:8]
            return h

        # Build individual operators per result (not per entity type),
        # so that multiple entities of the same type get different pseudonyms.
        mapping: dict[str, str] = {}
        for r in results:
            original = text[r.start:r.end]
            pseudo = make_pseudonym(original)
            mapping[original] = pseudo

        # Presidio's anonymizer requires operators per entity type.
        # With multiple entities of the same type: replace manually instead of using Presidio.
        pseudonymized = text
        # Sort by position descending so that offsets remain stable
        for r in sorted(results, key=lambda x: x.start, reverse=True):
            original = pseudonymized[r.start:r.end]
            pseudo = mapping.get(original, make_pseudonym(original))
            pseudonymized = pseudonymized[:r.start] + pseudo + pseudonymized[r.end:]

        return pseudonymized, mapping
```

Also update `pseudonymize_record` (lines 229-255) to match the new return type:

```python
    def pseudonymize_record(
        self, record: dict[str, Any], salt: str, language: str = "de"
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """
        Pseudonymizes PII in a record.
        Returns (pseudonymized record, mapping original→pseudonym).
        """
        pseudonymized = {}
        mapping: dict[str, str] = {}

        for key, value in record.items():
            if isinstance(value, str) and value.strip():
                scan = self.scan_text(value, language)
                if scan.contains_pii:
                    pseudo_text, text_mapping = self.pseudonymize_text(
                        value, salt, language
                    )
                    pseudonymized[key] = pseudo_text
                    mapping.update(text_mapping)
                else:
                    pseudonymized[key] = value
            else:
                pseudonymized[key] = value

        return pseudonymized, mapping
```

**Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_pseudonymize_fix.py -v`
Expected: PASS (at least `test_pseudonymize_text_returns_mapping`; person-detection test depends on spaCy model)

**Step 5: Commit**

```bash
git add ingestion/pii_scanner.py tests/test_pseudonymize_fix.py
git commit -m "fix: pseudonymize_text returns mapping, handles multiple same-type entities"
```

---

## Task 4: Ingestion Pipeline — OPA Integration + Dual Storage Path

**Files:**
- Modify: `ingestion/ingestion_api.py:121-194`
- Test: `tests/test_ingestion_dual_storage.py`

**Step 1: Write the failing test**

```python
# tests/test_ingestion_dual_storage.py
"""Validates ingestion_api.py has dual storage path wired to OPA."""
import unittest
import pathlib

API_FILE = pathlib.Path(__file__).resolve().parent.parent / "ingestion" / "ingestion_api.py"


class TestIngestionDualStorage(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.code = API_FILE.read_text(encoding="utf-8")

    def test_calls_opa_pii_action(self):
        """Ingestion must call OPA to determine PII action."""
        self.assertIn("kb/privacy/pii_action", self.code)

    def test_calls_opa_dual_storage(self):
        """Ingestion must check dual_storage_enabled policy."""
        self.assertIn("dual_storage_enabled", self.code)

    def test_vault_insert(self):
        """Ingestion must write to pii_vault.original_content."""
        self.assertIn("pii_vault.original_content", self.code)

    def test_mapping_insert(self):
        """Ingestion must write to pii_vault.pseudonym_mapping."""
        self.assertIn("pii_vault.pseudonym_mapping", self.code)

    def test_scan_log_insert(self):
        """Ingestion must write to pii_scan_log."""
        self.assertIn("pii_scan_log", self.code)

    def test_vault_ref_in_payload(self):
        """Qdrant payload must include vault_ref."""
        self.assertIn("vault_ref", self.code)

    def test_pseudonymize_called(self):
        """Must call pseudonymize_text, not just mask_text."""
        self.assertIn("pseudonymize_text", self.code)

    def test_project_salt_lookup(self):
        """Must look up or create project salt."""
        self.assertIn("project_salts", self.code)


if __name__ == "__main__":
    unittest.main()
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_ingestion_dual_storage.py -v`
Expected: FAIL — none of these strings exist in current code

**Step 3: Rewrite ingest_text_chunks in ingestion_api.py**

Replace the `ingest_text_chunks` function (lines 121-194) in `ingestion/ingestion_api.py` with:

```python
async def get_or_create_project_salt(project: str | None) -> str:
    """Retrieves or creates a salt for the project from pii_vault.project_salts."""
    import secrets
    if not pg_pool or not project:
        return secrets.token_hex(16)

    row = await pg_pool.fetchrow(
        "SELECT salt FROM pii_vault.project_salts WHERE project_id = $1",
        project,
    )
    if row:
        return row["salt"]

    salt = secrets.token_hex(16)
    try:
        await pg_pool.execute(
            """INSERT INTO pii_vault.project_salts (project_id, salt)
               VALUES ($1, $2)
               ON CONFLICT (project_id) DO NOTHING""",
            project, salt,
        )
    except Exception as e:
        log.warning(f"Project salt creation failed: {e}")
    return salt


async def check_opa_privacy(
    classification: str, contains_pii: bool, legal_basis: str | None = None
) -> dict:
    """Queries OPA for pii_action and dual_storage_enabled."""
    input_data = {
        "classification": classification,
        "contains_pii": contains_pii,
        "legal_basis": legal_basis or "",
    }
    result = {"pii_action": "block", "dual_storage_enabled": False}
    try:
        resp = await http.post(
            f"{OPA_URL}/v1/data/kb/privacy",
            json={"input": input_data},
        )
        resp.raise_for_status()
        data = resp.json().get("result", {})
        result["pii_action"] = data.get("pii_action", "block")
        result["dual_storage_enabled"] = data.get("dual_storage_enabled", False)
        result["retention_days"] = data.get("retention_days", 365)
    except Exception as e:
        log.warning(f"OPA privacy check failed, defaulting to block: {e}")
    return result


async def store_in_vault(
    doc_id: str,
    chunk_index: int,
    original_text: str,
    pii_entities: list[dict],
    mapping: dict[str, str],
    salt: str,
    retention_days: int,
    data_category: str | None,
) -> str:
    """Stores original text + mapping in pii_vault. Returns vault_ref UUID."""
    vault_id = str(uuid.uuid4())
    expires_at = datetime.now(timezone.utc) + timedelta(days=retention_days)

    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            # Store original
            await conn.execute("""
                INSERT INTO pii_vault.original_content
                    (id, document_id, chunk_index, original_text,
                     pii_entities, retention_expires_at, data_category)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
            """, vault_id, doc_id, chunk_index, original_text,
                json.dumps(pii_entities), expires_at, data_category)

            # Store mapping (one entry per entity)
            for original, pseudonym in mapping.items():
                entity_type = "UNKNOWN"
                for e in pii_entities:
                    if e.get("text") == original:
                        entity_type = e.get("type", "UNKNOWN")
                        break
                await conn.execute("""
                    INSERT INTO pii_vault.pseudonym_mapping
                        (document_id, chunk_index, pseudonym,
                         entity_type, salt)
                    VALUES ($1, $2, $3, $4, $5)
                """, doc_id, chunk_index, pseudonym, entity_type, salt)

    return vault_id


async def log_pii_scan(
    source: str,
    entities_found: dict,
    action_taken: str,
    classification: str,
    dataset_id: str | None = None,
):
    """Writes an entry to pii_scan_log."""
    if not pg_pool:
        return
    try:
        await pg_pool.execute("""
            INSERT INTO pii_scan_log
                (source, entities_found, action_taken, classification, dataset_id)
            VALUES ($1, $2, $3, $4, $5)
        """, source, json.dumps(entities_found), action_taken,
            classification, dataset_id)
    except Exception as e:
        log.warning(f"pii_scan_log insert failed: {e}")


async def ingest_text_chunks(
    chunks: list[str],
    collection: str,
    source: str,
    classification: str,
    project: str | None,
    metadata: dict[str, Any],
) -> dict:
    """Vectorizes chunks and stores them in Qdrant + PostgreSQL.

    Pipeline:
    1. PII scan of each chunk
    2. OPA policy: pii_action + dual_storage_enabled
    3. Depending on action: mask, pseudonymize+vault, or block
    4. Embed + Qdrant upsert
    5. PostgreSQL metadata
    """
    scanner = get_scanner()
    points = []
    pii_detected = False
    vault_refs: list[str | None] = []
    doc_id = str(uuid.uuid4())

    for i, chunk in enumerate(chunks):
        # 1. PII scan
        scan_result = scanner.scan_text(chunk)
        vault_ref = None

        if scan_result.contains_pii:
            pii_detected = True

            # 2. OPA Policy: What to do with PII?
            opa_result = await check_opa_privacy(
                classification, True, metadata.get("legal_basis")
            )
            pii_action = opa_result["pii_action"]
            dual_storage = opa_result["dual_storage_enabled"]
            retention_days = opa_result.get("retention_days", 365)

            if pii_action == "block":
                log.warning(
                    f"PII detected in chunk {i}, classification '{classification}'"
                    f" → blocked by OPA policy"
                )
                await log_pii_scan(
                    source, scan_result.entity_counts, "block", classification
                )
                return {
                    "status": "blocked",
                    "reason": f"PII in {classification} data blocked by policy",
                    "chunks_ingested": 0,
                    "pii_detected": True,
                }

            elif pii_action == "pseudonymize" and dual_storage:
                # 3a. Dual Storage: pseudonymize + store original in vault
                log.info(
                    f"PII in chunk {i}: {scan_result.entity_counts}"
                    f" → pseudonymizing (dual storage)"
                )
                salt = await get_or_create_project_salt(project)
                pseudo_text, mapping = scanner.pseudonymize_text(chunk, salt)

                # Vault: store original + mapping
                pii_entities = [
                    {
                        "type": loc["type"],
                        "text": chunk[loc["start"]:loc["end"]],
                        "start": loc["start"],
                        "end": loc["end"],
                        "score": loc["score"],
                    }
                    for loc in scan_result.entity_locations
                ]

                if pg_pool:
                    vault_ref = await store_in_vault(
                        doc_id, i, chunk, pii_entities, mapping,
                        salt, retention_days,
                        metadata.get("data_category"),
                    )

                chunk = pseudo_text
                await log_pii_scan(
                    source, scan_result.entity_counts,
                    "pseudonymize", classification,
                )

            else:
                # 3b. Fallback: mask (public or dual_storage=false)
                log.warning(
                    f"PII in chunk {i}: {scan_result.entity_counts} → masking"
                )
                chunk = scanner.mask_text(chunk)
                await log_pii_scan(
                    source, scan_result.entity_counts, "mask", classification
                )

        # 4. Embedding
        embedding = await get_embedding(chunk)

        point_id = str(uuid.uuid4())
        payload = {
            "text": chunk,
            "source": source,
            "classification": classification,
            "project": project or "",
            "chunk_index": i,
            "ingested_at": datetime.now(timezone.utc).isoformat(),
            "contains_pii": scan_result.contains_pii,
            "vault_ref": vault_ref,
            **metadata,
        }
        points.append(PointStruct(
            id=point_id, vector=embedding, payload=payload,
        ))
        vault_refs.append(vault_ref)

    # 5. Upsert into Qdrant
    if points:
        await qdrant.upsert(collection_name=collection, points=points)
        log.info(f"{len(points)} points inserted into '{collection}'")

    # 6. Store metadata in PostgreSQL
    if pg_pool:
        try:
            await pg_pool.execute("""
                INSERT INTO documents_meta
                    (id, title, source, qdrant_collection, classification,
                     chunk_count, contains_pii, metadata)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
                doc_id,
                source[:200],
                source,
                collection,
                classification,
                len(points),
                pii_detected,
                json.dumps({
                    **metadata,
                    "pii_detected": pii_detected,
                    "vault_refs": [v for v in vault_refs if v],
                }),
            )
        except Exception as e:
            log.error(f"PG documents_meta insert failed: {e}")

    return {
        "status": "ok",
        "collection": collection,
        "chunks_ingested": len(points),
        "pii_detected": pii_detected,
        "dual_storage": any(v is not None for v in vault_refs),
    }
```

Also add `from datetime import timedelta` to the imports at the top of the file if not already present.

**Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_ingestion_dual_storage.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add ingestion/ingestion_api.py tests/test_ingestion_dual_storage.py
git commit -m "feat: wire OPA privacy policy into ingestion, add dual storage vault path"
```

---

## Task 5: MCP Server — Token Validation + Vault Lookup

**Files:**
- Modify: `mcp-server/server.py` (add functions, modify search_knowledge handler)
- Test: `tests/test_mcp_vault_access.py`

**Step 1: Write the failing test**

```python
# tests/test_mcp_vault_access.py
"""Validates MCP server has vault access token validation and lookup."""
import unittest
import pathlib

SERVER_FILE = pathlib.Path(__file__).resolve().parent.parent / "mcp-server" / "server.py"


class TestMcpVaultAccess(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.code = SERVER_FILE.read_text(encoding="utf-8")

    def test_validate_pii_access_token_function(self):
        self.assertIn("def validate_pii_access_token", self.code)

    def test_vault_lookup_function(self):
        self.assertIn("def vault_lookup", self.code)

    def test_check_opa_vault_access(self):
        """Must call OPA vault_access_allowed policy."""
        self.assertIn("vault_access_allowed", self.code)

    def test_vault_access_log(self):
        """Must log vault access separately."""
        self.assertIn("vault_access_log", self.code)

    def test_search_knowledge_handles_token(self):
        """search_knowledge must accept pii_access_token parameter."""
        self.assertIn("pii_access_token", self.code)

    def test_hmac_validation(self):
        """Token validation must use HMAC."""
        self.assertIn("hmac", self.code)

    def test_redact_fields(self):
        """Must have field redaction function for vault results."""
        self.assertIn("def redact_fields", self.code)


if __name__ == "__main__":
    unittest.main()
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_mcp_vault_access.py -v`
Expected: FAIL

**Step 3: Add vault access functions to server.py**

Add these functions after `log_access()` (after line 204) in `mcp-server/server.py`:

```python
# ── Vault Access ────────────────────────────────────────────

VAULT_HMAC_SECRET = os.getenv("VAULT_HMAC_SECRET", "change-me-in-production")


def validate_pii_access_token(token: dict) -> dict:
    """
    Validates a PII access token (HMAC-signed, short-lived).
    Returns: {"valid": bool, "reason": str, "payload": dict}
    """
    import hmac as hmac_mod
    import hashlib
    from datetime import datetime, timezone

    signature = token.get("signature", "")
    payload = {k: v for k, v in token.items() if k != "signature"}

    # Verify HMAC signature
    expected = hmac_mod.new(
        VAULT_HMAC_SECRET.encode(),
        json.dumps(payload, sort_keys=True).encode(),
        hashlib.sha256,
    ).hexdigest()

    if not hmac_mod.compare_digest(signature, expected):
        return {"valid": False, "reason": "Invalid token signature", "payload": payload}

    # Check expiration
    expires_at = token.get("expires_at", "")
    try:
        exp = datetime.fromisoformat(expires_at)
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > exp:
            return {"valid": False, "reason": "Token expired", "payload": payload}
    except (ValueError, TypeError):
        return {"valid": False, "reason": "Invalid expires_at format", "payload": payload}

    return {"valid": True, "reason": "ok", "payload": payload}


async def check_opa_vault_access(
    agent_role: str, purpose: str, classification: str,
    data_category: str, token_valid: bool, token_expired: bool,
) -> dict:
    """Checks via OPA whether vault access is allowed."""
    input_data = {
        "agent_role": agent_role,
        "purpose": purpose,
        "classification": classification,
        "data_category": data_category,
        "token_valid": token_valid,
        "token_expired": token_expired,
    }
    try:
        resp = await http.post(
            f"{OPA_URL}/v1/data/kb/privacy/vault_access_allowed",
            json={"input": input_data},
        )
        resp.raise_for_status()
        allowed = resp.json().get("result", False)
        fields_resp = await http.post(
            f"{OPA_URL}/v1/data/kb/privacy/vault_fields_to_redact",
            json={"input": input_data},
        )
        fields_resp.raise_for_status()
        fields_to_redact = list(fields_resp.json().get("result", []))
    except Exception as e:
        log.warning(f"OPA vault access check failed: {e}")
        allowed = False
        fields_to_redact = []
    return {
        "allowed": allowed,
        "fields_to_redact": fields_to_redact,
    }


def redact_fields(text: str, pii_entities: list[dict], fields_to_redact: set[str]) -> str:
    """Redacts specific PII entity types in text based on OPA policy."""
    # Mapping from OPA field names to Presidio entity types
    field_to_entity = {
        "email": "EMAIL_ADDRESS",
        "phone": "PHONE_NUMBER",
        "iban": "IBAN_CODE",
        "birthdate": "DATE_OF_BIRTH",
        "address": "LOCATION",
    }
    entities_to_redact = {
        field_to_entity[f] for f in fields_to_redact if f in field_to_entity
    }

    if not entities_to_redact:
        return text

    # Sort by position descending for stable offsets
    sorted_entities = sorted(pii_entities, key=lambda e: e.get("start", 0), reverse=True)
    result = text
    for entity in sorted_entities:
        if entity.get("type") in entities_to_redact:
            start = entity.get("start", 0)
            end = entity.get("end", 0)
            if 0 <= start < end <= len(result):
                result = result[:start] + f"<{entity['type']}>" + result[end:]
    return result


async def vault_lookup(
    document_id: str, chunk_indices: list[int] | None = None
) -> list[dict]:
    """Retrieves original data from the vault."""
    pool = await get_pg_pool()
    if chunk_indices:
        rows = await pool.fetch("""
            SELECT id, chunk_index, original_text, pii_entities
            FROM pii_vault.original_content
            WHERE document_id = $1 AND chunk_index = ANY($2)
            ORDER BY chunk_index
        """, document_id, chunk_indices)
    else:
        rows = await pool.fetch("""
            SELECT id, chunk_index, original_text, pii_entities
            FROM pii_vault.original_content
            WHERE document_id = $1
            ORDER BY chunk_index
        """, document_id)
    return [
        {
            "vault_id": str(r["id"]),
            "chunk_index": r["chunk_index"],
            "original_text": r["original_text"],
            "pii_entities": json.loads(r["pii_entities"])
                if isinstance(r["pii_entities"], str)
                else r["pii_entities"],
        }
        for r in rows
    ]


async def log_vault_access(
    agent_id: str, document_id: str, chunk_index: int | None,
    purpose: str, token_hash: str,
):
    """Logs vault access to a separate audit log."""
    pool = await get_pg_pool()
    await pool.execute("""
        INSERT INTO pii_vault.vault_access_log
            (agent_id, document_id, chunk_index, purpose, token_hash)
        VALUES ($1, $2, $3, $4, $5)
    """, agent_id, document_id, chunk_index, purpose, token_hash)
```

Then modify the `search_knowledge` handler (lines 501-544) to accept and handle the token. The key change is adding `pii_access_token` to the tool definition in `list_tools()` and handling it in the search handler:

In `list_tools()`, find the `search_knowledge` tool definition and add the optional `pii_access_token` property to its input schema:

```python
# Add to search_knowledge tool input schema properties:
"pii_access_token": {
    "type": "object",
    "description": "Optional: HMAC-signed token for accessing original PII data from vault",
},
"purpose": {
    "type": "string",
    "description": "Required with pii_access_token: purpose for accessing PII data",
},
```

Modify the search_knowledge handler (lines 501-544) to check for the token and resolve vault refs:

```python
    if name == "search_knowledge":
        collection = arguments.get("collection", "knowledge_general")
        query      = arguments["query"]
        top_k      = arguments.get("top_k", DEFAULT_TOP_K)
        filters    = arguments.get("filters", {})
        pii_token  = arguments.get("pii_access_token")
        purpose    = arguments.get("purpose", "")

        vector = await embed_text(query)

        must_conditions = [
            FieldCondition(key=k, match=MatchValue(value=v)) for k, v in filters.items()
        ]
        qdrant_filter = Filter(must=must_conditions) if must_conditions else None
        oversample_k  = top_k * OVERSAMPLE_FACTOR if RERANKER_ENABLED else top_k

        with _otel_span("qdrant.search"):
            results = await qdrant.search(
                collection_name=collection, query_vector=vector,
                query_filter=qdrant_filter, limit=oversample_k, with_payload=True,
            )

        filtered = []
        for hit in results:
            classification = hit.payload.get("classification", "internal")
            policy = await check_opa_policy(agent_id, agent_role,
                                            f"{collection}/{hit.id}", classification)
            if policy["allowed"]:
                filtered.append({
                    "id": str(hit.id), "score": round(hit.score, 4),
                    "content": hit.payload.get("text", hit.payload.get("content", "")),
                    "metadata": {k: v for k, v in hit.payload.items()
                                 if k not in ("content", "text")},
                })

        reranked = await rerank_results(query, filtered, top_n=top_k)
        mcp_search_results_count.labels(collection=collection).observe(len(reranked))

        pool = await get_pg_pool()
        await check_feedback_warning(query, pool)

        # Vault resolution: if token provided, try to resolve originals
        if pii_token and purpose:
            token_result = validate_pii_access_token(pii_token)
            if token_result["valid"]:
                import hashlib as _hl
                token_hash = _hl.sha256(
                    json.dumps(pii_token, sort_keys=True).encode()
                ).hexdigest()[:16]

                for item in reranked:
                    vault_ref = item.get("metadata", {}).get("vault_ref")
                    if not vault_ref:
                        continue
                    doc_id = item.get("metadata", {}).get("document_id")
                    if not doc_id:
                        # Try to find doc_id from vault_ref
                        doc_row = await pool.fetchrow(
                            "SELECT document_id FROM pii_vault.original_content WHERE id = $1",
                            vault_ref,
                        )
                        doc_id = str(doc_row["document_id"]) if doc_row else None
                    if not doc_id:
                        continue

                    classification = item.get("metadata", {}).get("classification", "internal")
                    data_category = item.get("metadata", {}).get("data_category", "")

                    vault_policy = await check_opa_vault_access(
                        agent_role, purpose, classification,
                        data_category, True, False,
                    )
                    if vault_policy["allowed"]:
                        vault_data = await vault_lookup(doc_id, [item.get("metadata", {}).get("chunk_index", 0)])
                        if vault_data:
                            original = vault_data[0]
                            redacted_text = redact_fields(
                                original["original_text"],
                                original["pii_entities"],
                                set(vault_policy["fields_to_redact"]),
                            )
                            item["original_content"] = redacted_text
                            item["vault_access"] = True

                            await log_vault_access(
                                agent_id, doc_id,
                                item.get("metadata", {}).get("chunk_index"),
                                purpose, token_hash,
                            )

        await log_access(agent_id, agent_role, "search", collection, "search", "allow", {
            "query": query, "qdrant_results": len(results),
            "after_policy": len(filtered), "after_rerank": len(reranked),
            "vault_access_requested": pii_token is not None,
        })
        return [TextContent(type="text",
            text=json.dumps({"results": reranked, "total": len(reranked)}, ensure_ascii=False, indent=2))]
```

**Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_mcp_vault_access.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add mcp-server/server.py tests/test_mcp_vault_access.py
git commit -m "feat: add vault access token validation, lookup, and field redaction to MCP server"
```

---

## Task 6: Retention Cleanup — Vault Extension

**Files:**
- Modify: `ingestion/retention_cleanup.py` (extend for vault cleanup)
- Test: `tests/test_retention_vault_cleanup.py`

**Step 1: Write the failing test**

```python
# tests/test_retention_vault_cleanup.py
"""Validates retention_cleanup.py handles vault entries."""
import unittest
import pathlib

CLEANUP_FILE = pathlib.Path(__file__).resolve().parent.parent / "ingestion" / "retention_cleanup.py"


class TestRetentionVaultCleanup(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.code = CLEANUP_FILE.read_text(encoding="utf-8")

    def test_vault_content_cleanup(self):
        """Must query and delete expired vault content."""
        self.assertIn("pii_vault.original_content", self.code)

    def test_vault_mapping_cleanup(self):
        """Must delete associated pseudonym mappings."""
        self.assertIn("pii_vault.pseudonym_mapping", self.code)

    def test_orphaned_vault_cleanup(self):
        """Must handle orphaned vault entries."""
        self.assertIn("orphan", self.code.lower())

    def test_vault_cleanup_function(self):
        """Must have a dedicated vault cleanup function."""
        self.assertIn("def clean_expired_vault", self.code)


if __name__ == "__main__":
    unittest.main()
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_retention_vault_cleanup.py -v`
Expected: FAIL

**Step 3: Add vault cleanup to retention_cleanup.py**

Add the following functions before `main()` (before line 161) in `ingestion/retention_cleanup.py`:

```python
async def clean_expired_vault(conn, dry_run: bool = True) -> dict:
    """
    Deletes expired vault entries and associated mappings.
    Returns statistics.
    """
    stats = {"expired_content": 0, "expired_mappings": 0, "orphaned": 0}

    # 1. Find expired vault entries
    expired = await conn.fetch("""
        SELECT id, document_id, chunk_index
        FROM pii_vault.original_content
        WHERE retention_expires_at <= now()
    """)
    stats["expired_content"] = len(expired)

    if expired and not dry_run:
        expired_ids = [r["id"] for r in expired]
        expired_doc_chunks = [(r["document_id"], r["chunk_index"]) for r in expired]

        # Delete mappings
        for doc_id, chunk_idx in expired_doc_chunks:
            deleted = await conn.execute("""
                DELETE FROM pii_vault.pseudonym_mapping
                WHERE document_id = $1 AND chunk_index = $2
            """, doc_id, chunk_idx)
            stats["expired_mappings"] += int(deleted.split()[-1]) if deleted else 0

        # Delete original content
        await conn.execute("""
            DELETE FROM pii_vault.original_content
            WHERE id = ANY($1::uuid[])
        """, expired_ids)

        log.info(
            f"Vault Cleanup: {stats['expired_content']} expired entries deleted, "
            f"{stats['expired_mappings']} mappings removed"
        )

    # 2. Orphaned vault entries (document_meta deleted, vault still present)
    orphaned = await conn.fetch("""
        SELECT oc.id, oc.document_id
        FROM pii_vault.original_content oc
        LEFT JOIN documents_meta dm ON dm.id = oc.document_id
        WHERE dm.id IS NULL
    """)
    stats["orphaned"] = len(orphaned)

    if orphaned and not dry_run:
        orphan_ids = [r["id"] for r in orphaned]
        orphan_doc_ids = list({r["document_id"] for r in orphaned})

        for doc_id in orphan_doc_ids:
            await conn.execute("""
                DELETE FROM pii_vault.pseudonym_mapping
                WHERE document_id = $1
            """, doc_id)

        await conn.execute("""
            DELETE FROM pii_vault.original_content
            WHERE id = ANY($1::uuid[])
        """, orphan_ids)

        log.info(f"Vault Cleanup: {stats['orphaned']} orphaned entries removed")

    return stats
```

Then in `main()`, add a Phase 3 vault cleanup step after the existing Phase 2 (after `process_deletion_requests`):

```python
        # Phase 3: Vault cleanup
        log.info("=== Phase 3: Vault Retention + Orphan Cleanup ===")
        vault_stats = await clean_expired_vault(conn, dry_run=dry_run)
        log.info(
            f"Vault: {vault_stats['expired_content']} expired, "
            f"{vault_stats['orphaned']} orphaned"
            f"{' (dry-run)' if dry_run else ' → deleted'}"
        )
```

**Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_retention_vault_cleanup.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add ingestion/retention_cleanup.py tests/test_retention_vault_cleanup.py
git commit -m "feat: extend retention cleanup for vault expiration and orphan handling"
```

---

## Task 7: Art. 17 Deletion — 2-Tier Vault Deletion

**Files:**
- Modify: `ingestion/retention_cleanup.py` (extend `process_deletion_requests`)
- Test: `tests/test_art17_vault_deletion.py`

**Step 1: Write the failing test**

```python
# tests/test_art17_vault_deletion.py
"""Validates Art. 17 deletion handles vault data in two tiers."""
import unittest
import pathlib

CLEANUP_FILE = pathlib.Path(__file__).resolve().parent.parent / "ingestion" / "retention_cleanup.py"


class TestArt17VaultDeletion(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.code = CLEANUP_FILE.read_text(encoding="utf-8")

    def test_vault_deletion_in_process(self):
        """process_deletion_requests must delete vault data."""
        # The function should reference vault tables
        self.assertIn("pii_vault.original_content", self.code)
        self.assertIn("pii_vault.pseudonym_mapping", self.code)

    def test_restrict_tier(self):
        """Must handle 'restrict' action (delete vault, keep qdrant)."""
        self.assertIn("restrict", self.code)

    def test_deletion_records_vault(self):
        """Deletion records must track vault deletion."""
        self.assertIn("vault", self.code.lower())


if __name__ == "__main__":
    unittest.main()
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_art17_vault_deletion.py -v`
Expected: FAIL (or partial pass since "vault" might appear after Task 6)

**Step 3: Extend process_deletion_requests**

Modify `process_deletion_requests()` in `ingestion/retention_cleanup.py` (lines 109-158) to add vault deletion. In the deletion loop, after deleting Qdrant points and before updating deletion status, add:

```python
            # Vault: delete original + mapping (Tier 1: restrict)
            vault_deleted = 0
            mapping_deleted = 0
            for ds_id in dataset_ids:
                m_result = await conn.execute("""
                    DELETE FROM pii_vault.pseudonym_mapping
                    WHERE document_id IN (
                        SELECT id FROM documents_meta WHERE id = $1
                    )
                """, ds_id)
                mapping_deleted += int(m_result.split()[-1]) if m_result else 0

                v_result = await conn.execute("""
                    DELETE FROM pii_vault.original_content
                    WHERE document_id IN (
                        SELECT id FROM documents_meta WHERE id = $1
                    )
                """, ds_id)
                vault_deleted += int(v_result.split()[-1]) if v_result else 0

            # restrict: keep Qdrant points, but set contains_pii → false
            # (pseudonyms are now irreversible = effectively anonymous)
            if vault_deleted > 0:
                for ds_id in dataset_ids:
                    await conn.execute("""
                        UPDATE documents_meta SET contains_pii = false
                        WHERE id = $1
                    """, ds_id)
```

Also update the `deleted_records` JSON in the status update to include vault info:

```python
            deleted_records = json.dumps({
                "datasets": dataset_ids,
                "qdrant_points": qdrant_deleted,
                "vault_content": vault_deleted,
                "vault_mappings": mapping_deleted,
                "restrict": vault_deleted > 0,
            })
```

**Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_art17_vault_deletion.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add ingestion/retention_cleanup.py tests/test_art17_vault_deletion.py
git commit -m "feat: add 2-tier vault deletion to Art. 17 erasure flow"
```

---

## Task 8: Documentation — Update CLAUDE.md + bekannte-schwachstellen.md

**Files:**
- Modify: `CLAUDE.md` (add vault to architecture, components, tools)
- Modify: `docs/bekannte-schwachstellen.md` (mark closed gaps)

**Step 1: Update CLAUDE.md**

Add `pii_vault` schema to the architecture diagram. Add `VAULT_HMAC_SECRET` to the environment variables section. Add `vault_ref` to the Qdrant payload description. Update MCP-Tools count to mention vault access capability on `search_knowledge`.

**Step 2: Update bekannte-schwachstellen.md**

Mark the following as resolved:
- OPA `kb.privacy.pii_action` never called → now called in ingestion
- `pseudonymize_text()` never called → now used in dual storage path
- `pii_scan_log` never written → now written during ingestion
- `fields_to_redact` never applied → now applied during vault access
- Bug in `pseudonymize_text()` → fixed (individual pseudonyms per entity)

**Step 3: Commit**

```bash
git add CLAUDE.md docs/bekannte-schwachstellen.md
git commit -m "docs: update architecture and close resolved privacy gaps"
```

---

## Task 9: Integration Smoke Test

**Files:**
- Create: `tests/test_vault_integration.py`

**Step 1: Write integration test**

```python
# tests/test_vault_integration.py
"""
Integration smoke test for the Sealed Vault dual storage pipeline.
Requires running services (Qdrant, PostgreSQL, OPA, Ollama).
Run with: pytest tests/test_vault_integration.py -v --integration
"""
import unittest
import os

INTEGRATION = os.getenv("RUN_INTEGRATION_TESTS", "").lower() in ("1", "true", "yes")


@unittest.skipUnless(INTEGRATION, "Set RUN_INTEGRATION_TESTS=1 to run")
class TestVaultIntegration(unittest.TestCase):
    """Smoke test: ingest PII data → verify vault + qdrant → search → verify pseudonymized."""

    async def test_ingest_with_pii_creates_vault_entry(self):
        """Ingest internal-classified data with PII → should create vault entry."""
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.post("http://localhost:8081/ingest", json={
                "source": "integration-test-vault",
                "source_type": "csv",
                "project": "test-project",
                "classification": "internal",
                "content": "Max Mustermann, max@example.com, +49 170 1234567",
                "metadata": {"data_category": "customer_data"},
            })
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertTrue(data.get("pii_detected"))
            self.assertTrue(data.get("dual_storage"))

    async def test_search_returns_pseudonymized(self):
        """Search without token should return pseudonymized text."""
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.post("http://localhost:8080/mcp", json={
                "method": "tools/call",
                "params": {
                    "name": "search_knowledge",
                    "arguments": {
                        "query": "Mustermann",
                        "agent_id": "test-agent",
                        "agent_role": "analyst",
                    },
                },
            })
            self.assertEqual(resp.status_code, 200)
            # Results should NOT contain original names
            text = resp.text
            self.assertNotIn("Max Mustermann", text)
            self.assertNotIn("max@example.com", text)


if __name__ == "__main__":
    unittest.main()
```

**Step 2: Commit**

```bash
git add tests/test_vault_integration.py
git commit -m "test: add vault integration smoke test"
```

---

## Dependency Graph

```
Task 1 (SQL Migration)  ─┐
Task 2 (OPA Policies)   ─┼─→ Task 4 (Ingestion Pipeline) ─→ Task 7 (Art.17 Deletion)
Task 3 (Fix pseudonymize)┘                                 ↗
                                                          /
Task 5 (MCP Server Token+Vault) ─────────────────────────┘
Task 6 (Retention Cleanup) ──────────────────────────────→ Task 7
                                                            ↓
Task 8 (Documentation) ←────────────────────────────── Task 9 (Integration Test)
```

**Parallelizable:** Tasks 1, 2, 3 can run in parallel. Tasks 5 and 6 can run in parallel (after 1+2). Task 7 depends on 5+6. Tasks 8+9 are sequential at the end.
