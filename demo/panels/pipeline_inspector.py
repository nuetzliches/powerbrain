"""Tab E — Pipeline Inspector.

Shows what happens to a document end-to-end through Powerbrain's
ingestion pipeline without persisting anything. The narrative is
adapter-agnostic: fixtures are labelled as representative of the
existing adapters (GitHub, SharePoint, Outlook) so a decision-maker
sees the four phases (extract → PII scan → quality gate → OPA privacy)
applied consistently regardless of source.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

import streamlit as st

from mcp_client import _IngestionClient


FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"


def _load_manifest() -> list[dict]:
    manifest_path = FIXTURES_DIR / "manifest.json"
    if not manifest_path.exists():
        return []
    return json.loads(manifest_path.read_text(encoding="utf-8"))


PII_COLORS = {
    "PERSON":            "#c62828",
    "EMAIL_ADDRESS":     "#ad1457",
    "PHONE_NUMBER":      "#6a1b9a",
    "IBAN_CODE":         "#1565c0",
    "LOCATION":          "#2e7d32",
    "DE_DATE_OF_BIRTH":  "#ef6c00",
    "DE_TAX_ID":         "#00838f",
    "DATE_OF_BIRTH":     "#ef6c00",
}


def _badge(text: str, color: str) -> str:
    return (
        f"<span style='background:{color};color:white;padding:2px 8px;"
        f"border-radius:10px;font-size:0.8em;margin-right:4px'>{text}</span>"
    )


def _action_badge(pii_action: str | None) -> str:
    colors = {
        "mask":               "#455a64",
        "pseudonymize":       "#1565c0",
        "encrypt_and_store":  "#2e7d32",  # vault!
        "block":              "#c62828",
    }
    color = colors.get(pii_action or "", "#777")
    return _badge(pii_action or "?", color)


def _render_extract(extract: dict) -> None:
    st.markdown("#### 1 · Extract")
    status = extract.get("status", "skipped")
    if status == "skipped":
        st.caption("Input was already plain text — no extractor needed.")
        return
    cols = st.columns(4)
    cols[0].metric("Status", status)
    cols[1].metric("Extractor", extract.get("extractor", "?"))
    cols[2].metric("Bytes in", extract.get("bytes_in", "?"))
    cols[3].metric("Chars out", extract.get("chars_out", "?"))
    st.caption(f"Duration: {extract.get('duration_ms', '?')} ms")


def _render_scan(scan: dict, text: str) -> None:
    st.markdown("#### 2 · PII Scan")
    cols = st.columns([1, 2])
    with cols[0]:
        st.metric("Contains PII", "yes" if scan.get("contains_pii") else "no")
        st.caption(f"Duration: {scan.get('duration_ms', '?')} ms")
    with cols[1]:
        counts = scan.get("entity_counts") or {}
        if counts:
            badges = "".join(
                _badge(f"{k} ×{v}", PII_COLORS.get(k, "#555"))
                for k, v in counts.items()
            )
            st.markdown(badges, unsafe_allow_html=True)
        else:
            st.caption("No entities detected.")

    if scan.get("entity_locations"):
        with st.expander("First detected entities"):
            for loc in scan["entity_locations"][:10]:
                etype = loc.get("type", "?")
                snippet = (loc.get("text_snippet") or "").replace("\n", " ")
                score = loc.get("score", 0)
                st.markdown(
                    f"{_badge(etype, PII_COLORS.get(etype, '#555'))} "
                    f"score **{score:.2f}** · `{snippet.strip()[:80]}`",
                    unsafe_allow_html=True,
                )


def _render_verifier(verifier: dict) -> None:
    """Show the semantic-verifier outcome when the OPA policy enabled it.

    Noop deployments (community default) get a light-touch caption so the
    panel doesn't look empty; enterprise deployments with ``backend=llm``
    get counts + duration + per-entity-type breakdown.
    """
    st.markdown("#### 2b · Semantic Verifier")
    if not verifier.get("enabled"):
        backend = verifier.get("backend", "noop")
        st.caption(
            f"Disabled (`backend={backend}`). Enable via "
            "`pb.config.ingestion.pii_verifier.enabled=true` to have an "
            "LLM double-check ambiguous candidates (PERSON / LOCATION / "
            "ORGANIZATION) before downstream phases see them."
        )
        return

    cols = st.columns(5)
    cols[0].metric("Input", verifier.get("input_count", 0))
    cols[1].metric("Forwarded", verifier.get("forwarded", 0),
                   help="Pattern-based types (IBAN, email, phone, DOB) "
                        "kept without LLM review.")
    cols[2].metric("Reviewed", verifier.get("reviewed", 0),
                   help="Ambiguous candidates sent to the LLM.")
    cols[3].metric("Reverted", verifier.get("reverted", 0),
                   help="LLM decided these were false positives.",
                   delta=f"-{verifier.get('reverted', 0)} FP",
                   delta_color="inverse")
    cols[4].metric("Duration", f"{verifier.get('duration_ms', 0):.0f} ms")

    before = verifier.get("before") or {}
    if before:
        before_total = sum(int(v) for v in (before.get("entity_counts") or {}).values())
        st.caption(
            f"Before verifier: {before_total} candidates · "
            f"After: {verifier.get('kept', 0)} kept · "
            f"backend=`{verifier.get('backend', '?')}`"
        )

    per_type = verifier.get("by_entity_type") or {}
    if per_type:
        with st.expander("Per-entity-type breakdown"):
            rows = [
                {
                    "entity_type": etype,
                    "total":       b.get("total", 0),
                    "forwarded":   b.get("forwarded", 0),
                    "kept":        b.get("kept", 0),
                    "reverted":    b.get("reverted", 0),
                }
                for etype, b in sorted(per_type.items())
            ]
            st.dataframe(rows, use_container_width=True, hide_index=True)


def _render_quality(quality: dict) -> None:
    st.markdown("#### 3 · Quality Gate (Art. 10)")
    if "error" in quality:
        st.error(f"Quality compute error: {quality['error']}")
        return

    score = quality.get("score", 0.0)
    allowed = quality.get("gate_allowed", False)
    minimum = quality.get("gate_min_score", 0.0)
    language = quality.get("language", "?")

    cols = st.columns(4)
    cols[0].metric("Score", f"{score:.3f}",
                   delta=f"min {minimum:.2f}",
                   delta_color="off")
    cols[1].metric("Gate", "allow" if allowed else "reject")
    cols[2].metric("Language", language)
    cols[3].metric("Duration", f"{quality.get('duration_ms', '?')} ms")

    if not allowed:
        st.warning(
            f"Would be rejected: {quality.get('gate_reason', 'below threshold')}"
        )

    with st.expander("Factor breakdown (weights applied)"):
        factors = quality.get("factors") or {}
        weights = quality.get("weights") or {}
        for k, v in factors.items():
            w = weights.get(k, 0)
            st.progress(v, text=f"{k} — raw {v:.2f} · weight {w}")


def _render_privacy(privacy: dict) -> None:
    st.markdown("#### 4 · OPA Privacy Decision")
    if "error" in privacy:
        st.error(f"OPA error: {privacy['error']}")
        return

    action = privacy.get("pii_action")
    classification = privacy.get("classification", "?")
    dual_storage = privacy.get("dual_storage_enabled", False)
    retention = privacy.get("retention_days")

    cols = st.columns([2, 1, 1, 1])
    with cols[0]:
        st.markdown(
            f"**Classification:** `{classification}` → action {_action_badge(action)}",
            unsafe_allow_html=True,
        )
    cols[1].metric("Dual storage", "yes" if dual_storage else "no")
    cols[2].metric("Retention (days)", retention if retention is not None else "default")
    cols[3].metric("Duration", f"{privacy.get('duration_ms', '?')} ms")

    if action == "block" and not privacy.get("legal_basis_supplied"):
        st.info(
            "`block` is the default on confidential+PII until a legal basis "
            "is declared. Real ingestions set `metadata.legal_basis` — the "
            "seed does that for our customer fixtures."
        )
    if action == "encrypt_and_store":
        st.success(
            "Pseudonymised chunk goes to Qdrant, original lives in the "
            "sealed vault. Purpose-bound vault token unlocks originals "
            "via `POST /vault/resolve` (see Tab D)."
        )


def _render_summary(summary: dict) -> None:
    st.markdown("#### Outcome")
    would = summary.get("would_ingest", False)
    if would:
        st.success(
            f"✓ Would ingest → `{summary.get('target_collection')}`, "
            f"action `{summary.get('pii_action')}`, "
            f"{summary.get('chars', 0)} chars"
        )
    else:
        reasons = "; ".join(summary.get("reasons") or []) or "see phases above"
        st.error(f"✗ Would NOT ingest — {reasons}")
    st.caption(f"Total pipeline dry-run: {summary.get('duration_ms', '?')} ms")


def render(ingestion: _IngestionClient) -> None:
    st.subheader("Pipeline Inspector — what Powerbrain does with your document")
    st.write(
        "Pick a fixture or upload a document. Every ingestion phase "
        "runs against it without persisting anything, so you can show "
        "the pipeline end-to-end for every adapter on the same screen."
    )

    manifest = _load_manifest()
    if not manifest:
        st.warning("No fixtures available under demo/fixtures/.")
        return

    fixture_labels = [f"{m['title']} ({m['adapter']})" for m in manifest]
    idx = st.selectbox(
        "Fixture",
        options=list(range(len(manifest))),
        format_func=lambda i: fixture_labels[i],
        index=0,
    )
    fixture = manifest[idx]
    st.caption(fixture["description"])

    custom_col, upload_col = st.columns([2, 1])
    with custom_col:
        fixture_path = FIXTURES_DIR / fixture["file"]
        default_text = (
            fixture_path.read_text(encoding="utf-8")
            if fixture_path.exists() else ""
        )
        text = st.text_area(
            "Document text (editable)",
            value=st.session_state.get("pi_text_" + fixture["id"], default_text),
            height=240,
            key="pi_text_" + fixture["id"],
        )

    with upload_col:
        st.caption("Or upload your own file (runs through the extractor first):")
        uploaded = st.file_uploader(
            "Upload",
            type=["md", "txt", "pdf", "docx", "xlsx", "pptx", "msg", "eml",
                  "html", "csv", "json"],
            label_visibility="collapsed",
        )

    # Classification override row
    cls_cols = st.columns(3)
    with cls_cols[0]:
        classification = st.selectbox(
            "Classification", ["public", "internal", "confidential", "restricted"],
            index=["public", "internal", "confidential", "restricted"].index(
                fixture.get("classification", "internal")
            ),
        )
    with cls_cols[1]:
        source_type = st.selectbox(
            "source_type (→ quality gate)",
            ["default", "github", "office365", "email", "teams", "onenote", "code", "contracts"],
            index=(["default", "github", "office365", "email", "teams", "onenote", "code", "contracts"]
                   .index(fixture.get("source_type", "default"))
                   if fixture.get("source_type", "default") in
                       ["default", "github", "office365", "email", "teams", "onenote", "code", "contracts"]
                   else 0),
        )
    with cls_cols[2]:
        legal_basis_input = st.text_input(
            "legal_basis (optional)",
            value=fixture.get("legal_basis", ""),
            help=(
                "Required by OPA when PII meets confidential. Typical values: "
                "`legitimate_interest`, `contract_fulfillment`, `consent`."
            ),
        )

    submitted = st.button("Run dry-run", type="primary", use_container_width=True)

    if not submitted:
        st.info("Press **Run dry-run** to see the pipeline phases.")
        return

    try:
        if uploaded is not None:
            data_b64 = base64.b64encode(uploaded.read()).decode("ascii")
            result = ingestion.preview(
                data_b64=data_b64,
                filename=uploaded.name,
                classification=classification,
                source_type=source_type,
                legal_basis=legal_basis_input or None,
                metadata={"data_category": fixture.get("data_category") or ""},
            )
        else:
            result = ingestion.preview(
                text=text,
                classification=classification,
                source_type=source_type,
                legal_basis=legal_basis_input or None,
                metadata={"data_category": fixture.get("data_category") or ""},
            )
    except Exception as exc:  # noqa: BLE001
        st.error(f"Preview failed: {exc}")
        return

    _render_extract(result.get("extract") or {})
    _render_scan(result.get("scan") or {}, text)
    _render_verifier(result.get("verifier") or {})
    _render_quality(result.get("quality") or {})
    _render_privacy(result.get("privacy") or {})
    _render_summary(result.get("summary") or {})

    with st.expander("Raw /preview response (for debugging)"):
        st.json(result, expanded=False)

    with st.expander("How this maps to a real adapter run"):
        st.markdown(
            """
            - **Extract** is the step every adapter (GitHub, SharePoint,
              Outlook, OneNote, Teams, `/extract` chat uploads) goes
              through. Binary sources feed markitdown + fallbacks; text
              sources skip this phase.
            - **PII scan** uses the same Presidio pipeline that runs at
              the real `/ingest`. Counts and entity types are authoritative.
            - **Quality gate** thresholds live in
              `opa-policies/data.json → pb.config.ingestion.min_quality_score`
              and can be edited per source_type via the `manage_policies`
              MCP tool.
            - **OPA privacy decision** is the one that controls whether
              a document is pseudonymised, vaulted, or blocked. `block`
              is the default on confidential+PII until a `legal_basis`
              is declared — real ingestions supply it; our customer seed
              does too.
            - Nothing the inspector does here touches PostgreSQL or
              Qdrant. To ingest for real, use the CLI or the
              `/ingest` endpoint directly — the policy decisions will
              match what you see above.
            """
        )
