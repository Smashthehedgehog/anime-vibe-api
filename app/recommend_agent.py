"""Method 2: an LLM-driven recommendation agent.

Method 1 (app.py's POST /api/v1/search/vibe, and the MCP tool directly)
is deterministic: embed the query, rank by cosine similarity + popularity,
return the list. The caller does the picking.

This module adds a second way to get a recommendation: an LLM (Groq's
llama-3.3-70b-versatile, chosen for being fast and effectively free) acts
as a genuine MCP *client* against this same server's own `/sse` endpoint
-- the exact same tool (`search_anime_manga_vibes`) an external agent
like Claude Desktop would call, not a shortcut that calls vibe_search()
directly in-process. Two phases:

  1. Gather: connect over SSE to our own MCP server (authenticated with
     the caller's own API key -- no bypass credential, these search
     calls count against their own rate limit like anything else would),
     hand it the tool's schema (MCP's `list_tools()` already returns
     JSON Schema, so no translation needed), and let it call
     search_anime_manga_vibes as many times as it wants (up to
     MAX_TOOL_ROUNDS) to build a pool of real candidates -- e.g.
     retrying with a different phrasing if the first batch feels thin.
  2. Rank: once gathering stops, a separate request (Groq's JSON mode,
     no tools this time) asks it to rank the *actual* candidates it saw
     and return its top 10 with a reason each. Every returned id is
     cross-checked against what the tool actually returned -- a
     hallucinated id is dropped rather than trusted, same principle as
     the single-pick version this replaced.

Why go through MCP here instead of calling vibe_search() directly, given
this all runs in one process anyway: this endpoint exists specifically to
demonstrate and exercise the MCP surface with a real tool-calling LLM,
per the project's own goal. An in-process shortcut would be simpler and
faster, but would defeat the point of this particular feature.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from groq import Groq
from mcp import ClientSession
from mcp.client.sse import sse_client

logger = logging.getLogger("recommend_agent")

GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
# Render (and most PaaS hosts) assign the actual listening port via $PORT
# at runtime -- it's not always 8000. The Dockerfile's CMD already binds
# uvicorn to ${PORT:-8000} (see Dockerfile), so this default has to match
# that or the agent tries to reach itself on the wrong port and every
# recommend call fails. INTERNAL_MCP_URL can still be overridden directly
# for setups where the agent and MCP server aren't on the same host.
INTERNAL_MCP_URL = os.environ.get(
    "INTERNAL_MCP_URL", f"http://127.0.0.1:{os.environ.get('PORT', '8000')}/sse"
)
MAX_TOOL_ROUNDS = 3
TOP_N = 10

GATHER_SYSTEM_PROMPT = """You are a knowledgeable anime/manga recommender helping \
build a shortlist, not picking one title yet.

You have one tool, search_anime_manga_vibes, which performs semantic
search over a real catalog and returns candidates with a synopsis,
genres, tags, and popularity. Call it to find options matching the
user's described vibe -- request a limit around 20-30 so there's a
decent pool to choose from. You may call it more than once (e.g. a
second, differently-phrased or narrower query) if the first batch feels
thin or off-target, but don't over-search: stop once you have a good
enough pool to pick a strong top 10 from.

When you're done gathering, reply with a short plain-text note (a
sentence or two) about what you found -- do not attempt to produce the
final ranked list yet, that happens in a separate step."""

RANK_INSTRUCTION_TEMPLATE = """Based on everything you found, rank your top \
{top_n} best fits for this vibe: "{vibe}"

Respond with JSON only, in exactly this shape:
{{"recommendations": [{{"id": <int>, "reason": "<one specific sentence, written for the user, on why this fits>"}}, ...]}}

Rules:
- Order from best fit to weakest, most confident pick first.
- At most {top_n} items.
- Every "id" MUST be the numeric id of a title that was actually returned \
by search_anime_manga_vibes above -- never invent one.
- Do not repeat an id."""


async def get_recommendation(api_key: str, vibe: str, media_type: str) -> dict[str, Any]:
    """Run the two-phase agent and return {recommendations, tool_calls}.

    `recommendations` is a list of up to TOP_N {rank, reason, media}
    dicts, ordered by the model's own judgment -- not the raw embedding
    similarity/score from the search tool (those are still present on
    each `media` record, just not what determined this ordering).
    """
    groq_client = Groq(api_key=os.environ["GROQ_API_KEY"])

    async with sse_client(INTERNAL_MCP_URL, headers={"X-API-Key": api_key}) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools_result = await session.list_tools()
            groq_tools = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.inputSchema,
                    },
                }
                for t in tools_result.tools
            ]

            messages: list[dict[str, Any]] = [
                {"role": "system", "content": GATHER_SYSTEM_PROMPT},
                {"role": "user", "content": f"Vibe: {vibe}\nMedia type filter: {media_type}"},
            ]

            seen_candidates: dict[int, dict[str, Any]] = {}
            tool_call_log: list[dict[str, Any]] = []

            # --- Phase 1: gather candidates via real tool calls ---------
            for _round in range(MAX_TOOL_ROUNDS):
                response = groq_client.chat.completions.create(
                    model=GROQ_MODEL,
                    messages=messages,
                    tools=groq_tools,
                    tool_choice="auto",
                )
                msg = response.choices[0].message
                messages.append(msg.model_dump(exclude_none=True))

                if not msg.tool_calls:
                    break

                for tc in msg.tool_calls:
                    args = json.loads(tc.function.arguments or "{}")
                    logger.info("Agent calling %s(%s)", tc.function.name, args)
                    tool_call_log.append({"tool": tc.function.name, "arguments": args})

                    result = await session.call_tool(tc.function.name, args)
                    text = result.content[0].text if result.content else "{}"
                    try:
                        parsed = json.loads(text)
                        for r in parsed.get("results", []):
                            seen_candidates[r["id"]] = r
                    except (json.JSONDecodeError, KeyError, TypeError):
                        logger.warning("Could not parse tool result as search results: %r", text[:200])

                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": text})

            # Safety net: if the model never actually searched, do one
            # ourselves so phase 2 has something real to rank instead of
            # ranking nothing (or, worse, being tempted to invent ids).
            if not seen_candidates:
                logger.warning("Agent gathered zero candidates via tool calls; searching directly as a fallback")
                tool_call_log.append(
                    {"tool": "search_anime_manga_vibes", "arguments": {"query": vibe, "media_type": media_type, "limit": 20}}
                )
                result = await session.call_tool(
                    "search_anime_manga_vibes", {"query": vibe, "media_type": media_type, "limit": 20}
                )
                text = result.content[0].text if result.content else "{}"
                try:
                    parsed = json.loads(text)
                    for r in parsed.get("results", []):
                        seen_candidates[r["id"]] = r
                except (json.JSONDecodeError, KeyError, TypeError):
                    logger.error("Fallback search also failed to parse: %r", text[:200])

            # --- Phase 2: rank the actual candidates, structured output --
            recommendations = await _rank_candidates(groq_client, messages, vibe, seen_candidates)

            return {"recommendations": recommendations, "tool_calls": tool_call_log}


async def _rank_candidates(
    groq_client: Groq,
    messages: list[dict[str, Any]],
    vibe: str,
    seen_candidates: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    if not seen_candidates:
        return []

    rank_messages = messages + [
        {"role": "user", "content": RANK_INSTRUCTION_TEMPLATE.format(top_n=TOP_N, vibe=vibe)}
    ]

    response = groq_client.chat.completions.create(
        model=GROQ_MODEL,
        messages=rank_messages,
        response_format={"type": "json_object"},
    )
    raw = response.choices[0].message.content or "{}"

    try:
        parsed = json.loads(raw)
        items = parsed.get("recommendations", [])
    except json.JSONDecodeError:
        logger.error("Ranking response wasn't valid JSON: %r", raw[:300])
        items = []

    recommendations: list[dict[str, Any]] = []
    used_ids: set[int] = set()
    for item in items:
        if len(recommendations) >= TOP_N:
            break
        try:
            item_id = int(item["id"])
        except (KeyError, TypeError, ValueError):
            continue
        if item_id in used_ids or item_id not in seen_candidates:
            logger.warning("Dropping ranked id=%s: not among candidates actually seen", item_id)
            continue
        used_ids.add(item_id)
        recommendations.append(
            {
                "rank": len(recommendations) + 1,
                "reason": str(item.get("reason", "")).strip(),
                "media": seen_candidates[item_id],
            }
        )

    return recommendations
