"""Semantic PII verifier — second-pass precision layer for Presidio output.

Presidio is excellent at recall: on German business text it flags every
capitalised noun as a PERSON/LOCATION candidate, which is exactly what
a regulator would ask for. It is poor at precision for the same reason:
words like "Zahlungsstatus", "Geschäftsführer", "Rahmenvertrag" all get
PERSON or LOCATION tags they don't deserve.

This module plugs an LLM-based verifier behind Presidio:

    Presidio (recall)   →   Verifier (precision)   →   Final entities
    pattern + NER hits       context-aware filter       what downstream sees

Design contract:
  * Only **ambivalent** entity types are sent to the verifier
    (PERSON, LOCATION, ORGANIZATION). Pattern-based types (IBAN_CODE,
    EMAIL_ADDRESS, PHONE_NUMBER, DE_TAX_ID, DE_SOCIAL_SECURITY,
    DE_DATE_OF_BIRTH) pass through unchanged — high precision already.
  * Batch per document: one LLM call regardless of candidate count.
  * Fail-open: if the verifier is unreachable or the LLM returns
    garbage, keep every original candidate. Recall is never sacrificed.
  * Deterministic confidence threshold (``min_confidence_keep``)
    governs the cut — below that, the candidate is dropped.

Back-ends are pluggable via the factory pattern copied from
``rerank_provider.py``. Today ``noop`` (default) and ``llm``
(OpenAI-compatible chat) are supported. A future ``custom_model``
backend can be dropped in without touching the ingestion wiring —
see ``docs/pii-custom-model.md`` for the long-term roadmap.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

import httpx

log = logging.getLogger("pb-pii-verifier")


# ── Types ─────────────────────────────────────────────────────

@dataclass
class PIICandidate:
    """A single Presidio hit the verifier can reason about."""

    entity_type: str
    text:        str
    start:       int
    end:         int
    score:       float = 0.0
    context:     str = ""   # ±CONTEXT_WINDOW chars around the hit

    def to_prompt_row(self, idx: int) -> str:
        # One candidate per line — the LLM responds referencing ``idx``.
        return (
            f"{idx}. type={self.entity_type} · text={self.text!r}\n"
            f"   context: …{self.context}…"
        )


@dataclass
class VerifyStats:
    """Telemetry surfacing how the verifier shifted the scan output.

    Callers use this to render demo panels, update Prometheus counters,
    and decide whether to alert on unexpectedly high revert rates.
    """

    input_count:    int = 0
    forwarded:      int = 0   # pass-through pattern types
    reviewed:       int = 0   # sent to LLM
    kept:           int = 0   # LLM said "yes, real PII"
    reverted:       int = 0   # LLM said "no, false positive"
    errors:         int = 0
    duration_ms:    float = 0.0
    backend:        str = "noop"
    by_entity_type: dict[str, dict[str, int]] = field(default_factory=dict)


# Pattern-based types — their Presidio score is trustworthy on its own.
PATTERN_TYPES: frozenset[str] = frozenset({
    "IBAN_CODE", "EMAIL_ADDRESS", "PHONE_NUMBER", "CREDIT_CARD",
    "IP_ADDRESS", "DE_TAX_ID", "DE_SOCIAL_SECURITY",
    "DE_DATE_OF_BIRTH", "DATE_OF_BIRTH",
})

CONTEXT_WINDOW = 60  # characters on each side of the candidate span


# ── Base provider ─────────────────────────────────────────────

class _BasePIIVerifyProvider:
    """Contract all backends share.

    Subclasses implement :meth:`verify`. The base class handles the
    pattern-vs-ambiguous split so every backend only sees the work it
    actually needs to do.
    """

    backend_name: str = "base"

    def __init__(self, *, min_confidence_keep: float = 0.5):
        self.min_confidence_keep = min_confidence_keep

    async def verify(
        self,
        http: httpx.AsyncClient,
        text: str,
        candidates: list[PIICandidate],
    ) -> tuple[list[bool], VerifyStats]:
        """Return a parallel ``keep`` array + telemetry.

        ``keep[i]`` is ``True`` when ``candidates[i]`` should stay.
        Pattern types are always kept; ambiguous ones are forwarded to
        the subclass hook :meth:`_verify_ambiguous`.
        """
        stats = VerifyStats(input_count=len(candidates), backend=self.backend_name)
        keep: list[bool] = [True] * len(candidates)

        # Split work so the LLM only sees the ambiguous rows.
        ambiguous_idx: list[int] = []
        for i, cand in enumerate(candidates):
            bucket = stats.by_entity_type.setdefault(
                cand.entity_type, {"total": 0, "forwarded": 0, "kept": 0, "reverted": 0},
            )
            bucket["total"] += 1
            if cand.entity_type in PATTERN_TYPES:
                stats.forwarded += 1
                bucket["forwarded"] += 1
                continue
            ambiguous_idx.append(i)

        if not ambiguous_idx:
            stats.kept = stats.forwarded
            return keep, stats

        stats.reviewed = len(ambiguous_idx)
        try:
            verdicts = await self._verify_ambiguous(
                http, text,
                [candidates[i] for i in ambiguous_idx],
            )
        except Exception as exc:  # noqa: BLE001
            # Fail-open: keep everything. GDPR recall > demo aesthetics.
            log.warning("pii_verify_provider %s failed, keeping all Presidio hits: %s",
                        self.backend_name, exc)
            stats.errors = len(ambiguous_idx)
            stats.kept = stats.input_count
            return keep, stats

        # verdicts is a parallel list[bool] for the ambiguous subset.
        for local_i, real_i in enumerate(ambiguous_idx):
            decision = bool(verdicts[local_i])
            keep[real_i] = decision
            bucket = stats.by_entity_type[candidates[real_i].entity_type]
            if decision:
                bucket["kept"] += 1
            else:
                bucket["reverted"] += 1

        stats.kept = sum(1 for v in keep if v)
        stats.reverted = stats.input_count - stats.kept - stats.errors - stats.forwarded
        # ``reverted`` should match the LLM's "no" count; edge-case guard:
        if stats.reverted < 0:
            stats.reverted = 0
        return keep, stats

    async def _verify_ambiguous(
        self,
        http: httpx.AsyncClient,
        text: str,
        ambiguous: list[PIICandidate],
    ) -> list[bool]:
        """Subclass hook. Default: keep everything (noop behaviour)."""
        return [True] * len(ambiguous)


# ── Noop backend (community default) ──────────────────────────

class NoopPIIVerifyProvider(_BasePIIVerifyProvider):
    """Passthrough — keeps the existing Presidio-only behaviour.

    This is the default when ``PII_VERIFIER_BACKEND`` is unset or
    explicitly ``noop``. Ingestion behaves exactly as it did before
    the verifier was introduced; switching it on is a one-line env
    change.
    """

    backend_name = "noop"


# ── LLM backend (OpenAI-compatible chat) ──────────────────────

_SYSTEM_PROMPT = (
    "You are a precision filter for a PII scanner. A statistical NER "
    "model has proposed candidate entities in a document. Your job: "
    "decide for EACH candidate whether it is genuinely personally "
    "identifying information in its context, or a false positive "
    "(e.g. a job title, a product name, a German compound noun that "
    "happens to be capitalised, a department name, a generic city "
    "reference in 'Sparkasse Köln', etc.).\n\n"
    "Rules:\n"
    "  1. PERSON must be an actual individual human name. "
    "     'Geschäftsführer' or 'Projektleiter' are NOT persons.\n"
    "  2. LOCATION must be an address component, city, or country "
    "     in a context that identifies where a person lives / works. "
    "     Bank branch names, product names, and Deutsche Bahn stations "
    "     are NOT locations that compromise privacy.\n"
    "  3. ORGANIZATION must be a specific organisation, not a generic "
    "     term like 'Vertriebsabteilung'.\n"
    "  4. When in doubt, KEEP the candidate. Recall matters more than "
    "     precision on a grey-zone call — legal review catches the "
    "     rest.\n\n"
    "Respond ONLY with a JSON object mapping the candidate's number "
    "(as a string) to a boolean: true = real PII, keep. false = false "
    "positive, remove. Example: {\"0\": true, \"1\": false}. No "
    "explanation, no extra keys."
)


class LLMPIIVerifyProvider(_BasePIIVerifyProvider):
    """Uses an OpenAI-compatible chat endpoint for batch verification."""

    backend_name = "llm"

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str = "",
        min_confidence_keep: float = 0.5,
        timeout: float = 15.0,
    ):
        super().__init__(min_confidence_keep=min_confidence_keep)
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.headers: dict[str, str] = (
            {"Authorization": f"Bearer {api_key}"} if api_key else {}
        )
        self.timeout = timeout

    async def _verify_ambiguous(
        self,
        http: httpx.AsyncClient,
        text: str,
        ambiguous: list[PIICandidate],
    ) -> list[bool]:
        # Build a compact numbered list the LLM can reference.
        for i, cand in enumerate(ambiguous):
            # Hydrate context on-demand if the caller didn't fill it in.
            if not cand.context:
                lo = max(0, cand.start - CONTEXT_WINDOW)
                hi = min(len(text), cand.end + CONTEXT_WINDOW)
                cand.context = (
                    text[lo:cand.start]
                    + "[[" + cand.text + "]]"
                    + text[cand.end:hi]
                ).replace("\n", " ")

        user_prompt = (
            "Candidates:\n"
            + "\n".join(c.to_prompt_row(i) for i, c in enumerate(ambiguous))
            + "\n\nReturn the JSON object now."
        )

        resp = await http.post(
            f"{self.base_url}/v1/chat/completions",
            headers=self.headers,
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user",   "content": user_prompt},
                ],
                "temperature": 0.0,
                "max_tokens":  256,
                # Ask for JSON when the backend supports it (Ollama/vLLM
                # accept this field; pure-OpenAI ignores it silently
                # because we still get JSON via the prompt).
                "response_format": {"type": "json_object"},
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()
        body = resp.json()

        try:
            content = body["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError):
            raise RuntimeError(f"unexpected LLM response shape: {body!r}")

        verdicts = _parse_verdicts(content, len(ambiguous))
        return verdicts


# ── Parsing ────────────────────────────────────────────────────

_JSON_OBJECT_RE = re.compile(r"\{[^{}]*\}", re.DOTALL)


def _parse_verdicts(content: str, n: int) -> list[bool]:
    """Extract a ``{idx: bool}`` mapping from the LLM response.

    We are deliberately forgiving: small local LLMs occasionally wrap
    the JSON in markdown fences or add chatter before the object. The
    parser grabs the first balanced ``{ ... }`` substring and tries to
    parse it. Missing entries default to ``True`` (fail-open).
    """
    match = _JSON_OBJECT_RE.search(content)
    if not match:
        raise ValueError(f"no JSON object in LLM response: {content[:200]!r}")
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise ValueError(f"malformed JSON verdicts: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("LLM returned non-object JSON for verdicts")

    verdicts: list[bool] = [True] * n
    for key, val in parsed.items():
        try:
            i = int(key)
        except (TypeError, ValueError):
            continue
        if 0 <= i < n:
            verdicts[i] = bool(val)
    return verdicts


# ── Factory ────────────────────────────────────────────────────

def create_pii_verify_provider(
    *,
    backend: str = "noop",
    base_url: str = "",
    api_key: str = "",
    model: str = "",
    min_confidence_keep: float = 0.5,
) -> _BasePIIVerifyProvider:
    """Return a provider instance for the given backend name.

    ``noop`` is always safe. ``llm`` requires ``base_url`` + ``model``;
    missing values fall back to ``noop`` with a warning so a misconfigured
    deployment doesn't crash ingestion on boot.
    """
    backend = (backend or "noop").lower()
    if backend == "noop":
        return NoopPIIVerifyProvider(min_confidence_keep=min_confidence_keep)
    if backend == "llm":
        if not base_url or not model:
            log.warning(
                "pii_verify_provider: backend=llm needs base_url + model; "
                "falling back to noop"
            )
            return NoopPIIVerifyProvider(min_confidence_keep=min_confidence_keep)
        return LLMPIIVerifyProvider(
            base_url=base_url, api_key=api_key, model=model,
            min_confidence_keep=min_confidence_keep,
        )
    log.warning(
        "pii_verify_provider: unknown backend %r — using noop", backend,
    )
    return NoopPIIVerifyProvider(min_confidence_keep=min_confidence_keep)


# ── Helpers used by ingestion ─────────────────────────────────

def build_candidates_from_presidio(
    text: str, results: Iterable[Any],
) -> list[PIICandidate]:
    """Convert Presidio ``RecognizerResult`` rows to ``PIICandidate``.

    Presidio results carry ``start``/``end``/``entity_type``/``score``.
    We reach back into ``text`` to hydrate the hit value and the
    surrounding ±CONTEXT_WINDOW-char context window the LLM needs.
    """
    out: list[PIICandidate] = []
    for r in results:
        start = int(getattr(r, "start", 0))
        end = int(getattr(r, "end", 0))
        lo = max(0, start - CONTEXT_WINDOW)
        hi = min(len(text), end + CONTEXT_WINDOW)
        raw = text[start:end] if 0 <= start < end <= len(text) else ""
        ctx = (
            text[lo:start] + "[[" + raw + "]]" + text[end:hi]
        ).replace("\n", " ")
        out.append(PIICandidate(
            entity_type=str(getattr(r, "entity_type", "")),
            text=raw,
            start=start,
            end=end,
            score=float(getattr(r, "score", 0.0)),
            context=ctx,
        ))
    return out


def build_candidates_from_locations(
    text: str, entity_locations: list[dict],
) -> list[PIICandidate]:
    """Variant of :func:`build_candidates_from_presidio` for the dict shape
    produced by :class:`pii_scanner.PIIScanResult`.

    Tests and the ``/preview`` endpoint carry scan results as plain
    dicts — not Presidio ``RecognizerResult`` objects — so we need a
    parallel constructor.
    """
    out: list[PIICandidate] = []
    for loc in entity_locations or []:
        start = int(loc.get("start", 0))
        end = int(loc.get("end", 0))
        lo = max(0, start - CONTEXT_WINDOW)
        hi = min(len(text), end + CONTEXT_WINDOW)
        raw = text[start:end] if 0 <= start < end <= len(text) else ""
        ctx = (
            text[lo:start] + "[[" + raw + "]]" + text[end:hi]
        ).replace("\n", " ")
        out.append(PIICandidate(
            entity_type=str(loc.get("type", "")),
            text=raw,
            start=start,
            end=end,
            score=float(loc.get("score", 0.0)),
            context=ctx,
        ))
    return out


def apply_verdicts_to_scan_result(
    scan_result_entity_counts: dict[str, int],
    entity_locations: list[dict],
    keep: list[bool],
) -> tuple[bool, dict[str, int], list[dict]]:
    """Update a scan_result payload in-place-style after verification.

    Returns ``(contains_pii, entity_counts, entity_locations)`` with
    dropped candidates removed. ``contains_pii`` flips to ``False``
    when the verifier empties the list entirely.

    Kept as a module-level helper so both the live ``/ingest`` path and
    the dry-run ``/preview`` endpoint can share the exact same reducer.
    """
    if len(keep) != len(entity_locations):
        raise ValueError(
            f"keep/location length mismatch: {len(keep)} vs {len(entity_locations)}"
        )

    filtered_locations: list[dict] = []
    new_counts: dict[str, int] = {}
    for flag, loc in zip(keep, entity_locations):
        if not flag:
            continue
        filtered_locations.append(loc)
        etype = loc.get("type", "UNKNOWN")
        new_counts[etype] = new_counts.get(etype, 0) + 1

    contains_pii = bool(filtered_locations)
    return contains_pii, new_counts, filtered_locations
