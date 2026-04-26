"""Tab B — We never stored the secret.

Three-step live demo of the Sealed Vault:

    1. Ingest a synthetic German customer record (PII live).
    2. Search the same record — show the pseudonymised payload that lives in Qdrant.
    3. Reveal — issue a short-lived HMAC vault token, re-run the search with
       purpose=support, and surface the original content alongside the audit-log entry
       that was written during step 2 (and extended during step 3).
"""
from __future__ import annotations

import json
import uuid

import streamlit as st

from mcp_client import (
    DemoOutOfDateError,
    _IngestionClient,
    _MCPClient,
    build_vault_token,
)


SAMPLE_RECORD = (
    "Kundenprofil Demo. Name: Lena Vogt. E-Mail: lena.vogt@beispiel-kunden.de. "
    "Telefon: +49 151 98765432. Anschrift: Kastanienallee 18, 10435 Berlin. "
    "Bankverbindung: DE68 2005 0550 1234 5678 90. Geburtsdatum: 03.07.1991. "
    "Vertragsnummer: NT-DEMO-0001. Letzter Kontakt: durch Vertriebsmitarbeiter Tom Keller."
)


def _render_metadata_preview(metadata: dict) -> None:
    # Show only the keys that matter for the story — hide ingestion internals
    keys_of_interest = (
        "title", "classification", "project", "data_category",
        "vault_ref", "document_id", "chunk_index", "seed_id",
    )
    visible = {k: metadata.get(k) for k in keys_of_interest if k in metadata}
    st.json(visible, expanded=False)


def render(
    mcp: _MCPClient,
    ingestion: _IngestionClient,
    analyst_key: str,
    admin_key: str,
) -> None:
    st.subheader("We never stored the secret")
    st.write(
        "The vault splits every ingested PII document in two: a pseudonymised "
        "copy lives in Qdrant (searchable but anonymous), the original lives "
        "only in a PostgreSQL vault accessible via a short-lived, HMAC-signed "
        "token with purpose binding."
    )

    # ── Step 1 ─────────────────────────────────────────────────
    st.markdown("### Step 1 · Scan a customer record")
    st.caption(
        "We send the raw text to the ingestion `/scan` endpoint. Presidio reports "
        "which PII entities it detected — no data is stored yet."
    )

    text_area = st.text_area(
        "Customer record (editable)",
        value=st.session_state.get("pii_sample", SAMPLE_RECORD),
        height=140,
        key="pii_sample",
    )

    if st.button("Scan (detect PII)", key="pii_scan"):
        try:
            scan = ingestion.scan(text_area)
            st.session_state["pii_scan_result"] = scan
        except Exception as exc:  # noqa: BLE001
            st.error(f"Scan failed: {exc}")

    scan_result = st.session_state.get("pii_scan_result")
    if scan_result:
        cols = st.columns([2, 1])
        with cols[0]:
            st.markdown("**Masked (what would be stored):**")
            st.code(scan_result.get("masked_text", ""), language="text")
        with cols[1]:
            st.metric("Contains PII", "yes" if scan_result.get("contains_pii") else "no")
            st.markdown("**Entities:**")
            types = scan_result.get("entity_types") or []
            if types:
                for t in types:
                    st.markdown(f"- `{t}`")
            else:
                st.caption("none")

    # ── Step 2 ─────────────────────────────────────────────────
    st.markdown("### Step 2 · Ingest and search")
    st.caption(
        "The ingestion pipeline pseudonymises the record, stores the original in the "
        "`pii_vault` schema (PostgreSQL, RLS), and writes the masked text to Qdrant "
        "as the `confidential` / `customer_data` category."
    )

    cols = st.columns([1, 1])
    with cols[0]:
        if st.button("Ingest record", key="pii_ingest"):
            demo_id = f"demo-vault-{uuid.uuid4().hex[:8]}"
            try:
                result = ingestion.ingest(
                    text_area,
                    collection="pb_general",
                    classification="confidential",
                    project="novatech-sales",
                    metadata={
                        "title": f"Demo customer — {demo_id}",
                        "seed_id": demo_id,
                        "type": "customer-record",
                        "data_category": "customer_data",
                        # OPA privacy policy requires a legal basis for
                        # confidential ingestion; the demo uses a synthetic
                        # record so "legitimate_interest" is the right default.
                        "legal_basis": "legitimate_interest",
                    },
                )
                st.session_state["pii_ingest_result"] = result
                # Keyword search — Qdrant holds the pseudonymised text,
                # so matching on the name itself is intentionally unreliable.
                st.session_state["pii_search_key"] = "Kundenprofil"
            except Exception as exc:  # noqa: BLE001
                st.error(f"Ingest failed: {exc}")

    with cols[1]:
        search_term = st.text_input(
            "Search term",
            value=st.session_state.get("pii_search_key", "Kundenprofil"),
            key="pii_search_box",
            help=(
                "Qdrant stores the pseudonymised chunk, so searching for the "
                "original name will often miss. Use a structural keyword like "
                "'Kundenprofil' to locate the record, then reveal the "
                "original via the vault token."
            ),
        )

    ingest_result = st.session_state.get("pii_ingest_result")
    if ingest_result:
        st.success(
            f"Ingested: status={ingest_result.get('status')}, "
            f"chunks={ingest_result.get('chunks_ingested', 0)}, "
            f"doc_id={ingest_result.get('document_id', '—')}"
        )

    if st.button("Search with analyst key (no vault token)", key="pii_search"):
        try:
            resp = mcp.search_knowledge(
                search_term or "Kundenprofil",
                api_key=analyst_key,
                top_k=3,
            )
            st.session_state["pii_search_result"] = resp.model_dump()
        except DemoOutOfDateError as exc:
            st.error(f"Demo out of date: {exc}")
        except Exception as exc:  # noqa: BLE001
            st.error(f"Search failed: {exc}")

    sresult = st.session_state.get("pii_search_result")
    if sresult:
        st.caption(
            f"{sresult.get('total', 0)} hit(s) — content is the pseudonymised chunk "
            "stored in Qdrant."
        )
        for i, item in enumerate(sresult.get("results", []), 1):
            with st.container(border=True):
                title = item["metadata"].get("title") or item.get("id")
                st.markdown(f"**{i}. {title}**")
                st.code((item.get("content") or "").strip(), language="text")
                _render_metadata_preview(item["metadata"])

    # ── Step 3 ─────────────────────────────────────────────────
    st.markdown("### Step 3 · Reveal with a purpose-bound vault token")
    st.caption(
        "The token carries `purpose=support`, `data_category=customer_data`, and an "
        "expiry 10 minutes out. OPA (`pb.privacy.vault_access_allowed`) validates "
        "the signature, the purpose binding, and the role. Every access is written to "
        "the audit log."
    )

    purpose = st.selectbox(
        "Purpose",
        options=["support", "billing", "contract_fulfillment"],
        index=0,
        key="pii_purpose",
        help="Must match one of the purposes allowed for customer_data in OPA data.json.",
    )

    if st.button("Reveal (with vault token)", key="pii_reveal"):
        try:
            token = build_vault_token(
                purpose=purpose,
                data_category="customer_data",
                ttl_minutes=10,
            )
            st.session_state["pii_vault_token"] = {
                "purpose": token["purpose"],
                "expires_at": token["expires_at"],
                "signature": token["signature"][:16] + "…",  # preview only
            }
            resp = mcp.search_knowledge(
                search_term or "Kundenprofil",
                api_key=analyst_key,
                top_k=3,
                pii_access_token=token,
                purpose=purpose,
            )
            st.session_state["pii_reveal_result"] = resp.model_dump()
        except Exception as exc:  # noqa: BLE001
            st.error(f"Reveal failed: {exc}")

    token_preview = st.session_state.get("pii_vault_token")
    if token_preview:
        st.markdown("**Issued token (signature truncated):**")
        st.json(token_preview)

    reveal = st.session_state.get("pii_reveal_result")
    if reveal:
        any_unlocked = any(it.get("vault_access") for it in reveal.get("results", []))
        if not any_unlocked:
            st.warning(
                "None of the hits had a vault_ref or the policy denied access. "
                "Try ingesting a fresh record above (Step 2), or pick a different purpose."
            )
        for i, item in enumerate(reveal.get("results", []), 1):
            with st.container(border=True):
                title = item["metadata"].get("title") or item.get("id")
                unlocked = item.get("vault_access", False)
                badge = "🔓 unlocked" if unlocked else "🔒 pseudonymous"
                st.markdown(f"**{i}. {title}** &nbsp; `{badge}`")
                if unlocked and item.get("original_content"):
                    st.markdown("_Original (vault, redacted per purpose):_")
                    st.code(item["original_content"], language="text")
                st.markdown("_Qdrant payload:_")
                st.code((item.get("content") or "").strip(), language="text")

    # ── Audit ──────────────────────────────────────────────────
    with st.expander("Show recent audit entries for this agent"):
        try:
            data = mcp.query_data(
                "agent_access_log",
                api_key=admin_key,
                conditions={"agent_id": "demo-analyst"},
                limit=5,
            )
            rows = data.get("results") or data.get("rows") or []
            if rows:
                st.dataframe(rows, use_container_width=True)
            else:
                st.info("No entries yet — perform a search or reveal above.")
        except Exception as exc:  # noqa: BLE001
            st.caption(f"Audit preview unavailable: {exc}")
