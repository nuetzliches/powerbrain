"""Tab E — MCP vs Proxy (Community vs Enterprise).

Shows the same customer-related question answered through two paths
side by side:

* **Left / community** — direct MCP ``search_knowledge`` call. Returns
  raw hits with pseudonyms; the downstream application (or the user) has
  to interpret them.
* **Right / enterprise** — pb-proxy ``/v1/chat/completions`` call. The
  proxy injects MCP tools, runs the agent loop, calls
  ``/vault/resolve`` on tool results (OPA-gated, purpose-bound), and
  hands a natural-language answer back with resolved PII.

The panel is only rendered when a pb-proxy is reachable — otherwise it
shows an informational banner instead.
"""
from __future__ import annotations

import json
import os

import streamlit as st

from mcp_client import (
    DemoOutOfDateError,
    _MCPClient,
    _ProxyClient,
)


PROXY_MODEL = os.environ.get("PROXY_MODEL", "qwen-local")

SUGGESTED_QUESTIONS = [
    "Fasse die Daten zu unserem Kunden Julia Weber zusammen.",
    "Welche Kunden haben im April 2026 Kontakt gehabt?",
    "Wer ist unser wichtigster Kunde laut Umsatz?",
]


def _render_mcp_column(mcp: _MCPClient, query: str, api_key: str) -> None:
    st.markdown("#### MCP (community)")
    st.caption(
        "Direct `search_knowledge` call. Returns stored chunks with "
        "pseudonyms — glue code owns the final formatting."
    )
    try:
        resp = mcp.search_knowledge(query, api_key=api_key, top_k=3)
    except DemoOutOfDateError as exc:
        st.error(f"Demo out of date: {exc}")
        return
    except Exception as exc:  # noqa: BLE001
        st.error(f"MCP search failed: {exc}")
        return

    st.caption(f"{resp.total} result(s) from the pipeline")
    if resp.total == 0:
        st.info("No matches for this query.")
        return
    for i, item in enumerate(resp.results, 1):
        title = item.metadata.get("title") or item.id
        with st.container(border=True):
            st.markdown(f"**{i}. {title}**")
            snippet = (item.content or "").strip()
            if len(snippet) > 400:
                snippet = snippet[:400] + "…"
            if snippet:
                st.code(snippet, language="text")


def _render_proxy_column(
    proxy: _ProxyClient,
    query: str,
    api_key: str,
    purpose: str,
) -> None:
    st.markdown("#### pb-proxy (enterprise)")
    st.caption(
        "Chat-native path: pb-proxy orchestrates the LLM + MCP tool "
        "injection and resolves vault pseudonyms per purpose. Client "
        "sees a finished answer."
    )
    with st.spinner("Proxy is working (tool-call → LLM → vault resolve) ..."):
        try:
            body = proxy.chat(
                model=PROXY_MODEL,
                messages=[
                    {"role": "system", "content": (
                        "Du bist ein hilfreicher Assistent. Nutze die "
                        "bereitgestellten MCP-Tools, insbesondere "
                        "search_knowledge, um die Frage des Users zu "
                        "beantworten. Antworte auf Deutsch."
                    )},
                    {"role": "user", "content": query},
                ],
                api_key=api_key,
                purpose=purpose,
            )
        except Exception as exc:  # noqa: BLE001
            st.error(f"Proxy call failed: {exc}")
            return

    if "_error" in body:
        st.error(body["_error"])
        return
    if "error" in body and "choices" not in body:
        st.error(body["error"])
        return

    vault_info = (body.get("_proxy") or {}).get("vault_resolutions")
    if vault_info:
        resolved = vault_info.get("resolved", 0)
        total = vault_info.get("total", 0)
        st.success(
            f"Vault resolution: {resolved}/{total} pseudonyms "
            f"resolved for purpose=`{vault_info.get('purpose')}`"
        )
    else:
        st.caption("No pseudonyms encountered in tool output.")

    for choice in body.get("choices", []):
        content = choice.get("message", {}).get("content") or ""
        with st.container(border=True):
            st.markdown(content or "_(empty response)_")

    telemetry = body.get("_telemetry") or {}
    if telemetry:
        with st.expander("Proxy telemetry"):
            st.json(telemetry, expanded=False)


def render(
    mcp: _MCPClient,
    proxy: _ProxyClient,
    analyst_key: str,
) -> None:
    st.subheader("MCP vs Proxy — same question, two editions")
    st.write(
        "Direct MCP gives you the policy-compliant data layer. "
        "Adding pb-proxy on top turns that into a chat-native, vault-"
        "resolved experience — the community/enterprise split."
    )

    if not proxy.available():
        st.warning(
            "pb-proxy is not reachable at the configured URL — this tab "
            "only renders when the `demo` profile (or `proxy` profile) "
            "is up. Start with:\n\n"
            "```\n./scripts/quickstart.sh --demo\n```"
        )
        return

    purpose = st.selectbox(
        "Purpose (forwarded to pb-proxy via X-Purpose header)",
        options=["support", "billing", "contract_fulfillment"],
        index=0,
        help=(
            "OPA's pii_resolve_tool_results policy gates whether the "
            "proxy will call /vault/resolve for tool-call output. The "
            "purpose also drives fields_to_redact — billing keeps IBANs "
            "visible, support redacts them."
        ),
    )

    default_query = SUGGESTED_QUESTIONS[0]
    st.markdown("**Suggested questions:**")
    sug_cols = st.columns(len(SUGGESTED_QUESTIONS))
    for col, q in zip(sug_cols, SUGGESTED_QUESTIONS):
        with col:
            if st.button(q[:28] + ("…" if len(q) > 28 else ""), key=f"mvp_{q}",
                         use_container_width=True):
                st.session_state["mvp_query"] = q
                st.session_state["mvp_run_now"] = True
                st.rerun()

    with st.form("mvp_form"):
        query = st.text_area(
            "Question",
            value=st.session_state.get("mvp_query", default_query),
            height=80,
        )
        submitted = st.form_submit_button("Ask both paths", type="primary")

    run_now = st.session_state.pop("mvp_run_now", False)
    if not (submitted or run_now):
        st.info("Pick a suggestion or type a question and press **Ask both paths**.")
        return

    q = (query or "").strip()
    if not q:
        return

    left, right = st.columns(2, gap="large")
    with left:
        _render_mcp_column(mcp, q, analyst_key)
    with right:
        _render_proxy_column(proxy, q, analyst_key, purpose)

    with st.expander("What just happened?"):
        st.markdown(
            """
            1. **MCP (left)** ran `search_knowledge` directly. The
               retrieved chunks live in Qdrant with pseudonyms — names,
               emails and IBANs show up as `[PERSON:xxx]` and similar
               tags. Consumers have to deal with that themselves.
            2. **Proxy (right)** spoke `/v1/chat/completions`. The proxy:
               * pseudonymised the user message,
               * merged Powerbrain MCP tools into the LLM request,
               * executed the LLM's tool calls against MCP,
               * called `POST /vault/resolve` on the pseudonymised tool
                 output, subject to the OPA rule
                 `pb.proxy.pii_resolve_tool_results_allowed` for this
                 role + purpose,
               * fed the resolved text back to the LLM for the final
                 answer, and depseudonymised anything that came back.
            3. The audit chain (`verify_audit_integrity`) logged every
               vault access. Switching `purpose` changes which fields get
               redacted (see `pb.privacy.vault_fields_to_redact`).
            """
        )
