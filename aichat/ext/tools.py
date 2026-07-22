import logging
from dataclasses import dataclass, field
from pathlib import Path

from django.db.models import Count, Max, Q
from django.utils import timezone
from google.genai import types

from bd_models.models import Ball, BallInstance, Special
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


# Which axis the model wants the collection ranked by. Remember `Ball.rarity` is a spawn
# WEIGHT: a lower number means the collectible spawns less often, i.e. is rarer — so "rarest"
# sorts ascending, not descending.
COLLECTION_SORTS = {
    "count": "-count",
    "rarest": "ball__rarity",
    "commonest": "-ball__rarity",
    "recent": "-latest_catch",
    "oldest": "latest_catch",
    "strongest": "-ball__attack",
    "toughest": "-ball__health",
}
MAX_COLLECTION_ROWS = 100


async def get_my_collection(
    ctx: ToolContext,
    sort_by: str = "count",
    limit: int = 25,
    name: str | None = None,
    specials_only: bool = False,
    favorites_only: bool = False,
) -> dict:
    """Species-level summary: one row per kind of collectible, with how many the player owns."""
    try:
        player = await PlayerModel.objects.aget(discord_id=ctx.discord_id)
    except PlayerModel.DoesNotExist:
        return {
            "total_owned": 0,
            "unique_species": 0,
            "species_available": 0,
            "completion_percent": 0.0,
            "specials_owned": 0,
            "favorites_owned": 0,
            "sorted_by": sort_by,
            "results": [],
            "collectible_name": settings.plural_collectible_name,
        }

    order = COLLECTION_SORTS.get(sort_by, "-count")
    limit = max(1, min(limit, MAX_COLLECTION_ROWS))

    # The default manager already excludes soft-deleted rows, so no explicit `deleted` filter.
    owned = BallInstance.objects.filter(player=player)
    if name:
        owned = owned.filter(ball__country__icontains=name)
    if specials_only:
        owned = owned.filter(special__isnull=False)
    if favorites_only:
        owned = owned.filter(favorite=True)

    # Ranking by rarity is only meaningful for collectibles that can actually spawn. A
    # rarity of 0 is the "soft-disabled" convention (still owned/visible, but weight 0 so it
    # can never be rolled) — left in, such an entry would always top the "rarest" list and
    # bury genuinely rare collectibles.
    ranked = owned.filter(ball__rarity__gt=0) if sort_by in ("rarest", "commonest") else owned

    # Grouped DB-side rather than tallying instances in Python: correct at any collection size,
    # and returns only `limit` rows instead of deserializing the whole collection.
    grouped = (
        ranked.values("ball_id", "ball__short_name", "ball__country", "ball__rarity", "ball__attack", "ball__health")
        .annotate(count=Count("id"), latest_catch=Max("catch_date"))
        .order_by(order)
    )
    unique_species = await grouped.acount()
    rows = [row async for row in grouped[:limit]]

    total = await owned.acount()
    species_available = await Ball.enabled_objects.acount()
    specials_owned = await BallInstance.objects.filter(player=player, special__isnull=False).acount()
    favorites_owned = await BallInstance.objects.filter(player=player, favorite=True).acount()

    return {
        "total_owned": total,
        "unique_species": unique_species,
        "species_available": species_available,
        "completion_percent": round(unique_species / species_available * 100, 1) if species_available else 0.0,
        "specials_owned": specials_owned,
        "favorites_owned": favorites_owned,
        "sorted_by": sort_by if sort_by in COLLECTION_SORTS else "count",
        "results": [
            {
                "name": row["ball__short_name"] or row["ball__country"],
                "count": row["count"],
                "rarity": row["ball__rarity"],
                "attack": row["ball__attack"],
                "health": row["ball__health"],
                "last_caught": row["latest_catch"].isoformat() if row["latest_catch"] else None,
            }
            for row in rows
        ],
        "collectible_name": settings.plural_collectible_name,
    }


# Per-INSTANCE sorting. `attack`/`health` here are the effective values from the core
# manager's with_stats() annotation (base stat adjusted by that individual's bonus roll),
# not the species' base stats.
INSTANCE_SORTS = {
    "recent": "-catch_date",
    "oldest": "catch_date",
    "best_attack": "-attack",
    "best_health": "-health",
    "worst_attack": "attack",
    "worst_health": "health",
    "rarest": "ball__rarity",
}


async def get_my_instances(
    ctx: ToolContext,
    name: str | None = None,
    sort_by: str = "recent",
    limit: int = 25,
    specials_only: bool = False,
    favorites_only: bool = False,
) -> dict:
    """
    Individual-copy view: each row is one specific collectible the speaker owns, with its own
    ID and stat rolls — the level of detail needed to talk about a *particular* one rather
    than a species in aggregate.
    """
    try:
        player = await PlayerModel.objects.aget(discord_id=ctx.discord_id)
    except PlayerModel.DoesNotExist:
        return {"matched": 0, "sorted_by": sort_by, "results": [], "collectible_name": settings.plural_collectible_name}

    order = INSTANCE_SORTS.get(sort_by, "-catch_date")
    limit = max(1, min(limit, MAX_COLLECTION_ROWS))

    query = BallInstance.objects.with_stats().filter(player=player)
    if name:
        query = query.filter(ball__country__icontains=name)
    if specials_only:
        query = query.filter(special__isnull=False)
    if favorites_only:
        query = query.filter(favorite=True)
    if sort_by == "rarest":
        query = query.filter(ball__rarity__gt=0)  # see get_my_collection for why

    matched = await query.acount()
    # `.values(...)` instead of iterating model instances: with_stats()'s `attack`/`health`
    # annotations collide with BallInstance's own read-only @property of the same names, so
    # materializing an annotated queryset into model instances raises AttributeError on the
    # setattr Django does internally. Dicts sidestep that entirely.
    rows = [
        row
        async for row in query.values(
            "id",
            "ball__short_name",
            "ball__country",
            "ball__rarity",
            "attack",
            "health",
            "attack_bonus",
            "health_bonus",
            "special__name",
            "special__emoji",
            "favorite",
            "tradeable",
            "catch_date",
        ).order_by(order)[:limit]
    ]

    return {
        "matched": matched,
        "sorted_by": sort_by if sort_by in INSTANCE_SORTS else "recent",
        "results": [
            {
                # Ballsdex shows instance IDs as uppercase hex, which is how players refer to them.
                "id": f"{row['id']:0X}",
                "name": row["ball__short_name"] or row["ball__country"],
                "attack": row["attack"],
                "health": row["health"],
                "attack_bonus_percent": row["attack_bonus"],
                "health_bonus_percent": row["health_bonus"],
                "special": row["special__name"],
                "special_emoji": row["special__emoji"],
                "favorite": row["favorite"],
                "tradeable": row["tradeable"],
                "rarity": row["ball__rarity"],
                "caught_at": row["catch_date"].isoformat() if row["catch_date"] else None,
            }
            for row in rows
        ],
        "collectible_name": settings.plural_collectible_name,
    }


async def get_my_balance(ctx: ToolContext) -> dict:
    """
    The speaker's currency balance.

    Only ever offered when the owner has configured a currency (see build_tools) — `Player.money`
    exists on every instance, but an instance that never set `currency_name` has no user-facing
    currency at all, and reporting a number for a thing the game doesn't acknowledge is worse
    than not answering.
    """
    balance = await PlayerModel.objects.filter(discord_id=ctx.discord_id).values_list("money", flat=True).afirst()
    return {
        "balance": balance or 0,
        "currency_name": settings.currency_name,
        "currency_plural": settings.currency_plural,
    }


async def list_special_events() -> dict:
    """
    Every non-hidden special event, each flagged as currently active or not.

    `enabled_objects` is core's own hidden=False manager, so events the owner deliberately hid are
    never listed — that's the one hard boundary. Everything else is returned with an `active` flag
    rather than filtered out, so the AI can answer "what's on now?" and "did I miss it?" from one
    call, and can say when a past event ran instead of pretending it never existed.

    `active` uses the same window core uses to decide what can actually spawn
    (countryball.py::get_random_special): a null start or end date means unbounded.
    """
    now = timezone.now()
    events = [
        {
            "name": ev["name"],
            "emoji": ev["emoji"],
            "catch_phrase": ev["catch_phrase"],
            "tradeable": ev["tradeable"],
            # Obtainable right now — the only field that matters for "what event is on?".
            "active": (ev["start_date"] is None or ev["start_date"] <= now)
            and (ev["end_date"] is None or ev["end_date"] >= now),
            "starts_at": ev["start_date"].isoformat() if ev["start_date"] else None,
            "ends_at": ev["end_date"].isoformat() if ev["end_date"] else None,
        }
        async for ev in Special.enabled_objects.values(
            "name", "emoji", "catch_phrase", "tradeable", "start_date", "end_date"
        ).order_by("-start_date")
    ]
    return {"count": len(events), "active_count": sum(e["active"] for e in events), "events": events}


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
    description="Get a summary of the CURRENT SPEAKER's own collection. Always returns overall totals "
    "(how many they own, how many distinct kinds, and completion against everything available), plus a "
    "ranked list of what they own — each entry including how many copies, its rarity, attack and health, "
    "and when they last caught one. Choose `sort_by` to match what was actually asked: their rarest, "
    "their most/least numerous, their strongest, or their most recent catches. Cannot be used to look at "
    "anyone else's collection.",
    parameters_json_schema={
        "type": "object",
        "properties": {
            "sort_by": {
                "type": "string",
                "enum": sorted(COLLECTION_SORTS),
                "description": "How to rank the results. 'count' = most copies owned (default), "
                "'commonest'/'rarest' = by how often the collectible spawns, 'recent'/'oldest' = by when "
                "they last caught one, 'strongest'/'toughest' = by attack/health.",
            },
            "limit": {
                "type": "integer",
                "description": f"How many entries to return, 1-{MAX_COLLECTION_ROWS}, default 25. Ask for "
                "more when the user wants a broad list, fewer for a quick highlight.",
            },
            "name": {
                "type": "string",
                "description": "Optional: only include collectibles whose name contains this text.",
            },
            "specials_only": {
                "type": "boolean",
                "description": "Optional: only count copies that have a special/event background.",
            },
            "favorites_only": {
                "type": "boolean",
                "description": "Optional: only count copies the user marked as favorite.",
            },
        },
    },
)

_DECL_INSTANCES = types.FunctionDeclaration(
    name="get_my_instances",
    description="List the CURRENT SPEAKER's INDIVIDUAL copies — one entry per specific collectible they "
    "own, each with its own ID, its personal attack/health (including that copy's bonus roll), whether "
    "it's a special or a favorite, and when it was caught. Use this instead of get_my_collection whenever "
    "the question is about particular copies rather than totals — e.g. their best-rolled one, a specific "
    "collectible's stats, their most recent catches, or their specials. Cannot see anyone else's.",
    parameters_json_schema={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Optional: only copies whose collectible name contains this text.",
            },
            "sort_by": {
                "type": "string",
                "enum": sorted(INSTANCE_SORTS),
                "description": "How to rank. 'recent'/'oldest' by catch time (default recent), "
                "'best_attack'/'best_health' for the strongest individual rolls, 'worst_attack'/"
                "'worst_health' for the weakest, 'rarest' by how rarely the collectible spawns.",
            },
            "limit": {
                "type": "integer",
                "description": f"How many copies to return, 1-{MAX_COLLECTION_ROWS}, default 25.",
            },
            "specials_only": {
                "type": "boolean",
                "description": "Optional: only copies with a special/event background.",
            },
            "favorites_only": {
                "type": "boolean",
                "description": "Optional: only copies marked as favorite.",
            },
        },
    },
)

_DECL_BALANCE = types.FunctionDeclaration(
    name="get_my_balance",
    description="Get how much in-game currency the CURRENT SPEAKER has. Use it whenever they ask about "
    "their balance, how much they've got, or whether they can afford something. Cannot see anyone "
    "else's balance.",
    parameters_json_schema={"type": "object", "properties": {}},
)

_DECL_EVENTS = types.FunctionDeclaration(
    name="list_special_events",
    description="List this game's special events — the limited-time backgrounds collectibles can be "
    "caught with. Returns every public event, each with an `active` flag saying whether it can be "
    "caught RIGHT NOW, plus its name, emoji, catch phrase, whether those cards are tradeable, and its "
    "start/end dates. Use it for 'what event is on?', 'is X still available?', or when the speaker asks "
    "about a special. Only report an event as ongoing if `active` is true — the list includes finished "
    "and not-yet-started events too. Refer to events by NAME; the emoji is decoration, not a substitute.",
    parameters_json_schema={"type": "object", "properties": {}},
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

_CTX_AWARE = {"get_my_collection", "get_my_instances", "get_my_balance", "get_ball_image"}
_HANDLERS = {
    "get_my_collection": get_my_collection,
    "get_my_instances": get_my_instances,
    "get_my_balance": get_my_balance,
    "list_special_events": list_special_events,
    "get_ball_info": get_ball_info,
    "search_collectibles": search_collectibles,
    "get_ball_image": get_ball_image,
}


def build_tools(
    allow_stats: bool, allow_artwork: bool, allow_events: bool = False
) -> tuple[list[types.FunctionDeclaration], set[str]]:
    """
    Build the tool set for a request based on the owner's settings.

    The speaker's own-collection tools (aggregate and per-instance) are always available — they
    only ever reveal the caller's own data, and are bound to their Discord ID rather than any
    ID the model could name. Balance follows the same own-data rule but is only offered when the
    instance actually has a currency configured, mirroring how core's own money cog self-disables.
    Events, stats/search and artwork are OFF by default so a curious user can't coax the AI into
    leaking details, images, or unannounced events.
    """
    decls = [_DECL_COLLECTION, _DECL_INSTANCES]
    if allow_events:
        decls.append(_DECL_EVENTS)
    if settings.currency_enabled:
        decls.append(_DECL_BALANCE)
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
