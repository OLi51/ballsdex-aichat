import logging
from pathlib import Path

from google import genai
from google.genai import types

from .tools import TOOLS, ToolContext, dispatch

log = logging.getLogger("ballsdex.packages.aichat")

MAX_TOOL_ROUNDS = 4
FALLBACK_REPLY = "I got a bit tangled up thinking about that one — mind trying again?"


async def run_chat(
    *,
    api_key: str,
    model: str,
    system_prompt: str,
    history: list[types.Content],
    discord_id: int,
) -> tuple[str, Path | None]:
    """
    Runs one full turn of conversation against Gemini, including any function calls the
    model makes, and returns (reply text, optional local image path to attach).
    """
    client = genai.Client(api_key=api_key)
    ctx = ToolContext(discord_id=discord_id)
    config = types.GenerateContentConfig(
        system_instruction=system_prompt,
        tools=[types.Tool(function_declarations=TOOLS)],
    )

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
