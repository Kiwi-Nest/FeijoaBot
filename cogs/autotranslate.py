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
    from modules.KiwiBot import KiwiBot

log = logging.getLogger(__name__)


class AutoTranslate(commands.Cog):
    """Cog for handling automatic and reaction-based translations."""

    def __init__(self, bot: KiwiBot) -> None:
        """Initialize the AutoTranslate cog."""
        self.bot = bot
        if not bot.config.libretranslate_host:
            log.warning("LIBRETRANSLATE_HOST not set. Translation disabled.")
            self.translator = None  # type: ignore[assignment]
        else:
            self.translator = TranslationClient(
                host=bot.config.libretranslate_host,
                session=bot.http_session,
            )

        # Caches to reduce DB load and enable smart replies
        # (user_id, guild_id) -> language_code
        self.user_lang_cache: dict[tuple[UserId, GuildId], str | None] = {}

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
            app_commands.Choice(name="Romanian 🇷🇴", value="ro"),
            app_commands.Choice(name="Bulgarian 🇧🇬", value="bg"),
            app_commands.Choice(name="English 🇺🇸", value="en"),
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
        await self.bot.user_db.set_native_language(
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
            await interaction.followup.send("❌ Auto-translation disabled.")

    async def _determine_translation_context(
        self,
        message: discord.Message,
    ) -> tuple[str, str, bool] | None:
        """Determine source and target languages based on message context.

        Returns
        -------
            tuple(source_lang, target_lang, is_native_flow) or None

        """
        # 1. Check for Reply Context (Breadcrumbs)
        if message.reference and message.reference.message_id:
            try:
                # Resolve the referenced message
                resolved_ref = message.reference.resolved
                if not isinstance(resolved_ref, discord.Message):
                    channel = message.channel
                    if isinstance(channel, discord.TextChannel | discord.Thread):
                        resolved_ref = await channel.fetch_message(message.reference.message_id)

                # Check for breadcrumbs in the referenced message
                if resolved_ref and isinstance(resolved_ref, discord.Message) and resolved_ref.content:
                    # Note: parse_breadcrumb swaps src/tgt, so context.target_lang is the original source
                    context = self.translator.parse_breadcrumb(resolved_ref.content)
                    if context:
                        # Return (source, target, is_native_flow)
                        # We use "auto" for source to be safe
                        return ("auto", context.target_lang, False)

                # 2. Check Smart Reply Context (LRU Cache)
                # If replying to a message we previously tracked (e.g., original RO message)
                if message.reference.message_id in self.msg_context_cache:
                    target_lang = self.msg_context_cache[message.reference.message_id]
                    return ("auto", target_lang, False)

            except (discord.NotFound, discord.HTTPException):
                pass  # Reference invalid or inaccessible

        # 3. Check User Native Language Configuration
        user_key = (UserId(message.author.id), GuildId(message.guild.id))

        # Check cache first
        if user_key in self.user_lang_cache:
            native_lang = self.user_lang_cache[user_key]
        else:
            native_lang = await self.bot.user_db.get_native_language(*user_key)
            self.user_lang_cache[user_key] = native_lang

        # If user has a specific setting and it's NOT English
        if native_lang and native_lang != "en":
            # Translate: Native -> English
            return (native_lang, "en", True)

        return None

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

        source_lang, target_lang, is_native_flow = context

        # Perform Translation
        translation = await self.translator.translate(
            message.clean_content,
            source=source_lang,
            target=target_lang,
        )

        if translation:
            crumb = self.translator.get_breadcrumb_string(
                # If "auto" was used, we might want to standardize the crumb,
                # but passing "auto" is acceptable or we could use the detected lang if API returns it.
                source_lang,
                target_lang,
            )

            await message.reply(f"{crumb} {translation}", mention_author=False)

            # If this was a native flow (e.g. RO -> EN), cache this message ID
            # so replies to IT can be translated back to RO (Smart Reply)
            if is_native_flow:
                self._cache_msg_context(message.id, source_lang)

    @commands.Cog.listener()
    async def on_raw_reaction_add(
        self,
        payload: discord.RawReactionActionEvent,
    ) -> None:
        """Handle flag reactions to force translation."""
        if not self.translator or payload.user_id == self.bot.user.id:
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
        except discord.NotFound:
            return

        # Ignore empty or bots
        if not message.clean_content or message.author.bot:
            return

        # Prevent spam: Check if we've already translated this message to this language
        reaction_key = (message.id, target_lang)
        if reaction_key in self.reaction_cache:
            return

        # Translate
        translation = await self.translator.translate(
            message.clean_content,
            source="auto",
            target=target_lang,
            bypass_ignore=True,  # Explicit request, ignore length checks
        )

        if translation:
            # Update cache
            self.reaction_cache[reaction_key] = None
            if len(self.reaction_cache) > self.MAX_REACTION_CACHE_SIZE:
                self.reaction_cache.popitem(last=False)

            crumb = self.translator.get_breadcrumb_string("auto", target_lang)
            with contextlib.suppress(discord.HTTPException):
                await message.reply(f"{crumb} {translation}", mention_author=False)


async def setup(bot: KiwiBot) -> None:
    """Load the AutoTranslate cog."""
    await bot.add_cog(AutoTranslate(bot))
