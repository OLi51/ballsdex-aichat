import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands
from google.genai import types

from settings.models import settings

from .client import run_chat
from .gemini_queue import GeminiQueue, QueueFullError
from ..models import AIChatSettings, ChatMessage

if TYPE_CHECKING:
    from ballsdex.core.bot import BallsDexBot

log = logging.getLogger("ballsdex.packages.aichat")

DISCORD_MESSAGE_LIMIT = 2000

# The stock Ballsdex /about description. Most people experimenting never change it, and it
# hardcodes the word "countryballs" — which contradicts a reskinned collectible (e.g. "melodies").
# We only feed the description to the AI if the owner actually customized it away from this.
STOCK_ABOUT_DEFAULT = "Collect countryballs on Discord, exchange them and battle with friends!"


def _build_system_prompt(personality: str) -> str:
    """
    Bake the bot's identity into every request by prepending the core Ballsdex settings
    (the same values that name the bot everywhere else) ahead of the admin-authored
    personality text. This way the AI always knows which bot it is and what's collected
    without the owner having to repeat it inside the personality field.

    Safeguards, because most people experimenting never touch the default settings:
    - The collectible name (which owners *do* customize) is the source of truth for identity,
      not the /about description (which they usually don't).
    - The /about description is only included if it was genuinely customized and doesn't still
      say "countryball" — otherwise the AI would introduce itself with the wrong collectible.
    - A behavioral guard tells the AI not to over-play its "I'm just a collector bot" identity
      or hide behind it to refuse ordinary requests.
    """
    bot_name = settings.bot_name
    collectible = settings.collectible_name
    collectibles = settings.plural_collectible_name
    about = (settings.about_description or "").strip()

    parts = [
        f'You are the companion AI built into the Discord bot named "{bot_name}". '
        f"In this bot, players collect and trade {collectibles} (singular: {collectible}).",
    ]

    if about and about != STOCK_ABOUT_DEFAULT and "countryball" not in about.lower():
        parts.append(f"Here is how the bot describes itself: {about}")

    parts.append(
        f"Stay in character as part of this specific bot. When people ask what you are, you are "
        f"{bot_name}'s companion, not a generic assistant."
    )

    parts.append(
        "Talk like a normal, friendly person. Don't constantly announce that you're an AI, a bot, "
        f"or a {collectible} collector, and never hide behind that identity to dodge ordinary "
        'requests (avoid lines like "I\'m just a Discord collector bot, I can\'t do that"). If you '
        "genuinely can't help with something, say so briefly and naturally without exaggerating your "
        "limitations. Keep replies concise and in character."
    )

    # The personality field has a model-level default, but guard against it being blanked out.
    parts.append(
        "--- Your personality and behavior (set by the bot owner) ---\n"
        + (personality.strip() or "Be warm, brief, and friendly.")
    )

    return "\n\n".join(parts)


def _chunks(text: str, size: int = DISCORD_MESSAGE_LIMIT) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


async def _send_reply(sender, text: str, attachment_path):
    chunks = _chunks(text)
    file = discord.File(attachment_path) if attachment_path and attachment_path.exists() else None
    for i, chunk in enumerate(chunks):
        kwargs = {"file": file} if file and i == len(chunks) - 1 else {}
        await sender(chunk, **kwargs)


def _friendly_error(exc: Exception) -> str:
    # 429 = rate limit / daily quota exhausted (common on a shared free-tier key across servers)
    if getattr(exc, "code", None) == 429:
        return "I've hit my usage limit for the moment — give me a little while and try again!"
    return "Something went wrong on my end trying to think of a reply. Try again in a bit?"


class AIChat(commands.Cog):
    """
    A chatty companion cog: mention the bot or DM it to talk, powered by Gemini.
    """

    def __init__(self, bot: "BallsDexBot"):
        self.bot = bot
        # a single queue serializes every Gemini call this bot makes (across every guild
        # it's in), so a shared API key never gets hit with concurrent requests or bursts
        # past its per-minute quota. Requests just wait their turn.
        self.queue = GeminiQueue()
        self.queue.start()

    def cog_unload(self):
        self.queue.stop()

    async def _get_settings(self) -> AIChatSettings | None:
        config = await AIChatSettings.objects.afirst()
        if not config or not config.enabled or not config.api_key:
            return None
        return config

    async def _generate(self, config: AIChatSettings, history: list[types.Content], discord_id: int):
        self.queue.set_rate(config.requests_per_minute)
        system_prompt = _build_system_prompt(config.personality)
        return await self.queue.submit(
            lambda: run_chat(
                api_key=config.api_key,
                models=config.model_chain,
                system_prompt=system_prompt,
                history=history,
                discord_id=discord_id,
                allow_stats=config.allow_stats_lookup,
                allow_artwork=config.allow_artwork,
                allow_web_search=config.allow_web_search,
            )
        )

    async def _build_history(self, channel_id: int, max_history: int) -> list[types.Content]:
        rows = [
            row
            async for row in ChatMessage.objects.filter(channel_id=channel_id).order_by("-created_at")[:max_history]
        ]
        rows.reverse()
        history: list[types.Content] = []
        for row in rows:
            part = types.Part.from_text(text=row.content)
            if row.role == ChatMessage.Role.ASSISTANT:
                history.append(types.ModelContent(parts=[part]))
            else:
                history.append(types.UserContent(parts=[part]))
        return history

    async def _remember(self, channel_id: int, author_id: int | None, role: str, content: str):
        await ChatMessage.objects.acreate(channel_id=channel_id, author_id=author_id, role=role, content=content)

    async def _trim_history(self, channel_id: int, max_history: int):
        keep_ids = [
            pk
            async for pk in ChatMessage.objects.filter(channel_id=channel_id)
            .order_by("-created_at")
            .values_list("pk", flat=True)[:max_history]
        ]
        if keep_ids:
            await ChatMessage.objects.filter(channel_id=channel_id).exclude(pk__in=keep_ids).adelete()

    async def _respond(
        self,
        channel: discord.abc.Messageable,
        channel_id: int,
        author: discord.abc.User,
        text: str,
        is_dm: bool = False,
    ):
        config = await self._get_settings()
        if not config:
            return

        # The channel whitelist only restricts server channels; DMs are always allowed (the user
        # explicitly chose to message the bot directly).
        if not is_dm and config.allowed_channels and channel_id not in config.allowed_channels:
            return

        if not text.strip():
            return

        await self._remember(channel_id, author.id, ChatMessage.Role.USER, f"{author.display_name}: {text}")
        history = await self._build_history(channel_id, config.max_history)

        try:
            async with channel.typing():
                reply, attachment_path = await self._generate(config, history, author.id)
        except QueueFullError as e:
            await channel.send(str(e))
            return
        except Exception as e:
            log.error(f"Gemini chat request failed: {e}")
            await channel.send(_friendly_error(e))
            return

        await self._remember(channel_id, None, ChatMessage.Role.ASSISTANT, reply)
        await self._trim_history(channel_id, config.max_history)

        await _send_reply(channel.send, reply, attachment_path)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not self.bot.user:
            return

        is_dm = message.guild is None
        is_mentioned = self.bot.user in message.mentions

        if not is_dm and not is_mentioned:
            return

        content = message.content
        for pattern in (f"<@{self.bot.user.id}>", f"<@!{self.bot.user.id}>"):
            content = content.replace(pattern, "")
        content = content.strip()

        await self._respond(message.channel, message.channel.id, message.author, content, is_dm=is_dm)

    @app_commands.command(name="chat")
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.checks.cooldown(1, 5, key=lambda i: i.user.id)
    async def chat(self, interaction: discord.Interaction, message: str):
        """
        Chat with the bot directly, without needing to mention it.

        Parameters
        ----------
        message: str
            What do you want to say?
        """
        await interaction.response.defer(thinking=True)
        config = await self._get_settings()
        if not config:
            await interaction.followup.send("Chat isn't set up on this server yet.", ephemeral=True)
            return

        channel_id = interaction.channel_id
        is_dm = interaction.guild is None
        if not is_dm and config.allowed_channels and channel_id not in config.allowed_channels:
            await interaction.followup.send("I can't chat in this channel.", ephemeral=True)
            return

        await self._remember(
            channel_id, interaction.user.id, ChatMessage.Role.USER, f"{interaction.user.display_name}: {message}"
        )
        history = await self._build_history(channel_id, config.max_history)

        try:
            reply, attachment_path = await self._generate(config, history, interaction.user.id)
        except QueueFullError as e:
            await interaction.followup.send(str(e))
            return
        except Exception as e:
            log.error(f"Gemini chat request failed: {e}")
            await interaction.followup.send(_friendly_error(e))
            return

        await self._remember(channel_id, None, ChatMessage.Role.ASSISTANT, reply)
        await self._trim_history(channel_id, config.max_history)

        await _send_reply(interaction.followup.send, reply, attachment_path)

    @app_commands.command(name="forget")
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def forget(self, interaction: discord.Interaction):
        """
        Clear the bot's memory of this channel's conversation.
        """
        await ChatMessage.objects.filter(channel_id=interaction.channel_id).adelete()
        await interaction.response.send_message("Alright, clean slate!", ephemeral=True)
