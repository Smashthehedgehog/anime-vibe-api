"""Stage 1: async AniList -> Supabase ingestion worker.

Paginates ANIME and MANGA from AniList's public GraphQL API and upserts
metadata into `media_metadata` (embedding left null for the stage-2
vector_worker to backfill).

----------------------------------------------------------------------
AniList's page-depth cap, and how this works around it
----------------------------------------------------------------------
AniList's `Page` query refuses `page * perPage > 5000` outright (HTTP 400,
"Page depth exceeds maximum allowed for API requests (5000 entries)"),
regardless of how the results are filtered. A naive incrementing-page
loop -- fetch page 1, 2, 3, ... until `hasNextPage` is false -- silently
caps out at the first 5000 entries (sorted by ID, i.e. the *oldest* 5000
of each type) and never reaches the rest of the catalog. `pageInfo.total`
doesn't help detect this either: AniList also clamps `total` at 5000, so
a query matching 20,000 titles still reports `total: 5000`.

The fix is to partition the catalog by `startDate` into windows small
enough that each one's own result set stays under the cap, and page
through each window independently -- the depth limit is evaluated
against the *filtered* result set, not the whole unfiltered corpus, so a
window with 3,000 matching titles can be paginated in full even though
the catalog overall has far more. `_ingest_range` does this by
bisection: probe whether item #5000 exists within a given `startDate`
window (`_bucket_needs_split`); if it does, the window still exceeds the
cap, so split it in half by date and recurse; if not, the window is
small enough to page through directly with `_paginate_bucket`. Windows
are `(start_greater, start_lesser)` *exclusive* bounds so adjacent
splits neither overlap nor leave a gap, and the initial call spans
`(-1, 30000102)` -- covering `startDate = 0` (AniList's "no date set" /
TBA value) through the year 3000 -- with a diff-<=2 guard for the
vanishingly unlikely case of 5000+ titles sharing one literal date.

This was not a hypothetical concern caught by reading the docs: it was
found by actually running the naive version against production, which
silently stopped at exactly 5000 rows.
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

# AniList's hard `page * perPage` ceiling -- confirmed empirically (see
# module docstring), not documented anywhere obvious.
ANILIST_PAGE_DEPTH_CAP = 5000

# startDate window bounds for the initial (whole-catalog) bisection call.
# Exclusive bounds: -1 so startDate == 0 (AniList's "unset"/TBA value) is
# included; 30000102 as a sentinel comfortably past any real release date.
DATE_FLOOR = -1
DATE_CEILING = 30000102

MEDIA_QUERY = """
query (
  $page: Int
  $perPage: Int
  $type: MediaType
  $startGreater: FuzzyDateInt
  $startLesser: FuzzyDateInt
) {
  Page(page: $page, perPage: $perPage) {
    pageInfo {
      hasNextPage
    }
    media(
      type: $type
      sort: ID
      startDate_greater: $startGreater
      startDate_lesser: $startLesser
    ) {
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
    client: httpx.AsyncClient,
    media_type: str,
    page: int,
    per_page: int,
    start_greater: int,
    start_lesser: int,
) -> tuple[list[dict[str, Any]], bool]:
    variables = {
        "page": page,
        "perPage": per_page,
        "type": media_type,
        "startGreater": start_greater,
        "startLesser": start_lesser,
    }
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


async def _bucket_needs_split(
    client: httpx.AsyncClient, media_type: str, start_greater: int, start_lesser: int
) -> bool:
    """True if this startDate window still has >= the depth cap worth of
    entries, i.e. item #5000 within it exists. See module docstring."""
    media_list, _has_next = await _fetch_page(
        client,
        media_type,
        page=ANILIST_PAGE_DEPTH_CAP,
        per_page=1,
        start_greater=start_greater,
        start_lesser=start_lesser,
    )
    return len(media_list) > 0


async def _paginate_bucket(
    client: httpx.AsyncClient,
    supabase: Client,
    media_type: str,
    start_greater: int,
    start_lesser: int,
) -> int:
    """Fetch every page of a window already confirmed to be under the cap."""
    page = 1
    total = 0
    while True:
        media_list, has_next = await _fetch_page(
            client,
            media_type,
            page=page,
            per_page=PER_PAGE,
            start_greater=start_greater,
            start_lesser=start_lesser,
        )
        if media_list:
            rows = [_to_row(m, media_type) for m in media_list]
            supabase.table("media_metadata").upsert(rows).execute()
            total += len(rows)
            logger.info(
                "Upserted %s page %s [window (%s, %s)]: %s rows (window total=%s)",
                media_type,
                page,
                start_greater,
                start_lesser,
                len(rows),
                total,
            )
        if not has_next:
            break
        page += 1
    return total


async def _ingest_range(
    client: httpx.AsyncClient,
    supabase: Client,
    media_type: str,
    start_greater: int,
    start_lesser: int,
) -> int:
    if start_lesser - start_greater <= 1:
        return 0  # empty window

    if not await _bucket_needs_split(client, media_type, start_greater, start_lesser):
        return await _paginate_bucket(client, supabase, media_type, start_greater, start_lesser)

    if start_lesser - start_greater <= 2:
        # Exactly one startDate value left in this window and it alone
        # has >= 5000 entries -- can't bisect a single value any further.
        logger.warning(
            "%s startDate=%s has >= %s entries, exceeding AniList's page "
            "depth cap for a single date -- only the first %s will be "
            "ingested for that date",
            media_type,
            start_greater + 1,
            ANILIST_PAGE_DEPTH_CAP,
            ANILIST_PAGE_DEPTH_CAP,
        )
        return await _paginate_bucket(client, supabase, media_type, start_greater, start_lesser)

    mid = start_greater + (start_lesser - start_greater) // 2
    logger.info(
        "%s startDate window (%s, %s) exceeds the depth cap, splitting at %s",
        media_type,
        start_greater,
        start_lesser,
        mid,
    )
    count = await _ingest_range(client, supabase, media_type, start_greater, mid + 1)
    count += await _ingest_range(client, supabase, media_type, mid, start_lesser)
    return count


async def _ingest_media_type(client: httpx.AsyncClient, supabase: Client, media_type: str) -> int:
    return await _ingest_range(client, supabase, media_type, DATE_FLOOR, DATE_CEILING)


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
