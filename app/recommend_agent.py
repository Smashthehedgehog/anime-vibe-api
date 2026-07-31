"""Method 2: an LLM-driven recommendation agent.

Method 1 (app.py's POST /api/v1/search/vibe, and the MCP tool directly)
is deterministic: embed the query, rank by cosine similarity + popularity,
return the list. The caller does the picking.

This module adds a second way to get a recommendation: an LLM (Groq's
llama-3.3-70b-versatile, chosen for being fast and effectively free) acts
as a genuine MCP *client* against this same server's MCP server object
(`mcp_server.mcp`) -- the same `ClientSession`, the same tool schema from
`list_tools()`, the same `search_anime_manga_vibes` tool implementation
an external agent like Claude Desktop would call over `/sse`. Two phases:

  1. Gather: open a `ClientSession` against our own MCP server, hand it
     the tool's schema (MCP's `list_tools()` already returns JSON
     Schema, so no translation needed), and let it call
     search_anime_manga_vibes as many times as it wants (up to
     MAX_TOOL_ROUNDS) to build a pool of real candidates -- e.g.
     retrying with a different phrasing if the first batch feels thin.
  2. Rank: once gathering stops, a separate request (Groq's JSON mode,
     no tools this time) asks it to rank the *actual* candidates it saw
     and return its top 10 with a reason each. Every returned id is
     cross-checked against what the tool actually returned -- a
     hallucinated id is dropped rather than trusted, same principle as
     the single-pick version this replaced.

Transport: in-process memory streams (`mcp.shared.memory`), not a live
network SSE connection to our own `/sse` route. Originally this dialed
out over real SSE to demonstrate the wire protocol end to end -- but on
Render's free tier (512 MB RAM) that meant one process holding the
loaded embedding model *and* a second live HTTP/SSE socket to itself
*and* several rounds of Groq calls with full candidate payloads all at
once, which reliably OOM-killed the container (confirmed both in Render's
logs and by reproducing the crash directly). `create_connected_server_and_client_session`
still runs the real MCP `ClientSession`/`Server` pair -- real JSON-RPC
messages, the real tool schema, the real tool implementation -- just over
in-memory queues instead of a redundant loopback socket, which is the
part that was actually expensive, not the protocol itself. Candidate
payloads fed back into the LLM's own message history are also trimmed
(see `_tool_result_for_llm`) to keep that history from growing unbounded
across rounds; the full, untrimmed record is still what's returned to
the caller.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from groq import Groq
from mcp.shared.memory import create_connected_server_and_client_session

from app.mcp_server import mcp

logger = logging.getLogger("recommend_agent")

GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
MAX_TOOL_ROUNDS = 3
TOP_N = 10
# How much of each candidate's synopsis to keep in the LLM's own message
# history (phase 1 tool results carried into phase 2's ranking prompt).
# The full synopsis is always preserved in `seen_candidates` / the final
# response -- this only trims what gets fed back into the conversation,
# to stop it from growing unbounded across MAX_TOOL_ROUNDS rounds.
SYNOPSIS_CHARS_FOR_LLM = 300

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


def _tool_result_for_llm(parsed: dict[str, Any]) -> str:
    """Trim a tool result before it goes into the LLM's own message
    history -- full synopsis text times up to ~30 candidates times up to
    MAX_TOOL_ROUNDS rounds otherwise makes that history (and Groq's
    context) grow fast. The caller keeps the untrimmed `parsed` for
    `seen_candidates` / the final response; this is only what the model
    itself re-reads on the next round and in the ranking prompt.
    """
    trimmed = []
    for r in parsed.get("results", []):
        synopsis = r.get("synopsis") or ""
        trimmed.append(
            {
                "id": r.get("id"),
                "type": r.get("type"),
                "title_english": r.get("title_english"),
                "synopsis": synopsis[:SYNOPSIS_CHARS_FOR_LLM],
                "genres": r.get("genres"),
                "tags": r.get("tags"),
                "popularity": r.get("popularity"),
            }
        )
    return json.dumps({"query": parsed.get("query"), "count": len(trimmed), "results": trimmed})


async def _call_search_tool(
    session: Any,
    query: str,
    media_type: str,
    limit: int,
    seen_candidates: dict[int, dict[str, Any]],
) -> str:
    """Call the search tool, record full candidates, return a trimmed
    JSON string (or "{}" on a parse failure) for the LLM's own history.
    """
    result = await session.call_tool(
        "search_anime_manga_vibes", {"query": query, "media_type": media_type, "limit": limit}
    )
    text = result.content[0].text if result.content else "{}"
    try:
        parsed = json.loads(text)
        for r in parsed.get("results", []):
            seen_candidates[r["id"]] = r
        return _tool_result_for_llm(parsed)
    except (json.JSONDecodeError, KeyError, TypeError):
        logger.warning("Could not parse tool result as search results: %r", text[:200])
        return "{}"


async def get_recommendation(vibe: str, media_type: str) -> dict[str, Any]:
    """Run the two-phase agent and return {recommendations, tool_calls}.

    `recommendations` is a list of up to TOP_N {rank, reason, media}
    dicts, ordered by the model's own judgment -- not the raw embedding
    similarity/score from the search tool (those are still present on
    each `media` record, just not what determined this ordering).
    """
    groq_client = Groq(api_key=os.environ["GROQ_API_KEY"])

    async with create_connected_server_and_client_session(mcp, raise_exceptions=True) as session:
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

        # --- Phase 1: gather candidates via real tool calls -------------
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

                trimmed_text = await _call_search_tool(
                    session,
                    query=args.get("query", vibe),
                    media_type=args.get("media_type", media_type),
                    limit=args.get("limit", 20),
                    seen_candidates=seen_candidates,
                )
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": trimmed_text})

        # Safety net: if the model never actually searched, do one
        # ourselves so phase 2 has something real to rank instead of
        # ranking nothing (or, worse, being tempted to invent ids).
        if not seen_candidates:
            logger.warning("Agent gathered zero candidates via tool calls; searching directly as a fallback")
            tool_call_log.append(
                {"tool": "search_anime_manga_vibes", "arguments": {"query": vibe, "media_type": media_type, "limit": 20}}
            )
            await _call_search_tool(session, query=vibe, media_type=media_type, limit=20, seen_candidates=seen_candidates)

        # --- Phase 2: rank the actual candidates, structured output ----
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
