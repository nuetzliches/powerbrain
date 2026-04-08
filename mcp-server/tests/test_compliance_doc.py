"""Tests for B-46 Compliance Documentation Generator (Annex IV)."""

import json
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

import server
from compliance_doc import (
    ComplianceDocContext,
    SECTION_RENDERERS,
    collect_doc_context,
    generate_annex_iv_doc,
    render_doc,
)
from server import _dispatch


# ── Pure rendering tests ───────────────────────────────────

def _full_ctx(**overrides):
    base = {
        "transparency": {
            "report_version": "abc12345abc12345",
            "system_purpose": "Powerbrain feeds AI agents with policy-compliant context.",
            "deployment_constraints": [
                "OPA mediates all access",
                "PII scanning at ingestion",
            ],
            "models": {
                "embedding": {"name": "nomic-embed-text", "provider_url": "http://ollama"},
                "llm":       {"name": "qwen2.5:3b",       "provider_url": "http://ollama"},
                "reranker":  {"backend": "powerbrain", "model": "ms-marco", "enabled": True},
            },
            "opa": {
                "active_policies": ["pb.access", "pb.privacy", "pb.oversight"],
            },
            "collections": [
                {"name": "pb_general", "status": "green", "points": 100, "vectors": 100},
                {"name": "pb_code",    "status": "green", "points": 50,  "vectors": 50},
            ],
            "pii_scanner": {
                "scanner_enabled": True,
                "languages":       ["en", "de"],
                "entity_types":    ["PERSON", "EMAIL_ADDRESS"],
            },
            "audit_integrity": {"valid": True, "total_checked": 42},
        },
        "health": {
            "status": "info",
            "indicators": [
                {"name": "opa_reachable",       "severity": "ok",       "value": True},
                {"name": "audit_chain_integrity", "severity": "ok",     "value": "valid"},
                {"name": "circuit_breaker_active", "severity": "info",  "value": False},
            ],
        },
        "eval_stats": {
            "period_days": 30,
            "total_feedback": 250,
            "avg_rating": 4.1,
            "satisfaction_pct": 78.0,
            "windowed": [
                {"window": "1h", "collection": "pb_general", "samples": 5,
                 "avg_rating": 4.2, "empty_rate": 0.0, "rerank": 0.85},
            ],
            "drift_baselines": [
                {"collection": "pb_general", "seeded_at": "2026-04-08T10:00:00+00:00",
                 "sample_count": 200, "embedding_dim": 768},
            ],
        },
        "risk_register": "## R-01 Demo\nMitigation: tests.\n",
        "generated_at":  "2026-04-08T12:00:00+00:00",
    }
    base.update(overrides)
    return ComplianceDocContext(**base)


class TestRenderDoc:
    def test_full_doc_contains_all_section_headings(self):
        ctx = _full_ctx()
        body = render_doc(ctx)
        for heading in (
            "# EU AI Act Annex IV",
            "## 1. General Description",
            "## 2. System Elements",
            "## 3. Monitoring",
            "## 4. Accuracy and Performance Metrics",
            "## 5. Risk Management System",
            "## 6. Relevant Changes",
            "## 7. Harmonised Standards",
            "## 8. EU Declaration of Conformity",
            "## 9. Post-Market Monitoring",
        ):
            assert heading in body, f"missing section: {heading}"

    def test_header_includes_version_and_timestamp(self):
        ctx = _full_ctx()
        body = render_doc(ctx)
        assert "abc12345abc12345" in body
        assert "2026-04-08T12:00:00+00:00" in body

    def test_section_2_lists_all_models(self):
        ctx = _full_ctx()
        body = render_doc(ctx)
        assert "nomic-embed-text" in body
        assert "qwen2.5:3b" in body
        assert "ms-marco" in body  # reranker model name (preferred over backend)

    def test_section_2_lists_collections(self):
        ctx = _full_ctx()
        body = render_doc(ctx)
        assert "pb_general" in body
        assert "pb_code" in body

    def test_section_3_includes_audit_chain_status(self):
        ctx = _full_ctx()
        body = render_doc(ctx)
        assert "Hash chain valid: `True`" in body
        assert "verified at last check: `42`" in body

    def test_section_3_lists_indicators(self):
        ctx = _full_ctx()
        body = render_doc(ctx)
        assert "opa_reachable" in body
        assert "circuit_breaker_active" in body

    def test_section_4_includes_windowed_metrics(self):
        ctx = _full_ctx()
        body = render_doc(ctx)
        assert "Total feedback: `250`" in body
        assert "0.85" in body  # rerank score from windowed

    def test_section_4_includes_drift_baselines(self):
        ctx = _full_ctx()
        body = render_doc(ctx)
        assert "embedding_reference_set" in body
        assert "768" in body

    def test_section_5_embeds_risk_register(self):
        ctx = _full_ctx()
        body = render_doc(ctx)
        assert "## R-01 Demo" in body
        assert "Mitigation: tests." in body

    def test_deployer_placeholders_marked(self):
        ctx = _full_ctx()
        body = render_doc(ctx)
        # All [Deployer] markers should be present
        assert body.count("[Deployer]") >= 4

    def test_handles_empty_eval_stats(self):
        ctx = _full_ctx(eval_stats={})
        body = render_doc(ctx)
        assert "no feedback collected yet" in body
        assert "no baselines seeded yet" in body

    def test_handles_missing_transparency(self):
        ctx = _full_ctx(transparency={})
        body = render_doc(ctx)
        # Renderer must not crash on empty transparency
        assert "## 1. General Description" in body


# ── collect_doc_context ────────────────────────────────────

class TestCollectDocContext:
    async def test_loaders_called_in_parallel(self, tmp_path):
        calls = []

        async def t_loader():
            calls.append("t")
            return {"report_version": "v1"}

        async def h_loader():
            calls.append("h")
            return {"status": "info"}

        async def e_loader():
            calls.append("e")
            return {"period_days": 7}

        risk_path = tmp_path / "risk.md"
        risk_path.write_text("# Risk\n", encoding="utf-8")

        ctx = await collect_doc_context(
            transparency_loader=t_loader,
            health_loader=h_loader,
            eval_stats_loader=e_loader,
            risk_register_path=str(risk_path),
        )
        assert ctx.transparency == {"report_version": "v1"}
        assert ctx.health == {"status": "info"}
        assert ctx.eval_stats == {"period_days": 7}
        assert ctx.risk_register == "# Risk\n"
        assert set(calls) == {"t", "h", "e"}

    async def test_missing_risk_register_is_empty(self):
        async def t(): return {}
        async def h(): return {}
        async def e(): return {}
        ctx = await collect_doc_context(
            transparency_loader=t, health_loader=h, eval_stats_loader=e,
            risk_register_path="/nonexistent/path/risk.md",
        )
        assert ctx.risk_register == ""


# ── generate_annex_iv_doc ─────────────────────────────────

class TestGenerateAnnexIvDoc:
    async def test_inline_returns_markdown(self):
        async def t(): return {"report_version": "v1"}
        async def h(): return {"status": "ok"}
        async def e(): return {"period_days": 30}

        result = await generate_annex_iv_doc(
            transparency_loader=t, health_loader=h, eval_stats_loader=e,
            output_mode="inline",
            risk_register_path="/nonexistent.md",
        )
        assert result["output_mode"] == "inline"
        assert "markdown" in result
        assert result["markdown"].startswith("# EU AI Act Annex IV")
        assert result["report_version"] == "v1"
        assert result["size_bytes"] > 0
        assert "path" not in result

    async def test_file_writes_and_returns_path(self, tmp_path):
        async def t(): return {"report_version": "v2"}
        async def h(): return {}
        async def e(): return {}

        result = await generate_annex_iv_doc(
            transparency_loader=t, health_loader=h, eval_stats_loader=e,
            output_mode="file",
            output_dir=str(tmp_path),
            risk_register_path="/nonexistent.md",
        )
        assert result["output_mode"] == "file"
        assert "path" in result
        assert "markdown" not in result
        assert os.path.exists(result["path"])
        body = open(result["path"]).read()
        assert body.startswith("# EU AI Act Annex IV")
        assert result["size_bytes"] == len(body.encode("utf-8"))

    async def test_invalid_output_mode_raises(self):
        async def t(): return {}
        async def h(): return {}
        async def e(): return {}
        with pytest.raises(ValueError):
            await generate_annex_iv_doc(
                transparency_loader=t, health_loader=h, eval_stats_loader=e,
                output_mode="pdf",
            )


# ── MCP tool wrapper ───────────────────────────────────────

@pytest.fixture(autouse=True)
def _patch_globals(monkeypatch):
    mock_http = AsyncMock()
    mock_pool = AsyncMock()
    monkeypatch.setattr(server, "http", mock_http)
    monkeypatch.setattr(server, "pg_pool", mock_pool)

    async def _fake_get_pool():
        return mock_pool
    monkeypatch.setattr(server, "get_pg_pool", _fake_get_pool)

    async def _noop_log(*args, **kwargs):
        return None
    monkeypatch.setattr(server, "log_access", _noop_log)
    return mock_http, mock_pool


class TestGenerateComplianceDocTool:
    async def test_non_admin_denied(self, _patch_globals):
        result = await _dispatch(
            "generate_compliance_doc", {}, "agent-1", "analyst",
        )
        payload = json.loads(result[0].text)
        assert "error" in payload
        assert "admin" in payload["error"].lower()

    async def test_admin_inline_mode(self, _patch_globals, monkeypatch):
        # Stub the three loaders that compliance_doc calls
        async def _t():  return {"report_version": "v-test", "system_purpose": "test"}
        async def _h():  return {"status": "ok", "indicators": []}
        monkeypatch.setattr(server, "_get_transparency_report",  _t)
        monkeypatch.setattr(server, "_build_risk_health_payload", _h)

        # Stub _dispatch's eval-stats path
        original_dispatch = server._dispatch
        async def _patched_dispatch(name, arguments, agent_id, agent_role):
            if name == "get_eval_stats":
                from mcp.types import TextContent
                return [TextContent(type="text", text=json.dumps({"period_days": 30}))]
            return await original_dispatch(name, arguments, agent_id, agent_role)
        monkeypatch.setattr(server, "_dispatch", _patched_dispatch)

        result = await _patched_dispatch(
            "generate_compliance_doc", {"output_mode": "inline"},
            "admin-1", "admin",
        )
        payload = json.loads(result[0].text)
        assert payload["output_mode"] == "inline"
        assert payload["report_version"] == "v-test"
        assert "markdown" in payload
        assert "Annex IV" in payload["markdown"]

    async def test_admin_file_mode(self, _patch_globals, monkeypatch, tmp_path):
        async def _t():  return {"report_version": "v-file"}
        async def _h():  return {}
        monkeypatch.setattr(server, "_get_transparency_report",  _t)
        monkeypatch.setattr(server, "_build_risk_health_payload", _h)
        monkeypatch.setenv("COMPLIANCE_DOC_DIR", str(tmp_path))

        # Re-import so the env var is picked up
        import importlib, compliance_doc as _cd
        importlib.reload(_cd)
        monkeypatch.setattr(server, "_dispatch", server._dispatch)

        original_dispatch = server._dispatch
        async def _patched_dispatch(name, arguments, agent_id, agent_role):
            if name == "get_eval_stats":
                from mcp.types import TextContent
                return [TextContent(type="text", text=json.dumps({}))]
            return await original_dispatch(name, arguments, agent_id, agent_role)
        monkeypatch.setattr(server, "_dispatch", _patched_dispatch)

        result = await _patched_dispatch(
            "generate_compliance_doc", {"output_mode": "file"},
            "admin-1", "admin",
        )
        payload = json.loads(result[0].text)
        assert payload["output_mode"] == "file"
        assert "path" in payload
        assert os.path.exists(payload["path"])

    async def test_invalid_output_mode_returns_error(self, _patch_globals, monkeypatch):
        async def _t():  return {}
        async def _h():  return {}
        monkeypatch.setattr(server, "_get_transparency_report",  _t)
        monkeypatch.setattr(server, "_build_risk_health_payload", _h)

        original_dispatch = server._dispatch
        async def _patched_dispatch(name, arguments, agent_id, agent_role):
            if name == "get_eval_stats":
                from mcp.types import TextContent
                return [TextContent(type="text", text=json.dumps({}))]
            return await original_dispatch(name, arguments, agent_id, agent_role)
        monkeypatch.setattr(server, "_dispatch", _patched_dispatch)

        result = await _patched_dispatch(
            "generate_compliance_doc", {"output_mode": "pdf"},
            "admin-1", "admin",
        )
        payload = json.loads(result[0].text)
        assert "error" in payload


# ── Section renderer count sanity check ───────────────────

def test_section_renderer_count():
    # Header + 9 sections
    assert len(SECTION_RENDERERS) == 10
