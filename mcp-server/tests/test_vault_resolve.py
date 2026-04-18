"""Tests for ``vault_resolve_pseudonyms`` (text-level vault lookup).

The function is the hot path for the enterprise edition pb-proxy: it
converts ``[ENTITY_TYPE:hash]`` pseudonyms back to originals subject to
the same OPA policy and audit trail as the search_knowledge inline
vault reveal. Covers the hash-matching logic, OPA denial, purpose-based
field redaction, graceful skip for unknown pseudonyms, and the audit
log call pattern.
"""
from __future__ import annotations

import hashlib
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import server


SALT = "project-salt-1"


def _pseudo(entity_text: str, entity_type: str, salt: str = SALT) -> str:
    digest = hashlib.sha256(f"{salt}:{entity_text}".encode()).hexdigest()[:8]
    return f"[{entity_type}:{digest}]"


def _mapping_row(
    pseudonym: str,
    doc_id: str,
    chunk_index: int,
    entity_type: str,
    salt: str,
    original_text: str,
    pii_entities: list[dict],
    classification: str = "confidential",
    data_category: str = "customer_data",
) -> dict:
    """Shape a row matching the SQL SELECT inside vault_resolve_pseudonyms()."""
    return {
        "pseudonym":      pseudonym,
        "document_id":    doc_id,
        "chunk_index":    chunk_index,
        "entity_type":    entity_type,
        "salt":           salt,
        "original_text":  original_text,
        "pii_entities":   json.dumps(pii_entities),
        "classification": classification,
        "metadata":       json.dumps({"data_category": data_category}),
    }


@pytest.fixture
def mock_pool_with_rows():
    """Factory returning a pool whose fetch() yields the supplied rows."""
    def _make(rows):
        pool = AsyncMock()
        pool.fetch = AsyncMock(return_value=rows)
        pool.execute = AsyncMock(return_value="INSERT 0 1")
        return pool
    return _make


@pytest.fixture
def patch_pg_pool(mock_pool_with_rows, monkeypatch):
    def _apply(rows):
        pool = mock_pool_with_rows(rows)
        async def _get_pool():
            return pool
        monkeypatch.setattr(server, "get_pg_pool", _get_pool)
        return pool
    return _apply


@pytest.fixture
def allow_vault(monkeypatch):
    """OPA vault_access_allowed always allows, with empty fields_to_redact."""
    async def _allow(*args, **kwargs):
        return {"allowed": True, "fields_to_redact": []}
    monkeypatch.setattr(server, "check_opa_vault_access", _allow)


class TestVaultResolvePseudonyms:
    @pytest.mark.asyncio
    async def test_empty_text(self, patch_pg_pool, allow_vault):
        patch_pg_pool([])
        out = await server.vault_resolve_pseudonyms(
            "", purpose="support", agent_role="analyst",
            agent_id="a", token_hash="t",
        )
        assert out["text"] == ""
        assert out["resolved"] == 0
        assert out["total"] == 0

    @pytest.mark.asyncio
    async def test_text_without_pseudonyms(self, patch_pg_pool, allow_vault):
        patch_pg_pool([])
        out = await server.vault_resolve_pseudonyms(
            "Just plain text.", purpose="support", agent_role="analyst",
            agent_id="a", token_hash="t",
        )
        assert out["text"] == "Just plain text."
        assert out["resolved"] == 0

    @pytest.mark.asyncio
    async def test_resolves_matching_pseudonym(
        self, patch_pg_pool, allow_vault,
    ):
        pseudo = _pseudo("Anna Müller", "PERSON")
        row = _mapping_row(
            pseudonym=pseudo,
            doc_id="doc-1", chunk_index=0,
            entity_type="PERSON", salt=SALT,
            original_text="Kunde: Anna Müller ist aktiv.",
            pii_entities=[
                {"type": "PERSON", "text": "Anna Müller",
                 "start": 7, "end": 18, "score": 0.95}
            ],
        )
        patch_pg_pool([row])

        out = await server.vault_resolve_pseudonyms(
            f"Kunde {pseudo} braucht Support.",
            purpose="support", agent_role="analyst",
            agent_id="a", token_hash="t",
        )
        assert out["resolved"] == 1
        assert out["total"] == 1
        assert "Anna Müller" in out["text"]
        assert pseudo not in out["text"]

    @pytest.mark.asyncio
    async def test_skip_when_hash_has_no_matching_entity(
        self, patch_pg_pool, allow_vault,
    ):
        """The mapping row exists but pii_entities doesn't contain the
        original for this specific hash (e.g. vault-entities corrupted or
        another entity of the same type). Don't leak: skip silently."""
        pseudo = _pseudo("Anna Müller", "PERSON")
        row = _mapping_row(
            pseudonym=pseudo, doc_id="doc-1", chunk_index=0,
            entity_type="PERSON", salt=SALT,
            original_text="...",
            pii_entities=[
                {"type": "PERSON", "text": "Someone Else"},
            ],
        )
        patch_pg_pool([row])

        out = await server.vault_resolve_pseudonyms(
            f"Case {pseudo}", purpose="support", agent_role="analyst",
            agent_id="a", token_hash="t",
        )
        assert out["resolved"] == 0
        assert pseudo in out["text"]  # left intact

    @pytest.mark.asyncio
    async def test_opa_denial_keeps_pseudonym(
        self, patch_pg_pool, monkeypatch,
    ):
        async def _deny(*args, **kwargs):
            return {"allowed": False, "fields_to_redact": []}
        monkeypatch.setattr(server, "check_opa_vault_access", _deny)

        pseudo = _pseudo("Anna Müller", "PERSON")
        row = _mapping_row(
            pseudonym=pseudo, doc_id="doc-1", chunk_index=0,
            entity_type="PERSON", salt=SALT,
            original_text="Anna Müller",
            pii_entities=[{"type": "PERSON", "text": "Anna Müller"}],
        )
        patch_pg_pool([row])

        out = await server.vault_resolve_pseudonyms(
            pseudo, purpose="reporting", agent_role="analyst",
            agent_id="a", token_hash="t",
        )
        assert out["resolved"] == 0
        assert out["skipped"] == 1
        assert pseudo in out["text"]

    @pytest.mark.asyncio
    async def test_purpose_fields_to_redact_blocks_person(
        self, patch_pg_pool, monkeypatch,
    ):
        """support purpose allows vault access, but if the policy lists
        `person` in fields_to_redact we must not resolve the PERSON tag
        — this is how billing vs. support differ in the sales demo."""
        async def _allow_but_redact_person(*args, **kwargs):
            return {"allowed": True, "fields_to_redact": ["person"]}
        monkeypatch.setattr(server, "check_opa_vault_access",
                            _allow_but_redact_person)

        pseudo = _pseudo("Anna Müller", "PERSON")
        row = _mapping_row(
            pseudonym=pseudo, doc_id="doc-1", chunk_index=0,
            entity_type="PERSON", salt=SALT,
            original_text="Anna Müller",
            pii_entities=[{"type": "PERSON", "text": "Anna Müller"}],
        )
        patch_pg_pool([row])

        out = await server.vault_resolve_pseudonyms(
            pseudo, purpose="support", agent_role="analyst",
            agent_id="a", token_hash="t",
        )
        assert out["resolved"] == 0
        assert pseudo in out["text"]

    @pytest.mark.asyncio
    async def test_unknown_pseudonym_silently_skipped(
        self, patch_pg_pool, allow_vault,
    ):
        patch_pg_pool([])  # no matching mapping row
        bogus = "[PERSON:00000000]"
        out = await server.vault_resolve_pseudonyms(
            f"Case {bogus}.", purpose="support", agent_role="analyst",
            agent_id="a", token_hash="t",
        )
        assert out["resolved"] == 0
        assert out["skipped"] == 1
        assert bogus in out["text"]

    @pytest.mark.asyncio
    async def test_multiple_pseudonyms_same_doc_audit_once(
        self, patch_pg_pool, allow_vault,
    ):
        """Two pseudonyms in the same chunk → one vault_access_log entry."""
        p1 = _pseudo("Anna Müller", "PERSON")
        p2 = _pseudo("anna@example.de", "EMAIL_ADDRESS")
        rows = [
            _mapping_row(
                pseudonym=p1, doc_id="doc-1", chunk_index=0,
                entity_type="PERSON", salt=SALT,
                original_text="Anna Müller · anna@example.de",
                pii_entities=[
                    {"type": "PERSON", "text": "Anna Müller"},
                    {"type": "EMAIL_ADDRESS", "text": "anna@example.de"},
                ],
            ),
            _mapping_row(
                pseudonym=p2, doc_id="doc-1", chunk_index=0,
                entity_type="EMAIL_ADDRESS", salt=SALT,
                original_text="Anna Müller · anna@example.de",
                pii_entities=[
                    {"type": "PERSON", "text": "Anna Müller"},
                    {"type": "EMAIL_ADDRESS", "text": "anna@example.de"},
                ],
            ),
        ]
        pool = patch_pg_pool(rows)

        out = await server.vault_resolve_pseudonyms(
            f"{p1} · {p2}", purpose="support", agent_role="analyst",
            agent_id="a", token_hash="t",
        )
        assert out["resolved"] == 2
        assert "Anna Müller" in out["text"]
        assert "anna@example.de" in out["text"]
        # log_vault_access should fire once per (doc_id, chunk_index).
        # The fetch() call is one, and execute() (log) is one.
        assert pool.execute.await_count == 1
