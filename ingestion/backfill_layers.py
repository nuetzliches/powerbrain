"""
Backfill Script: L0/L1 Context Layers for Existing Data
========================================================
One-shot migration that:
1. Tags all existing Qdrant points (without a ``layer`` field) as L2.
2. Groups them by ``doc_id``.
3. Generates L0 (abstract) and L1 (overview) points per document.
4. Upserts L0/L1 into Qdrant and updates ``documents_meta``.

Idempotent — checks for existing L0/L1 before generating.

Usage:
    python backfill_layers.py                         # all collections
    python backfill_layers.py --dry-run               # report only
    python backfill_layers.py --collection pb_code
    python backfill_layers.py --batch-size 50
"""

import argparse
import asyncio
import logging
import os
import sys
import uuid
from collections import defaultdict
from datetime import datetime, timezone

import httpx
import asyncpg
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Filter,
    FieldCondition,
    IsNullCondition,
    MatchValue,
    PayloadField,
    PointStruct,
)

# ── Path setup for shared/ imports ────────────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from shared.llm_provider import CompletionProvider, EmbeddingProvider
from shared.config import build_postgres_url

# ── Configuration ─────────────────────────────────────────────
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
POSTGRES_URL = build_postgres_url()

_OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
LLM_PROVIDER_URL = os.getenv("LLM_PROVIDER_URL", _OLLAMA_URL)
LLM_MODEL = os.getenv("LLM_MODEL", os.getenv("SUMMARIZATION_MODEL", "qwen2.5:3b"))
LLM_API_KEY = os.getenv("LLM_API_KEY", "")

EMBEDDING_PROVIDER_URL = os.getenv("EMBEDDING_PROVIDER_URL", _OLLAMA_URL)
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "nomic-embed-text")
EMBEDDING_API_KEY = os.getenv("EMBEDDING_API_KEY", "")

ALL_COLLECTIONS = ["pb_general", "pb_code", "pb_rules"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("backfill-layers")

# ── L0/L1 prompts (same as ingestion_api.py) ─────────────────

L0_SYSTEM_PROMPT = (
    "You are a document abstraction engine. Generate a single-sentence abstract "
    "(max 100 tokens) that captures the essence of the document. The abstract must "
    "enable quick relevance assessment. Do not include specific details — only the "
    "topic and scope. Respond with the abstract only, no preamble."
)

L1_SYSTEM_PROMPT = (
    "You are a document overview engine. Generate a structured Markdown overview "
    "(max 500 tokens) that covers:\n"
    "1. What this document is about (1 sentence)\n"
    "2. Key sections/topics as bullet points\n"
    "3. Most important facts or numbers\n"
    "4. What kind of detailed information is available in the full document\n\n"
    "The overview enables an AI agent to decide whether to load the full document. "
    "Respond with the overview only, no preamble. Use Markdown formatting."
)


# ── Generation helpers ────────────────────────────────────────


async def generate_l0(
    http_client: httpx.AsyncClient,
    completion: CompletionProvider,
    chunks: list[str],
    source: str = "",
    classification: str = "",
) -> str | None:
    """Generate L0 abstract from document chunks."""
    try:
        full_text = "\n\n".join(chunks)
        if len(full_text) > 4000:
            full_text = full_text[:4000] + "\n\n[truncated]"
        user_prompt = (
            f"Document source: {source}\n"
            f"Classification: {classification}\n"
            f"Full text (from {len(chunks)} chunks):\n\n{full_text}"
        )
        return await completion.generate(
            http_client,
            model=LLM_MODEL,
            system_prompt=L0_SYSTEM_PROMPT,
            user_prompt=user_prompt,
        )
    except Exception as e:
        log.warning(f"L0 generation failed: {e}")
        return None


async def generate_l1(
    http_client: httpx.AsyncClient,
    completion: CompletionProvider,
    chunks: list[str],
    source: str = "",
    classification: str = "",
) -> str | None:
    """Generate L1 overview from document chunks."""
    try:
        full_text = "\n\n".join(chunks)
        if len(full_text) > 8000:
            full_text = full_text[:8000] + "\n\n[truncated]"
        user_prompt = (
            f"Document source: {source}\n"
            f"Classification: {classification}\n"
            f"Full text (from {len(chunks)} chunks):\n\n{full_text}"
        )
        return await completion.generate(
            http_client,
            model=LLM_MODEL,
            system_prompt=L1_SYSTEM_PROMPT,
            user_prompt=user_prompt,
        )
    except Exception as e:
        log.warning(f"L1 generation failed: {e}")
        return None


# ── Core backfill logic ──────────────────────────────────────


async def tag_unlayered_points(
    qdrant: AsyncQdrantClient,
    collection: str,
    batch_size: int,
    dry_run: bool,
) -> int:
    """Find all points without a ``layer`` field and set ``layer=L2``.

    Returns the number of points tagged.
    """
    tagged = 0
    offset = None  # Qdrant scroll uses point ID as offset

    while True:
        # Scroll points where 'layer' key is missing (IsNullCondition)
        results, next_offset = await qdrant.scroll(
            collection_name=collection,
            scroll_filter=Filter(must=[
                IsNullCondition(is_null=PayloadField(key="layer")),
            ]),
            limit=batch_size,
            with_payload=False,
            with_vectors=False,
            offset=offset,
        )

        if not results:
            break

        point_ids = [p.id for p in results]

        if not dry_run:
            await qdrant.set_payload(
                collection_name=collection,
                payload={"layer": "L2"},
                points=point_ids,
            )

        tagged += len(point_ids)
        log.info(
            f"  {'[DRY-RUN] ' if dry_run else ''}"
            f"Tagged {len(point_ids)} points as L2 "
            f"(total: {tagged})"
        )

        if next_offset is None:
            break
        offset = next_offset

    return tagged


async def collect_doc_groups(
    qdrant: AsyncQdrantClient,
    collection: str,
    batch_size: int,
) -> dict[str, list[dict]]:
    """Scroll all L2 points and group them by ``doc_id``.

    Returns {doc_id: [point_payloads sorted by chunk_index]}.
    """
    groups: dict[str, list[dict]] = defaultdict(list)
    offset = None

    while True:
        results, next_offset = await qdrant.scroll(
            collection_name=collection,
            scroll_filter=Filter(must=[
                FieldCondition(key="layer", match=MatchValue(value="L2")),
            ]),
            limit=batch_size,
            with_payload=True,
            with_vectors=False,
            offset=offset,
        )

        if not results:
            break

        for p in results:
            doc_id = (p.payload or {}).get("doc_id")
            if doc_id:
                groups[doc_id].append(p.payload)

        if next_offset is None:
            break
        offset = next_offset

    # Sort each group by chunk_index
    for doc_id in groups:
        groups[doc_id].sort(key=lambda c: c.get("chunk_index", 0))

    return dict(groups)


async def l0l1_exist(
    qdrant: AsyncQdrantClient,
    collection: str,
    doc_id: str,
) -> tuple[bool, bool]:
    """Check whether L0 and L1 points already exist for a given doc_id."""
    has_l0 = False
    has_l1 = False

    for layer in ("L0", "L1"):
        results, _ = await qdrant.scroll(
            collection_name=collection,
            scroll_filter=Filter(must=[
                FieldCondition(key="doc_id", match=MatchValue(value=doc_id)),
                FieldCondition(key="layer", match=MatchValue(value=layer)),
            ]),
            limit=1,
            with_payload=False,
            with_vectors=False,
        )
        if results:
            if layer == "L0":
                has_l0 = True
            else:
                has_l1 = True

    return has_l0, has_l1


async def backfill_collection(
    qdrant: AsyncQdrantClient,
    http_client: httpx.AsyncClient,
    pg_pool: asyncpg.Pool | None,
    completion: CompletionProvider,
    embedding: EmbeddingProvider,
    collection: str,
    batch_size: int,
    dry_run: bool,
) -> dict:
    """Run the full backfill pipeline for one collection."""

    stats = {
        "collection": collection,
        "points_tagged_l2": 0,
        "documents_found": 0,
        "l0_generated": 0,
        "l1_generated": 0,
        "l0_skipped": 0,
        "l1_skipped": 0,
        "errors": 0,
    }

    # ── Step 1: Tag unlayered points as L2 ──
    log.info(f"[{collection}] Step 1: Tagging unlayered points as L2...")
    stats["points_tagged_l2"] = await tag_unlayered_points(
        qdrant, collection, batch_size, dry_run,
    )
    log.info(
        f"[{collection}] Tagged {stats['points_tagged_l2']} points as L2"
        f"{' (dry-run)' if dry_run else ''}"
    )

    # ── Step 2: Group L2 points by doc_id ──
    log.info(f"[{collection}] Step 2: Collecting L2 points by doc_id...")
    doc_groups = await collect_doc_groups(qdrant, collection, batch_size)
    stats["documents_found"] = len(doc_groups)
    log.info(f"[{collection}] Found {len(doc_groups)} documents")

    if not doc_groups:
        return stats

    # ── Steps 3–6: Generate L0/L1 per document ──
    log.info(f"[{collection}] Step 3-6: Generating L0/L1 layers...")

    for i, (doc_id, chunks_payload) in enumerate(doc_groups.items(), 1):
        source = chunks_payload[0].get("source", "")
        classification = chunks_payload[0].get("classification", "internal")
        chunk_texts = [c.get("text", "") for c in chunks_payload]

        log.info(
            f"[{collection}] Document {i}/{len(doc_groups)}: "
            f"doc_id={doc_id[:12]}... source={source[:60]} "
            f"({len(chunk_texts)} chunks)"
        )

        # Step 3: Check if L0/L1 already exist (idempotency)
        has_l0, has_l1 = await l0l1_exist(qdrant, collection, doc_id)

        if has_l0:
            log.info(f"  L0 already exists — skipping")
            stats["l0_skipped"] += 1
        if has_l1:
            log.info(f"  L1 already exists — skipping")
            stats["l1_skipped"] += 1

        if has_l0 and has_l1:
            continue

        if dry_run:
            if not has_l0:
                log.info(f"  [DRY-RUN] Would generate L0")
                stats["l0_generated"] += 1
            if not has_l1:
                log.info(f"  [DRY-RUN] Would generate L1")
                stats["l1_generated"] += 1
            continue

        # Gather common payload fields from the first chunk (minus chunk-specific ones)
        base_payload = {
            k: v
            for k, v in chunks_payload[0].items()
            if k not in ("text", "chunk_index", "layer", "vault_ref", "contains_pii")
        }
        now_iso = datetime.now(timezone.utc).isoformat()

        l0_point_id: str | None = None
        l1_point_id: str | None = None

        # ── Step 4+5: Generate and upsert L0 ──
        if not has_l0:
            try:
                l0_text = await generate_l0(
                    http_client, completion, chunk_texts,
                    source=source, classification=classification,
                )
                if l0_text:
                    l0_embedding = await embedding.embed(
                        http_client, l0_text, EMBEDDING_MODEL,
                    )
                    l0_point_id = str(uuid.uuid4())
                    l0_point = PointStruct(
                        id=l0_point_id,
                        vector=l0_embedding,
                        payload={
                            **base_payload,
                            "text": l0_text,
                            "chunk_index": 0,
                            "layer": "L0",
                            "doc_id": doc_id,
                            "contains_pii": False,
                            "vault_ref": None,
                            "ingested_at": now_iso,
                        },
                    )
                    await qdrant.upsert(
                        collection_name=collection, points=[l0_point],
                    )
                    stats["l0_generated"] += 1
                    log.info(f"  L0 upserted: {l0_point_id}")
                else:
                    log.warning(f"  L0 generation returned None")
                    stats["errors"] += 1
            except Exception as e:
                log.error(f"  L0 failed: {e}")
                stats["errors"] += 1

        # ── Step 4+5: Generate and upsert L1 ──
        if not has_l1:
            try:
                l1_text = await generate_l1(
                    http_client, completion, chunk_texts,
                    source=source, classification=classification,
                )
                if l1_text:
                    l1_embedding = await embedding.embed(
                        http_client, l1_text, EMBEDDING_MODEL,
                    )
                    l1_point_id = str(uuid.uuid4())
                    l1_point = PointStruct(
                        id=l1_point_id,
                        vector=l1_embedding,
                        payload={
                            **base_payload,
                            "text": l1_text,
                            "chunk_index": 0,
                            "layer": "L1",
                            "doc_id": doc_id,
                            "contains_pii": False,
                            "vault_ref": None,
                            "ingested_at": now_iso,
                        },
                    )
                    await qdrant.upsert(
                        collection_name=collection, points=[l1_point],
                    )
                    stats["l1_generated"] += 1
                    log.info(f"  L1 upserted: {l1_point_id}")
                else:
                    log.warning(f"  L1 generation returned None")
                    stats["errors"] += 1
            except Exception as e:
                log.error(f"  L1 failed: {e}")
                stats["errors"] += 1

        # ── Step 6: Update documents_meta ──
        if pg_pool and (l0_point_id or l1_point_id):
            try:
                # Only update the columns that were newly generated
                if l0_point_id and l1_point_id:
                    await pg_pool.execute(
                        """UPDATE documents_meta
                           SET l0_point_id = $2, l1_point_id = $3
                           WHERE id = $1""",
                        uuid.UUID(doc_id),
                        uuid.UUID(l0_point_id),
                        uuid.UUID(l1_point_id),
                    )
                elif l0_point_id:
                    await pg_pool.execute(
                        """UPDATE documents_meta
                           SET l0_point_id = $2
                           WHERE id = $1""",
                        uuid.UUID(doc_id),
                        uuid.UUID(l0_point_id),
                    )
                elif l1_point_id:
                    await pg_pool.execute(
                        """UPDATE documents_meta
                           SET l1_point_id = $2
                           WHERE id = $1""",
                        uuid.UUID(doc_id),
                        uuid.UUID(l1_point_id),
                    )
                log.info(f"  documents_meta updated")
            except Exception as e:
                log.error(f"  documents_meta update failed: {e}")
                stats["errors"] += 1

    return stats


# ── Main ──────────────────────────────────────────────────────


async def main():
    parser = argparse.ArgumentParser(
        description="Backfill L0/L1 context layers for existing Qdrant data",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be done without making changes",
    )
    parser.add_argument(
        "--collection",
        type=str,
        default=None,
        help=(
            "Process a single collection (default: all three — "
            "pb_general, pb_code, pb_rules)"
        ),
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Number of points per scroll batch (default: 100)",
    )
    args = parser.parse_args()

    collections = [args.collection] if args.collection else ALL_COLLECTIONS
    mode = "DRY-RUN" if args.dry_run else "EXECUTE"
    log.info(f"Backfill Layers started [{mode}]")
    log.info(f"  Collections: {collections}")
    log.info(f"  Batch size:  {args.batch_size}")
    log.info(f"  LLM:         {LLM_PROVIDER_URL} / {LLM_MODEL}")
    log.info(f"  Embedding:   {EMBEDDING_PROVIDER_URL} / {EMBEDDING_MODEL}")

    # ── Initialize clients ──
    qdrant = AsyncQdrantClient(url=QDRANT_URL)
    http_client = httpx.AsyncClient(timeout=120.0)

    completion = CompletionProvider(base_url=LLM_PROVIDER_URL, api_key=LLM_API_KEY)
    embedding_prov = EmbeddingProvider(
        base_url=EMBEDDING_PROVIDER_URL, api_key=EMBEDDING_API_KEY,
    )

    pg_pool: asyncpg.Pool | None = None
    try:
        pg_pool = await asyncpg.create_pool(POSTGRES_URL, min_size=1, max_size=5)
        log.info("PostgreSQL connected")
    except Exception as e:
        log.warning(f"PostgreSQL unavailable (documents_meta won't be updated): {e}")

    # ── Process each collection ──
    all_stats = []
    for collection in collections:
        log.info(f"{'=' * 60}")
        log.info(f"Processing collection: {collection}")
        log.info(f"{'=' * 60}")

        try:
            stats = await backfill_collection(
                qdrant=qdrant,
                http_client=http_client,
                pg_pool=pg_pool,
                completion=completion,
                embedding=embedding_prov,
                collection=collection,
                batch_size=args.batch_size,
                dry_run=args.dry_run,
            )
            all_stats.append(stats)
        except Exception as e:
            log.error(f"Collection {collection} failed: {e}")
            all_stats.append({"collection": collection, "error": str(e)})

    # ── Summary ──
    log.info(f"\n{'=' * 60}")
    log.info(f"BACKFILL SUMMARY [{mode}]")
    log.info(f"{'=' * 60}")
    for s in all_stats:
        col = s.get("collection", "?")
        if "error" in s:
            log.info(f"  {col}: FAILED — {s['error']}")
        else:
            log.info(
                f"  {col}: "
                f"tagged={s['points_tagged_l2']}, "
                f"docs={s['documents_found']}, "
                f"L0={s['l0_generated']}(+{s['l0_skipped']} skipped), "
                f"L1={s['l1_generated']}(+{s['l1_skipped']} skipped), "
                f"errors={s['errors']}"
            )

    # ── Cleanup ──
    if pg_pool:
        await pg_pool.close()
    await http_client.aclose()
    log.info("Backfill complete.")


if __name__ == "__main__":
    asyncio.run(main())
