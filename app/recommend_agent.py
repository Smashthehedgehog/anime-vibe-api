"""Method 2: an LLM-driven recommendation agent.

Method 1 (app.py's POST /api/v1/search/vibe, and the MCP tool directly)
is deterministic: embed the query, rank by cosine similarity + popularity,
return the list. The caller does the picking.

This module adds a second way to get a recommendation: an LLM (Groq's
llama-3.3-70b-versatile, chosen for being fast and effectively free) acts
as a genuine MCP *client* against this same server's own `/sse` endpoint
-- the exact same tool (`search_anime_manga_vibes`) an external agent
like Claude Desktop would call, not a shortcut that calls vibe_search()
directly in-process. Concretely:

  1. Connect over SSE to our own MCP server, authenticated with the
     caller's own API key (so the search calls this triggers count
     against their own rate limit -- no special-cased bypass credential).
  2. Ask it what tools it has (`list_tools`) and hand that schema straight
     to Groq as an available function -- MCP's tool schema is already
     JSON Schema, so no translation is needed.
  3. Loop: give Groq the user's vibe, let it decide whether to call the
     tool (possibly more than once -- e.g. to retry with a narrower
     query if the first batch of results doesn't fit well), execute
     whatever it asks for via the real MCP `call_tool`, feed the result
     back, repeat until it gives a final answer instead of another tool
     call.
  4. The system prompt requires the final answer to end with a
     `RECOMMENDATION_ID: <id>` line referencing one of the actual
     candidates the tool returned. We parse that out to attach the full
     media record (cover image, genres, etc.) to the response instead of
     asking the frontend to parse it out of prose.

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
INTERNAL_MCP_URL = os.environ.get("INTERNAL_MCP_URL", "http://127.0.0.1:8000/sse")
MAX_TOOL_ROUNDS = 3

SYSTEM_PROMPT = """You are a knowledgeable, opinionated anime/manga recommender.

You have one tool, search_anime_manga_vibes, which performs semantic
search over a real catalog and returns candidates with a synopsis,
genres, tags, popularity, and a similarity/score. Use it to find options
matching the user's described vibe. You may call it more than once -- if
the first results don't feel like a strong fit, try a differently-phrased
or narrower query before settling.

Do not recommend anything the tool did not actually return -- never
invent a title or id.

Once you have a confident pick, reply with a short, specific explanation
(2-4 sentences, written for the user, not a summary of your search
process) of why that title fits what they asked for. End your reply with
exactly one line, on its own, in this exact format:

RECOMMENDATION_ID: <id>

where <id> is the numeric id field of the title you're recommending, from
one of the tool's results."""

_ID_LINE_PREFIX = "RECOMMENDATION_ID:"


def _extract_recommendation_id(text: str) -> int | None:
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if line.upper().startswith(_ID_LINE_PREFIX):
            digits = "".join(ch for ch in line[len(_ID_LINE_PREFIX):] if ch.isdigit())
            if digits:
                return int(digits)
    return None


def _strip_recommendation_line(text: str) -> str:
    lines = [
        line for line in (text or "").splitlines() if not line.strip().upper().startswith(_ID_LINE_PREFIX)
    ]
    return "\n".join(lines).strip()


async def get_recommendation(api_key: str, vibe: str, media_type: str) -> dict[str, Any]:
    """Run the agent loop and return {explanation, media, tool_calls, raw_text}.

    `media` is the full candidate record the agent settled on (or None if
    it never called the tool, or its final RECOMMENDATION_ID didn't match
    anything actually returned -- treated as a failure to recommend
    rather than trusted blindly).
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
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"Vibe: {vibe}\nMedia type filter: {media_type}",
                },
            ]

            seen_candidates: dict[int, dict[str, Any]] = {}
            tool_call_log: list[dict[str, Any]] = []

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
                    return _finalize(msg.content, seen_candidates, tool_call_log)

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

            # Exhausted MAX_TOOL_ROUNDS still calling tools -- force a final
            # answer without offering the tool again, using whatever it's
            # already seen.
            messages.append(
                {"role": "user", "content": "Give your final recommendation now, no more tool calls."}
            )
            response = groq_client.chat.completions.create(model=GROQ_MODEL, messages=messages)
            return _finalize(response.choices[0].message.content, seen_candidates, tool_call_log)


def _finalize(
    text: str | None, seen_candidates: dict[int, dict[str, Any]], tool_call_log: list[dict[str, Any]]
) -> dict[str, Any]:
    rec_id = _extract_recommendation_id(text or "")
    media = seen_candidates.get(rec_id) if rec_id is not None else None
    if rec_id is not None and media is None:
        logger.warning(
            "Agent's RECOMMENDATION_ID=%s doesn't match any candidate it actually saw (%s)",
            rec_id,
            sorted(seen_candidates),
        )
    return {
        "explanation": _strip_recommendation_line(text or ""),
        "media": media,
        "tool_calls": tool_call_log,
    }
