import logging
from dataclasses import dataclass
from pathlib import Path

import aiohttp
from google.genai import types

from bd_models.models import Ball, BallInstance
from bd_models.models import Player as PlayerModel
from settings.models import settings

log = logging.getLogger("ballsdex.packages.aichat")


@dataclass
class ToolContext:
    """
    Per-request context threaded into tool calls. `discord_id` is bound by the cog to
    whoever is actually talking, so the model can never ask to see someone else's
    collection just by naming their user ID.
    """

    discord_id: int
    pending_attachment: Path | None = None


def _ball_display_name(ball: Ball) -> str:
    return ball.short_name or ball.country


async def get_my_collection(ctx: ToolContext, limit: int = 10) -> dict:
    try:
        player = await PlayerModel.objects.aget(discord_id=ctx.discord_id)
    except PlayerModel.DoesNotExist:
        return {"total_owned": 0, "unique_species": 0, "top_by_count": []}

    total = await BallInstance.objects.filter(player=player, deleted=False).acount()
    counts: dict[str, int] = {}
    async for instance in BallInstance.objects.filter(player=player, deleted=False).select_related("ball")[:500]:
        name = _ball_display_name(instance.ball)
        counts[name] = counts.get(name, 0) + 1
    top = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:limit]
    return {
        "total_owned": total,
        "unique_species": len(counts),
        "top_by_count": [{"name": n, "count": c} for n, c in top],
        "collectible_name": settings.plural_collectible_name,
    }


async def get_ball_info(name: str) -> dict:
    ball = await Ball.objects.filter(country__icontains=name).afirst()
    if not ball:
        return {"found": False}
    return {
        "found": True,
        "name": ball.country,
        "rarity": ball.rarity,
        "health": ball.health,
        "attack": ball.attack,
        "capacity_name": ball.capacity_name,
        "capacity_description": ball.capacity_description,
        "credits": ball.credits,
        "tradeable": ball.tradeable,
    }


async def get_ball_image(ctx: ToolContext, name: str) -> dict:
    ball = await Ball.objects.filter(country__icontains=name).afirst()
    if not ball or not ball.collection_card:
        return {"found": False}
    ctx.pending_attachment = Path(ball.collection_card.path)
    return {"found": True, "name": ball.country}


async def generate_music(prompt: str) -> dict:
    # imported lazily to avoid a hard import cycle with models.py at app load time
    from ..models import AIChatSettings

    config = await AIChatSettings.objects.afirst()
    if not config or not config.music_enabled:
        return {
            "available": False,
            "message": "Music generation isn't configured on this server. The bot owner needs to set "
            "music_api_url and music_api_key in the AI chat settings admin page — no provider is bundled "
            "by default.",
        }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                config.music_api_url,
                json={"prompt": prompt},
                headers={"Authorization": f"Bearer {config.music_api_key}"},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    return {"available": True, "success": False, "error": f"Provider returned HTTP {resp.status}"}
                data = await resp.json()
                return {"available": True, "success": True, "result": data}
    except Exception as e:
        log.error(f"Music generation request failed: {e}")
        return {"available": True, "success": False, "error": str(e)}


TOOLS = [
    types.FunctionDeclaration(
        name="get_my_collection",
        description="Get a summary of the CURRENT SPEAKER's own collection: total owned, unique species, "
        "and the top entries by how many copies are owned. Cannot be used to look at anyone else's collection.",
        parameters_json_schema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max number of top entries to return, default 10"},
            },
        },
    ),
    types.FunctionDeclaration(
        name="get_ball_info",
        description="Look up game stats (rarity, health, attack, special capacity) for a specific "
        "collectible by name.",
        parameters_json_schema={
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Name of the collectible to look up"}},
            "required": ["name"],
        },
    ),
    types.FunctionDeclaration(
        name="get_ball_image",
        description="Fetch the artwork for a specific collectible so it can be shown as an attachment in chat.",
        parameters_json_schema={
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Name of the collectible"}},
            "required": ["name"],
        },
    ),
    types.FunctionDeclaration(
        name="generate_music",
        description="Generate a short music clip from a text prompt. Only works if the server owner has "
        "configured a music generation provider; otherwise reports that it's unavailable and why.",
        parameters_json_schema={
            "type": "object",
            "properties": {"prompt": {"type": "string", "description": "Description of the music to generate"}},
            "required": ["prompt"],
        },
    ),
]

_CTX_AWARE = {"get_my_collection", "get_ball_image"}
_HANDLERS = {
    "get_my_collection": get_my_collection,
    "get_ball_info": get_ball_info,
    "get_ball_image": get_ball_image,
    "generate_music": generate_music,
}


async def dispatch(name: str, args: dict, ctx: ToolContext) -> dict:
    handler = _HANDLERS.get(name)
    if not handler:
        return {"error": f"Unknown tool {name}"}
    try:
        if name in _CTX_AWARE:
            return await handler(ctx, **args)
        return await handler(**args)
    except Exception as e:
        log.error(f"Tool {name} failed: {e}")
        return {"error": str(e)}
