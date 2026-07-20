import logging
from pathlib import Path

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from .tools import ToolContext, build_tools, dispatch

log = logging.getLogger("ballsdex.packages.aichat")

MAX_TOOL_ROUNDS = 4
FALLBACK_REPLY = "I got a bit tangled up thinking about that one — mind trying again?"


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
        contents.append(types.Content(role="tool", parts=response_parts))

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
    allow_web_search: bool = False,
) -> tuple[str, Path | None]:
    """
    Runs one full turn of conversation against Gemini and returns (reply text, optional image
    path to attach).

    Robustness:
    - `models` is tried in order; if one hits its quota or errors, the next is used, so the bot
      keeps working after the primary model's daily allowance runs out.
    - When web search is enabled, each model is first tried WITH Google Search grounding; if that
      specific combination is rejected (some models/tiers don't allow it), it retries the same
      model without search rather than failing outright.

    Tool exposure: the collection tool is always available (speaker's own data only); stats/search
    and artwork are gated by the owner's settings.
    """
    client = genai.Client(api_key=api_key)
    decls, allowed = build_tools(allow_stats=allow_stats, allow_artwork=allow_artwork)
    ctx = ToolContext(discord_id=discord_id, allowed=allowed)

    base_tools: list[types.Tool] = []
    if decls:
        base_tools.append(types.Tool(function_declarations=decls))

    # Tool variants tried per model: with search first (if enabled), then a plain fallback.
    tool_variants: list[list[types.Tool]] = []
    if allow_web_search:
        tool_variants.append(base_tools + [types.Tool(google_search=types.GoogleSearch())])
    tool_variants.append(base_tools)

    last_exc: Exception | None = None
    for model in models:
        for i, tools in enumerate(tool_variants):
            config = types.GenerateContentConfig(
                system_instruction=system_prompt,
                tools=tools or None,
            )
            try:
                return await _run_once(client=client, model=model, config=config, history=history, ctx=ctx)
            except genai_errors.ClientError as e:
                last_exc = e
                # 400 usually means a bad tool combination (e.g. search not supported) — drop search
                # and retry the same model with the next variant before giving up on it.
                if getattr(e, "code", None) == 400 and i + 1 < len(tool_variants):
                    log.warning(f"Model {model} rejected tool combo, retrying without search: {e}")
                    continue
                # 429 / auth / other client errors: this model won't help, move to the next model.
                log.warning(f"Model {model} client error, trying next model: {e}")
                break
            except genai_errors.ServerError as e:
                last_exc = e
                log.warning(f"Model {model} server error, trying next model: {e}")
                break

    if last_exc:
        raise last_exc
    return FALLBACK_REPLY, ctx.pending_attachment
