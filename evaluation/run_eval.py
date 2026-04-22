"""
Offline-Evaluator (Baustein 3: Evaluation + Feedback-Loop)
===========================================================
Evaluiert Retrieval-Qualität gegen ein vordefiniertes Testset und
speichert Ergebnisse in `eval_runs`. Vergleicht mit dem letzten Run
und erzeugt einen Regression-Alert bei >10% Verschlechterung.

Verwendung:
  python run_eval.py                    # Evaluierung starten
  python run_eval.py --dry-run          # Nur ausgeben, nicht speichern
  python run_eval.py --collection code  # Nur eine Collection

Als Cronjob (wöchentlich via cron oder docker exec):
  0 3 * * 0  docker exec pb-ingestion python /app/evaluation/run_eval.py
"""

import os
import sys
import json
import time
import asyncio
import logging
from dataclasses import dataclass, field, asdict
from typing import Any

import httpx
import asyncpg

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from shared.config import build_postgres_url
from shared.opa_client import OpaPolicyMissingError, opa_query

log = logging.getLogger("pb-eval")

# ── Konfiguration ────────────────────────────────────────────
POSTGRES_URL = build_postgres_url()
MCP_BASE_URL = os.getenv("MCP_EVAL_URL", "http://mcp-server:8080")   # Direkt gegen Ingestion/Qdrant
QDRANT_URL   = os.getenv("QDRANT_URL",   "http://qdrant:6333")
RERANKER_URL = os.getenv("RERANKER_URL", "http://reranker:8082")
OPA_URL      = os.getenv("OPA_URL",      "http://opa:8181")

# ── Backward-compat fallback ──
_OLLAMA_URL = os.getenv("OLLAMA_URL", "http://ollama:11434")

# ── Embedding provider ──
EMBEDDING_PROVIDER_URL = os.getenv("EMBEDDING_PROVIDER_URL", _OLLAMA_URL)
EMBEDDING_MODEL        = os.getenv("EMBEDDING_MODEL", "nomic-embed-text")
EMBEDDING_API_KEY      = os.getenv("EMBEDDING_API_KEY", "")

import sys as _sys
_sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from shared.llm_provider import EmbeddingProvider

_embedding_provider = EmbeddingProvider(
    base_url=EMBEDDING_PROVIDER_URL, api_key=EMBEDDING_API_KEY
)

EVAL_AGENT_ID      = "eval-bot"
EVAL_AGENT_ROLE    = "analyst"
OVERSAMPLE_FACTOR  = 5
TOP_K              = 10

REGRESSION_THRESHOLD = 0.10   # 10% Verschlechterung → Alert


# ── Datenklassen ─────────────────────────────────────────────

@dataclass
class QueryResult:
    query: str
    collection: str
    category: str | None
    expected_ids: list[str]
    expected_keywords: list[str]
    returned_ids: list[str]
    precision: float
    recall: float
    mrr: float                  # Mean Reciprocal Rank
    keyword_coverage: float
    latency_ms: float


@dataclass
class EvalReport:
    test_count: int
    avg_precision: float
    avg_recall: float
    avg_mrr: float
    avg_latency_ms: float
    per_query: list[dict] = field(default_factory=list)
    config: dict = field(default_factory=dict)


# ── Metriken ─────────────────────────────────────────────────

def precision_at_k(returned: list[str], expected: list[str]) -> float:
    if not returned or not expected:
        return 0.0
    hits = sum(1 for r in returned if r in expected)
    return hits / len(returned)


def recall_at_k(returned: list[str], expected: list[str]) -> float:
    if not expected:
        return 1.0  # kein Ground Truth → kein Recall messbar, als ok werten
    hits = sum(1 for r in returned if r in expected)
    return hits / len(expected)


def reciprocal_rank(returned: list[str], expected: list[str]) -> float:
    for i, r in enumerate(returned, start=1):
        if r in expected:
            return 1.0 / i
    return 0.0


def keyword_coverage(returned_texts: list[str], expected_keywords: list[str]) -> float:
    if not expected_keywords:
        return 1.0
    combined = " ".join(returned_texts).lower()
    found = sum(1 for kw in expected_keywords if kw.lower() in combined)
    return found / len(expected_keywords)


# ── Embedding + Suche ─────────────────────────────────────────

async def embed_text(client: httpx.AsyncClient, text: str) -> list[float]:
    return await _embedding_provider.embed(client, text, EMBEDDING_MODEL)


_opa_access_cache: dict[str, bool] = {}

async def check_opa_access(client: httpx.AsyncClient,
                           classification: str) -> bool:
    """Check OPA policy for eval agent access to a classification level."""
    if classification in _opa_access_cache:
        return _opa_access_cache[classification]
    try:
        raw = await opa_query(
            client, OPA_URL, "pb/access/allow",
            {
                "agent_id": EVAL_AGENT_ID,
                "agent_role": EVAL_AGENT_ROLE,
                "resource": "eval/search",
                "classification": classification,
                "action": "read",
            },
        )
        result = bool(raw)
        _opa_access_cache[classification] = result
        return result
    except OpaPolicyMissingError as exc:
        log.error("OPA policy %s not loaded — denying eval access", exc.package_path)
        return False
    except Exception as e:
        log.warning("OPA check failed, denying access: %s", e)
        return False


async def search(client: httpx.AsyncClient, query: str, collection: str) -> tuple[list[str], list[str], float]:
    """
    Führt vollständige Suche durch (Embed → Qdrant → Reranker).
    Gibt (result_ids, result_texts, latency_ms) zurück.
    """
    t0 = time.perf_counter()

    vector = await embed_text(client, query)

    # Qdrant-Suche
    resp = await client.post(
        f"{QDRANT_URL}/collections/{collection}/points/search",
        json={"vector": vector, "limit": TOP_K * OVERSAMPLE_FACTOR, "with_payload": True},
    )
    resp.raise_for_status()
    hits = resp.json().get("result", [])

    documents = [
        {"id": str(h["id"]), "content": h["payload"].get("content", ""), "score": h["score"],
         "metadata": {k: v for k, v in h["payload"].items() if k != "content"}}
        for h in hits
    ]

    # OPA policy filter — remove documents the eval agent may not access
    filtered = []
    for doc in documents:
        classification = doc["metadata"].get("classification", "internal")
        allowed = await check_opa_access(client, classification)
        if allowed:
            filtered.append(doc)
        else:
            log.debug(f"OPA denied eval access to {doc['id']} ({classification})")
    documents = filtered

    # Reranking
    try:
        rr = await client.post(f"{RERANKER_URL}/rerank", json={
            "query": query, "documents": documents, "top_n": TOP_K, "return_scores": True
        })
        rr.raise_for_status()
        reranked = rr.json()["results"]
        result_ids   = [r["id"] for r in reranked]
        result_texts = [r["content"] for r in reranked]
    except Exception:
        result_ids   = [d["id"] for d in documents[:TOP_K]]
        result_texts = [d["content"] for d in documents[:TOP_K]]

    latency_ms = (time.perf_counter() - t0) * 1000
    return result_ids, result_texts, latency_ms


# ── Evaluierung ───────────────────────────────────────────────

async def run_evaluation(dry_run: bool = False,
                         collection_filter: str | None = None) -> EvalReport:
    pool = await asyncpg.create_pool(POSTGRES_URL, min_size=1, max_size=3)

    async with httpx.AsyncClient(timeout=60.0) as client:
        # Testset laden
        q = "SELECT * FROM eval_test_set"
        params: list[Any] = []
        if collection_filter:
            q += " WHERE collection = $1"
            params.append(collection_filter)
        q += " ORDER BY id"

        test_cases = await pool.fetch(q, *params)
        log.info(f"Starte Evaluierung mit {len(test_cases)} Testfällen")

        results: list[QueryResult] = []

        for tc in test_cases:
            query      = tc["query"]
            collection = tc["collection"] or "pb_general"
            exp_ids    = tc["expected_ids"] or []
            exp_kws    = tc["expected_keywords"] or []
            category   = tc["category"]

            try:
                returned_ids, returned_texts, latency_ms = await search(
                    client, query, collection
                )
            except Exception as e:
                log.warning(f"Suche fehlgeschlagen für '{query[:60]}': {e}")
                continue

            qr = QueryResult(
                query=query,
                collection=collection,
                category=category,
                expected_ids=exp_ids,
                expected_keywords=exp_kws,
                returned_ids=returned_ids,
                precision=precision_at_k(returned_ids, exp_ids),
                recall=recall_at_k(returned_ids, exp_ids),
                mrr=reciprocal_rank(returned_ids, exp_ids),
                keyword_coverage=keyword_coverage(returned_texts, exp_kws),
                latency_ms=round(latency_ms, 1),
            )
            results.append(qr)
            log.info(
                f"[{category or '-'}] P={qr.precision:.2f} R={qr.recall:.2f} "
                f"MRR={qr.mrr:.2f} KW={qr.keyword_coverage:.2f} "
                f"({latency_ms:.0f}ms) — {query[:60]}"
            )

        if not results:
            log.error("Keine Ergebnisse — Testset leer oder alle Suchen fehlgeschlagen")
            await pool.close()
            return EvalReport(0, 0, 0, 0, 0)

        n = len(results)
        report = EvalReport(
            test_count=n,
            avg_precision=round(sum(r.precision for r in results) / n, 4),
            avg_recall=round(sum(r.recall for r in results) / n, 4),
            avg_mrr=round(sum(r.mrr for r in results) / n, 4),
            avg_latency_ms=round(sum(r.latency_ms for r in results) / n, 1),
            per_query=[asdict(r) for r in results],
            config={
                "embedding_model": EMBEDDING_MODEL,
                "top_k": TOP_K,
                "oversample_factor": OVERSAMPLE_FACTOR,
                "collection_filter": collection_filter,
                "reranker_url": RERANKER_URL,
            },
        )

        log.info(
            f"\n{'='*60}\n"
            f"EVAL ERGEBNIS ({n} Queries)\n"
            f"  Precision:  {report.avg_precision:.4f}\n"
            f"  Recall:     {report.avg_recall:.4f}\n"
            f"  MRR:        {report.avg_mrr:.4f}\n"
            f"  Latenz:     {report.avg_latency_ms:.1f}ms\n"
            f"{'='*60}"
        )

        if not dry_run:
            # Ergebnis speichern
            row = await pool.fetchrow("""
                INSERT INTO eval_runs (test_count, avg_precision, avg_recall, avg_mrr, avg_latency_ms, details, config)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING id
            """, n, report.avg_precision, report.avg_recall, report.avg_mrr,
                report.avg_latency_ms,
                json.dumps(report.per_query),
                json.dumps(report.config))

            log.info(f"Eval-Run in DB gespeichert (ID={row['id']})")

            # Regression-Check: vorherigen Run laden
            prev = await pool.fetchrow("""
                SELECT avg_precision, avg_recall, avg_mrr
                FROM eval_runs
                WHERE id != $1
                ORDER BY run_date DESC
                LIMIT 1
            """, row["id"])

            if prev:
                for metric, curr_val, prev_val in [
                    ("precision", report.avg_precision, float(prev["avg_precision"])),
                    ("recall",    report.avg_recall,    float(prev["avg_recall"])),
                    ("mrr",       report.avg_mrr,       float(prev["avg_mrr"])),
                ]:
                    if prev_val > 0 and (prev_val - curr_val) / prev_val > REGRESSION_THRESHOLD:
                        log.warning(
                            f"[REGRESSION] {metric.upper()} verschlechtert: "
                            f"{prev_val:.4f} → {curr_val:.4f} "
                            f"({(prev_val - curr_val) / prev_val * 100:.1f}%)"
                        )

        await pool.close()
        return report


# ── CLI ───────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    dry_run    = "--dry-run" in sys.argv
    collection = None
    if "--collection" in sys.argv:
        idx = sys.argv.index("--collection")
        collection = sys.argv[idx + 1]

    report = asyncio.run(run_evaluation(dry_run=dry_run, collection_filter=collection))
    print(json.dumps({
        "test_count":     report.test_count,
        "avg_precision":  report.avg_precision,
        "avg_recall":     report.avg_recall,
        "avg_mrr":        report.avg_mrr,
        "avg_latency_ms": report.avg_latency_ms,
    }, indent=2))
