"""Automatic translation functionality.

Handles message events and user configuration for real-time translation.
"""

from __future__ import annotations

import contextlib
import logging
from collections import OrderedDict
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from modules.dtypes import GuildId, UserId
from modules.translation import TranslationClient

if TYPE_CHECKING:
    import aiohttp

    from modules.config import BotConfig
    from modules.ConfigDB import ConfigDB
    from modules.KiwiBot import KiwiBot
    from modules.UserDB import UserDB

log = logging.getLogger(__name__)


class AutoTranslate(commands.Cog):
    """Cog for handling automatic and reaction-based translations."""

    def __init__(
        self,
        bot: KiwiBot,
        *,
        config: BotConfig,
        http_session: aiohttp.ClientSession,
        user_db: UserDB,
        config_db: ConfigDB,
    ) -> None:
        """Initialize the AutoTranslate cog."""
        self.bot = bot
        self.config = config
        self.http_session = http_session
        self.user_db = user_db
        self.config_db = config_db
        if not self.config.libretranslate_host:
            log.warning("LIBRETRANSLATE_HOST not set. Translation disabled.")
            self.translator = None
        else:
            self.translator = TranslationClient(
                host=self.config.libretranslate_host,
                session=self.http_session,
            )

        # Caches to reduce DB load and enable smart replies
        # (user_id, guild_id) -> language_code
        self.user_lang_cache: dict[tuple[UserId, GuildId], str | None] = {}
        # (user_id, guild_id) -> bool
        self.user_autotranslate_cache: dict[tuple[UserId, GuildId], bool] = {}

        # message_id -> original_language (Simple LRU)
        self.msg_context_cache: OrderedDict[int, str] = OrderedDict()
        self.MAX_CACHE_SIZE = 5000

        # (message_id, target_lang) -> None (Simple LRU for reaction spam prevention)
        self.reaction_cache: OrderedDict[tuple[int, str], None] = OrderedDict()
        self.MAX_REACTION_CACHE_SIZE = 500

    def _cache_msg_context(self, message_id: int, lang: str) -> None:
        """Cache the language context of a message for smart replies."""
        self.msg_context_cache[message_id] = lang
        if len(self.msg_context_cache) > self.MAX_CACHE_SIZE:
            self.msg_context_cache.popitem(last=False)

    # --- Slash Commands for Configuration ---

    @app_commands.command(
        name="language",
        description="Set your native language for auto-translation.",
    )
    @app_commands.describe(lang="Your native language ('en' to disable).")
    @app_commands.choices(
        lang=[
            app_commands.Choice(name="Server Default (Reset)", value="none"),
            app_commands.Choice(name="Romanian 🇷🇴", value="ro"),
            app_commands.Choice(name="Bulgarian 🇧🇬", value="bg"),
            app_commands.Choice(name="English 🇬🇧", value="en"),
        ],
    )
    async def set_language(
        self,
        interaction: discord.Interaction,
        lang: str,
    ) -> None:
        """Set the user's preferred language."""
        if not interaction.guild:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        value = None if lang == "none" else lang
        await self.user_db.set_native_language(
            UserId(interaction.user.id),
            GuildId(interaction.guild.id),
            value,
        )

        # Update cache
        self.user_lang_cache[(UserId(interaction.user.id), GuildId(interaction.guild.id))] = value

        if value:
            await interaction.followup.send(
                f"✅ Auto-translation set to **{value.upper()}**.",
            )
        else:
            await interaction.followup.send("✅ Auto-translation reset to **Server Default**.")

    @app_commands.command(
        name="autotranslate",
        description="Toggle auto-translation for your messages.",
    )
    @app_commands.describe(enabled="Enable or disable auto-translation.")
    @app_commands.choices(
        enabled=[
            app_commands.Choice(name="True (Enable)", value=1),
            app_commands.Choice(name="False (Disable)", value=0),
        ],
    )
    async def toggle_autotranslate(
        self,
        interaction: discord.Interaction,
        enabled: int,  # 1 or 0
    ) -> None:
        """Toggle auto-translation opt-in."""
        if not interaction.guild:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        is_enabled = bool(enabled)
        await self.user_db.set_autotranslate(
            UserId(interaction.user.id),
            GuildId(interaction.guild.id),
            is_enabled,
        )

        # Update cache
        self.user_autotranslate_cache[(UserId(interaction.user.id), GuildId(interaction.guild.id))] = is_enabled

        state = "ENABLED" if is_enabled else "DISABLED"
        await interaction.followup.send(f"✅ Auto-translation is now **{state}**.")

    async def _get_user_native_language(
        self,
        user_id: UserId,
        guild_id: GuildId,
    ) -> str | None:
        """Fetch user's native language from cache or database."""
        user_key = (user_id, guild_id)
        if user_key in self.user_lang_cache:
            return self.user_lang_cache[user_key]
        native_lang = await self.user_db.get_native_language(*user_key)
        self.user_lang_cache[user_key] = native_lang
        return native_lang

    async def _get_user_autotranslate_status(
        self,
        user_id: UserId,
        guild_id: GuildId,
    ) -> bool:
        """Fetch user's autotranslate opt-in status from cache or database."""
        user_key = (user_id, guild_id)
        if user_key in self.user_autotranslate_cache:
            return self.user_autotranslate_cache[user_key]
        is_opted_in = await self.user_db.get_autotranslate(*user_key)
        self.user_autotranslate_cache[user_key] = is_opted_in
        return is_opted_in

    async def _get_reply_target_language(self, message: discord.Message) -> str | None:
        """Determine target language from reply context if available."""
        if not message.reference or not message.reference.message_id:
            return None

        try:
            # Resolve the referenced message
            resolved_ref = message.reference.resolved
            if not isinstance(resolved_ref, discord.Message):
                channel = message.channel
                if isinstance(channel, discord.TextChannel | discord.Thread):
                    resolved_ref = await channel.fetch_message(message.reference.message_id)

            # Check for breadcrumbs in the referenced message
            if resolved_ref and isinstance(resolved_ref, discord.Message) and resolved_ref.content:
                context = self.translator.parse_breadcrumb(resolved_ref.content)
                if context:
                    return context.target_lang

            # Smart Reply Cache Fallback
            if message.reference.message_id in self.msg_context_cache:
                return self.msg_context_cache[message.reference.message_id]

        except (discord.NotFound, discord.HTTPException):
            pass  # Reference invalid or inaccessible

        return None

    async def _determine_translation_context(
        self,
        message: discord.Message,
    ) -> tuple[str, str] | None:
        """Determine source and target languages based on message context.

        Returns
        -------
            tuple(source_lang, target_lang) or None

        """
        if not message.guild:
            return None

        # 1. Fetch Guild Context
        guild_config = await self.config_db.get_guild_config(GuildId(message.guild.id))
        server_default = guild_config.default_language or "en"

        # 2. Identify Source (User's Native Language)
        user_id = UserId(message.author.id)
        guild_id = GuildId(message.guild.id)
        native_lang = await self._get_user_native_language(user_id, guild_id)
        is_opted_in = await self._get_user_autotranslate_status(user_id, guild_id)

        # Default Assumption: Source is native/auto, Target is server default
        source_lang = native_lang or server_default
        target_lang = server_default

        # 3. Check for Reply Context
        reply_target = await self._get_reply_target_language(message)
        if reply_target:
            target_lang = reply_target

        # 4. Guardrail A: "Same-Language" Pass-through
        if source_lang == target_lang:
            return None

        # 5. Guardrail B: Opt-in Check
        is_standard_path = target_lang == server_default
        if is_standard_path and not is_opted_in and not message.reference:
            return None

        return (source_lang, target_lang)

    # --- Event Listeners ---

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Handle auto-translation for configured users and smart replies."""
        if not self.translator or message.author.bot or not message.guild:
            return

        # Fetch context using the new helper
        context = await self._determine_translation_context(message)

        if not context:
            return

        source_lang, target_lang = context

        # Perform Translation
        translation = await self.translator.translate(
            message.clean_content,
            source=source_lang,
            target=target_lang,
        )

        if translation:
            crumb = self.translator.get_breadcrumb_string(
                source_lang,
                target_lang,
            )

            await message.reply(f"{crumb} {translation}", mention_author=False)

            # Since strictly defined (Native or Server Default), always cache for smart replies
            self._cache_msg_context(message.id, source_lang)

    @commands.Cog.listener()
    async def on_raw_reaction_add(  # noqa: PLR0912
        self,
        payload: discord.RawReactionActionEvent,
    ) -> None:
        """Handle flag reactions to force translation."""
        if not self.translator or payload.user_id == self.bot.user.id or not payload.guild_id:
            return

        # Map emoji to target language
        # Note: Discord sends unicode emojis as strings
        target_lang = None
        emoji_name = str(payload.emoji)

        if "🇷🇴" in emoji_name:
            target_lang = "ro"
        elif "🇧🇬" in emoji_name:
            target_lang = "bg"
        elif "🇺🇸" in emoji_name or "🇬🇧" in emoji_name:
            target_lang = "en"

        if not target_lang:
            return

        # Fetch context
        channel = self.bot.get_channel(payload.channel_id)
        if not isinstance(channel, discord.TextChannel | discord.Thread):
            return

        try:
            message = await channel.fetch_message(payload.message_id)
        except (discord.NotFound, discord.Forbidden):
            return

        # Ignore empty or bots
        if not message.clean_content or message.author.bot:
            return

        # Fetch Guild Config for defaults
        guild_config = await self.config_db.get_guild_config(GuildId(payload.guild_id))
        server_default = guild_config.default_language or "en"

        # 1. Determine Source (Reaction Specific Logic)

        # Check if the AUTHOR of the message has a config
        user_key = (UserId(message.author.id), GuildId(message.guild.id))

        # We try to check cache first, then DB (using a fire-and-forget lookup or await)
        # Since on_raw_reaction is async, we can await DB
        if user_key in self.user_lang_cache:
            author_lang = self.user_lang_cache[user_key]
        else:
            author_lang = await self.user_db.get_native_language(*user_key)
            self.user_lang_cache[user_key] = author_lang

        # If unconfigured, they speak the server language
        source_lang = author_lang or server_default

        # Prevent spam: Check if we've already translated this message to this language
        reaction_key = (message.id, target_lang)
        if reaction_key in self.reaction_cache:
            return

        # Translate
        translation = await self.translator.translate(
            message.clean_content,
            source=source_lang,
            target=target_lang,
            bypass_ignore=True,  # Explicit request, ignore length checks
        )

        if translation:
            # Update cache
            self.reaction_cache[reaction_key] = None
            if len(self.reaction_cache) > self.MAX_REACTION_CACHE_SIZE:
                self.reaction_cache.popitem(last=False)

            crumb = self.translator.get_breadcrumb_string(source_lang, target_lang)
            with contextlib.suppress(discord.HTTPException):
                await message.reply(f"{crumb} {translation}", mention_author=False)


async def setup(bot: KiwiBot) -> None:
    """Load the AutoTranslate cog."""
    await bot.add_cog(
        AutoTranslate(
            bot=bot,
            config=bot.config,
            http_session=bot.http_session,
            user_db=bot.user_db,
            config_db=bot.config_db,
        ),
    )
