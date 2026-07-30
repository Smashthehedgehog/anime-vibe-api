"""Stage 2: backfill embeddings for media_metadata rows.

Fetches rows with a null embedding, encodes title + synopsis + genres +
tags with SentenceTransformers (all-MiniLM-L6-v2) in batches, and writes
the resulting 384-dim vectors back to Supabase.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from dotenv import load_dotenv
from sentence_transformers import SentenceTransformer
from supabase import Client, create_client

MODEL_NAME = os.environ.get("EMBEDDING_MODEL_NAME", "all-MiniLM-L6-v2")
FETCH_PAGE_SIZE = 500
ENCODE_BATCH_SIZE = 64

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("vector_worker")


def _build_context(row: dict[str, Any]) -> str:
    parts = [
        row.get("title_english") or "",
        row.get("synopsis") or "",
        ", ".join(row.get("genres") or []),
        ", ".join(row.get("tags") or []),
    ]
    return "\n".join(p for p in parts if p)


def _fetch_pending(supabase: Client, limit: int) -> list[dict[str, Any]]:
    response = (
        supabase.table("media_metadata")
        .select("id, title_english, synopsis, genres, tags")
        .is_("embedding", "null")
        .limit(limit)
        .execute()
    )
    return response.data or []


def run() -> None:
    load_dotenv()
    supabase: Client = create_client(
        os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    )

    logger.info("Loading embedding model %s", MODEL_NAME)
    model = SentenceTransformer(MODEL_NAME)

    total = 0
    while True:
        rows = _fetch_pending(supabase, FETCH_PAGE_SIZE)
        if not rows:
            break

        contexts = [_build_context(row) for row in rows]
        embeddings = model.encode(
            contexts, batch_size=ENCODE_BATCH_SIZE, show_progress_bar=False
        )

        updates = [
            {"id": row["id"], "embedding": embedding.tolist()}
            for row, embedding in zip(rows, embeddings)
        ]
        supabase.table("media_metadata").upsert(updates).execute()

        total += len(rows)
        logger.info("Embedded %s rows (running total=%s)", len(rows), total)

    logger.info("Done. %s rows embedded.", total)


if __name__ == "__main__":
    run()
