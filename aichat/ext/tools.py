import logging
from dataclasses import dataclass, field
from pathlib import Path

from google.genai import types

from bd_models.models import Ball, BallInstance
from bd_models.models import Player as PlayerModel
from settings.models import settings

log = logging.getLogger("ballsdex.packages.aichat")


@dataclass
class ToolContext:
    """
    Per-request context threaded into tool calls.

    - `discord_id` is bound by the cog to whoever is actually talking, so the model can never
      ask to see someone else's collection just by naming their user ID.
    - `allowed` is the set of tool names the owner has enabled for this request. dispatch()
      refuses anything outside it, so even a hallucinated call to a disabled tool is a no-op.
    """

    discord_id: int
    allowed: set[str] = field(default_factory=set)
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
    # enabled_objects only: never leak stats for disabled / unreleased / admin-only collectibles.
    ball = await Ball.enabled_objects.filter(country__icontains=name).afirst()
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


async def search_collectibles(name: str | None = None, limit: int = 15) -> dict:
    # enabled_objects only, same leak protection as get_ball_info.
    query = Ball.enabled_objects.all()
    if name:
        query = query.filter(country__icontains=name)
    limit = max(1, min(limit, 25))
    results = [
        {"name": ball.country, "rarity": ball.rarity, "capacity_name": ball.capacity_name}
        async for ball in query.order_by("rarity")[:limit]
    ]
    return {"count": len(results), "results": results}


async def get_ball_image(ctx: ToolContext, name: str) -> dict:
    # enabled_objects only: never surface artwork for disabled / unreleased collectibles.
    ball = await Ball.enabled_objects.filter(country__icontains=name).afirst()
    if not ball or not ball.collection_card:
        return {"found": False}
    ctx.pending_attachment = Path(ball.collection_card.path)
    return {"found": True, "name": ball.country}


# --- Tool declarations, defined individually so the set can be built per-request ---

_DECL_COLLECTION = types.FunctionDeclaration(
    name="get_my_collection",
    description="Get a summary of the CURRENT SPEAKER's own collection: total owned, unique species, "
    "and the top entries by how many copies are owned. Cannot be used to look at anyone else's collection.",
    parameters_json_schema={
        "type": "object",
        "properties": {"limit": {"type": "integer", "description": "Max number of top entries to return, default 10"}},
    },
)

_DECL_BALL_INFO = types.FunctionDeclaration(
    name="get_ball_info",
    description="Look up game stats (rarity, health, attack, special capacity) for a specific released "
    "collectible by name.",
    parameters_json_schema={
        "type": "object",
        "properties": {"name": {"type": "string", "description": "Name of the collectible to look up"}},
        "required": ["name"],
    },
)

_DECL_SEARCH = types.FunctionDeclaration(
    name="search_collectibles",
    description="Search released collectibles by name fragment (or list some), returning name, rarity and "
    "capacity for each. Useful for discovery questions like 'what fire-type ones exist'.",
    parameters_json_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Optional name fragment to filter by"},
            "limit": {"type": "integer", "description": "Max results, 1-25, default 15"},
        },
    },
)

_DECL_IMAGE = types.FunctionDeclaration(
    name="get_ball_image",
    description="Fetch the artwork for a specific released collectible so it can be shown as an attachment.",
    parameters_json_schema={
        "type": "object",
        "properties": {"name": {"type": "string", "description": "Name of the collectible"}},
        "required": ["name"],
    },
)

_CTX_AWARE = {"get_my_collection", "get_ball_image"}
_HANDLERS = {
    "get_my_collection": get_my_collection,
    "get_ball_info": get_ball_info,
    "search_collectibles": search_collectibles,
    "get_ball_image": get_ball_image,
}


def build_tools(allow_stats: bool, allow_artwork: bool) -> tuple[list[types.FunctionDeclaration], set[str]]:
    """
    Build the tool set for a request based on the owner's settings.

    get_my_collection is always available (it only ever reveals the speaker's own data, safely
    guarded). Stats/search and artwork are OFF by default so a curious user can't coax the AI into
    leaking details or images of rare, unreleased or admin-only collectibles.
    """
    decls = [_DECL_COLLECTION]
    if allow_stats:
        decls += [_DECL_BALL_INFO, _DECL_SEARCH]
    if allow_artwork:
        decls.append(_DECL_IMAGE)
    return decls, {d.name for d in decls}


async def dispatch(name: str, args: dict, ctx: ToolContext) -> dict:
    if name not in ctx.allowed:
        return {"error": f"The {name} tool is disabled on this server."}
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
