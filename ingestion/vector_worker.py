"""Stage 2: backfill embeddings for media_metadata rows.

Fetches rows with a null embedding, encodes title + synopsis + genres +
tags with SentenceTransformers (all-MiniLM-L6-v2) in batches, and writes
the resulting 384-dim vectors back to Supabase.

Writes go through the `bulk_update_embeddings` RPC (see
supabase/migrations/20260730071040_bulk_update_embeddings.sql), not
`.upsert()`. supabase-py's `.upsert()` performs a real
`INSERT ... ON CONFLICT DO UPDATE` against the *whole* row shape: any
column left out of the payload -- title_english, synopsis, genres, tags,
popularity, cover_image_url -- gets overwritten with its default/null
rather than left alone. Confirmed directly against production data
before this fix landed: upserting just `{id, embedding}` attempted to
null out every other column on the row, and only failed loudly because
`type` happens to be NOT NULL -- a column without that constraint would
have been silently wiped across all 158k+ rows. The RPC does a genuine
targeted `UPDATE ... SET embedding = ...`, so it only ever touches the
one column this script is responsible for.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from dotenv import load_dotenv
from postgrest.exceptions import APIError
from sentence_transformers import SentenceTransformer
from supabase import Client, create_client

MODEL_NAME = os.environ.get("EMBEDDING_MODEL_NAME", "all-MiniLM-L6-v2")
FETCH_PAGE_SIZE = 500
ENCODE_BATCH_SIZE = 64
MAX_ATTEMPTS_PER_SIZE = 3
INITIAL_BACKOFF_SECONDS = 3.0

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


def _write_updates(supabase: Client, updates: list[dict[str, Any]]) -> None:
    """Write a batch via bulk_update_embeddings, retrying transient
    failures with backoff. If a batch keeps failing even after retries,
    halve it and recurse -- guards against both a momentary Postgres/
    pooler hiccup (e.g. Supabase's pooled connections enforce a
    statement_timeout well under what a full 500-row batch can
    occasionally take) and the possibility that a specific batch is
    simply too large for that timeout, without permanently shrinking
    every batch just because one was slow.
    """
    if not updates:
        return

    attempt = 0
    while True:
        try:
            supabase.rpc("bulk_update_embeddings", {"updates": updates}).execute()
            return
        except APIError as exc:
            attempt += 1
            if attempt > MAX_ATTEMPTS_PER_SIZE:
                if len(updates) == 1:
                    logger.error(
                        "Giving up on id=%s after %s attempts: %s", updates[0]["id"], attempt - 1, exc
                    )
                    raise
                mid = len(updates) // 2
                logger.warning(
                    "Batch of %s still failing after %s attempts (code=%s), splitting into "
                    "%s + %s and retrying",
                    len(updates),
                    attempt - 1,
                    exc.code,
                    mid,
                    len(updates) - mid,
                )
                _write_updates(supabase, updates[:mid])
                _write_updates(supabase, updates[mid:])
                return
            backoff = INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1))
            logger.warning(
                "bulk_update_embeddings failed (attempt %s/%s, code=%s): %s -- retrying in %ss",
                attempt,
                MAX_ATTEMPTS_PER_SIZE,
                exc.code,
                exc.message,
                backoff,
            )
            time.sleep(backoff)


def _fetch_pending(supabase: Client, limit: int) -> list[dict[str, Any]]:
    """Fetch the next batch of rows still missing an embedding.

    Backed by media_metadata_pending_embedding_idx (see
    supabase/migrations/20260730132906_pending_embedding_idx.sql) -- a
    partial index on `WHERE embedding IS NULL`, added after this query
    started timing out once null rows became a small fraction of the
    table (a sequential scan has to examine more and more non-matching
    rows to find each batch as the backfill nears completion). Retried
    with backoff on top of that fix as defense in depth, matching
    _write_updates.
    """
    attempt = 0
    while True:
        try:
            response = (
                supabase.table("media_metadata")
                .select("id, title_english, synopsis, genres, tags")
                .is_("embedding", "null")
                .limit(limit)
                .execute()
            )
            return response.data or []
        except APIError as exc:
            attempt += 1
            if attempt > MAX_ATTEMPTS_PER_SIZE:
                raise
            backoff = INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1))
            logger.warning(
                "Fetching pending rows failed (attempt %s/%s, code=%s): %s -- retrying in %ss",
                attempt,
                MAX_ATTEMPTS_PER_SIZE,
                exc.code,
                exc.message,
                backoff,
            )
            time.sleep(backoff)


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
        _write_updates(supabase, updates)

        total += len(rows)
        logger.info("Embedded %s rows (running total=%s)", len(rows), total)

    logger.info("Done. %s rows embedded.", total)


if __name__ == "__main__":
    run()
