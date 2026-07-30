"""Stage 1: async AniList -> Supabase ingestion worker.

Paginates ANIME and MANGA from AniList's public GraphQL API and upserts
metadata into `media_metadata` (embedding left null for the stage-2
vector_worker to backfill).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from typing import Any

import httpx
from dotenv import load_dotenv
from supabase import Client, create_client

ANILIST_URL = "https://graphql.anilist.co"
PER_PAGE = 50
REQUEST_TIMEOUT = 30.0
MIN_RATE_REMAINING = 2  # back off once the bucket gets this low
MAX_SERVER_ERROR_RETRIES = 5

MEDIA_QUERY = """
query ($page: Int, $perPage: Int, $type: MediaType) {
  Page(page: $page, perPage: $perPage) {
    pageInfo {
      hasNextPage
    }
    media(type: $type, sort: ID) {
      id
      title {
        english
        romaji
      }
      description(asHtml: false)
      genres
      tags {
        name
      }
      popularity
      coverImage {
        large
      }
    }
  }
}
"""

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("ingestion_worker")

_TAG_RE = re.compile(r"<[^>]+>")


def _clean_synopsis(raw: str | None) -> str | None:
    if not raw:
        return None
    return _TAG_RE.sub("", raw).strip()


def _to_row(media: dict[str, Any], media_type: str) -> dict[str, Any]:
    title = media.get("title") or {}
    cover = media.get("coverImage") or {}
    return {
        "id": media["id"],
        "type": media_type,
        "title_english": title.get("english") or title.get("romaji"),
        "synopsis": _clean_synopsis(media.get("description")),
        "genres": media.get("genres") or [],
        "tags": [t["name"] for t in media.get("tags") or [] if t.get("name")],
        "popularity": media.get("popularity") or 0,
        "cover_image_url": cover.get("large"),
    }


async def _respect_rate_limit(response: httpx.Response) -> None:
    remaining = response.headers.get("X-RateLimit-Remaining")
    if remaining is not None and int(remaining) <= MIN_RATE_REMAINING:
        logger.info("Rate limit nearly exhausted (remaining=%s), pausing 60s", remaining)
        await asyncio.sleep(60)


async def _fetch_page(
    client: httpx.AsyncClient, media_type: str, page: int
) -> tuple[list[dict[str, Any]], bool]:
    variables = {"page": page, "perPage": PER_PAGE, "type": media_type}
    server_error_attempts = 0

    while True:
        response = await client.post(
            ANILIST_URL, json={"query": MEDIA_QUERY, "variables": variables}
        )

        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", "60"))
            logger.warning("Rate limited by AniList, sleeping %ss", retry_after)
            await asyncio.sleep(retry_after)
            continue

        if response.status_code >= 500:
            server_error_attempts += 1
            if server_error_attempts > MAX_SERVER_ERROR_RETRIES:
                response.raise_for_status()
            backoff = min(2**server_error_attempts, 60)
            logger.warning(
                "AniList server error %s on %s page %s, retrying in %ss",
                response.status_code,
                media_type,
                page,
                backoff,
            )
            await asyncio.sleep(backoff)
            continue

        response.raise_for_status()
        await _respect_rate_limit(response)
        payload = response.json()["data"]["Page"]
        return payload["media"], payload["pageInfo"]["hasNextPage"]


async def _ingest_media_type(
    client: httpx.AsyncClient, supabase: Client, media_type: str
) -> int:
    page = 1
    total = 0
    while True:
        media_list, has_next = await _fetch_page(client, media_type, page)
        if media_list:
            rows = [_to_row(m, media_type) for m in media_list]
            supabase.table("media_metadata").upsert(rows).execute()
            total += len(rows)
            logger.info(
                "Upserted %s page %s: %s rows (running total=%s)",
                media_type,
                page,
                len(rows),
                total,
            )
        if not has_next:
            break
        page += 1
    return total


async def run() -> None:
    load_dotenv()
    supabase_url = os.environ["SUPABASE_URL"]
    supabase_key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    supabase = create_client(supabase_url, supabase_key)

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        for media_type in ("ANIME", "MANGA"):
            logger.info("Starting ingestion for %s", media_type)
            count = await _ingest_media_type(client, supabase, media_type)
            logger.info("Finished %s: %s rows upserted", media_type, count)


def main() -> None:
    try:
        asyncio.run(run())
    except KeyError as exc:
        logger.error("Missing required environment variable: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
