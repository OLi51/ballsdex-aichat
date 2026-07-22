import logging
from pathlib import Path

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from .tools import ToolContext, build_tools, dispatch

log = logging.getLogger("ballsdex.packages.aichat")

# A single question can legitimately chain several lookups — e.g. find the rarest species, then
# that species' individual copies, then its base stats, then its artwork. Four rounds is enough
# for that exact chain and nothing more, so one extra exploratory call would truncate the answer.
# Rounds only cost a request when the model actually uses them; parallel calls share a round.
MAX_TOOL_ROUNDS = 6
FALLBACK_REPLY = "I got a bit tangled up thinking about that one — mind trying again?"


def model_supports_search(model: str) -> bool:
    """
    Heuristic for whether a model can use Google Search grounding on the free tier.

    Search grounding is available on Gemini 1.5 and 2.x; on Gemini 3.x the free search-grounding
    quota is zero (a grounded call just 429s), so we don't bother attaching the search tool there.
    Web search is a secondary nicety here — the bot's real value is its own tools, so we keep this
    simple rather than maintaining an exhaustive capability table.
    """
    m = model.lower()
    return any(tag in m for tag in ("gemini-2", "gemini-1.5"))


async def _run_once(
    *,
    client: genai.Client,
    model: str,
    config: types.GenerateContentConfig,
    history: list[types.Content],
    ctx: ToolContext,
) -> tuple[str, Path | None]:
    """One model+toolset attempt, including the function-calling loop."""
    ctx.pending_attachment = None  # reset in case a prior attempt half-populated it
    contents = list(history)
    for _ in range(MAX_TOOL_ROUNDS):
        response = await client.aio.models.generate_content(model=model, contents=contents, config=config)
        calls = response.function_calls
        if not calls:
            return response.text or FALLBACK_REPLY, ctx.pending_attachment

        contents.append(response.candidates[0].content)
        response_parts = []
        for call in calls:
            result = await dispatch(call.name, dict(call.args or {}), ctx)
            response_parts.append(types.Part.from_function_response(name=call.name, response=result))
        # Function results go back as role="user", NOT "tool". Gemini 3.x rejects "tool" outright
        # ("Role 'tool' is not supported"), which silently burned the primary model on every
        # tool-using request and left anyone without a fallback model with no tool calls at all.
        contents.append(types.Content(role="user", parts=response_parts))

    return FALLBACK_REPLY, ctx.pending_attachment


async def run_chat(
    *,
    api_key: str,
    models: list[str],
    system_prompt: str,
    history: list[types.Content],
    discord_id: int,
    allow_stats: bool = False,
    allow_artwork: bool = False,
    allow_events: bool = False,
    allow_web_search: bool = False,
) -> tuple[str, Path | None]:
    """
    Runs one full turn of conversation against Gemini and returns (reply text, optional image
    path to attach).

    Robustness:
    - `models` is tried in order; if one hits its quota or errors, the next is used, so the bot
      keeps working after the primary model's daily allowance runs out.
    - Web search is only attached to a model that actually supports it (see model_supports_search),
      so we never waste a call grounding a model whose search quota is zero. If a supported model's
      grounded call still fails, the same model is retried without search before moving on.

    Tool exposure: the collection tools are always available (speaker's own data only); events,
    stats/search and artwork are gated by the owner's settings.
    """
    client = genai.Client(api_key=api_key)
    decls, allowed = build_tools(allow_stats=allow_stats, allow_artwork=allow_artwork, allow_events=allow_events)
    ctx = ToolContext(discord_id=discord_id, allowed=allowed)

    base_tools: list[types.Tool] = []
    if decls:
        base_tools.append(types.Tool(function_declarations=decls))

    # Build the ordered list of (model, toolset) attempts. For a search-capable model with web
    # search enabled, try it WITH search first, then plain; every model always has a plain attempt.
    attempts: list[tuple[str, list[types.Tool]]] = []
    for model in models:
        if allow_web_search and model_supports_search(model):
            attempts.append((model, base_tools + [types.Tool(google_search=types.GoogleSearch())]))
        attempts.append((model, base_tools))

    last_exc: Exception | None = None
    for model, tools in attempts:
        config = types.GenerateContentConfig(system_instruction=system_prompt, tools=tools or None)
        try:
            return await _run_once(client=client, model=model, config=config, history=history, ctx=ctx)
        except (genai_errors.ClientError, genai_errors.ServerError) as e:
            last_exc = e
            log.warning(f"Gemini attempt failed (model={model}), trying next: {e}")
            continue

    if last_exc:
        raise last_exc
    return FALLBACK_REPLY, ctx.pending_attachment
