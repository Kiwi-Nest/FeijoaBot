"""Automatic translation functionality.

Handles message events and user configuration for real-time translation.
"""

from __future__ import annotations

import contextlib
import logging
import re
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


class TranslateSelectView(discord.ui.View):
    """Button view for selecting translation target language."""

    def __init__(
        self,
        cog: AutoTranslate,
        original_message: discord.Message,
        timeout: float = 60,
    ) -> None:
        super().__init__(timeout=timeout)
        self.cog = cog
        self.original_message = original_message

    @discord.ui.button(label="English", emoji="🇬🇧", style=discord.ButtonStyle.secondary)
    async def english(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await self.cog._perform_reactive_translation(interaction, self.original_message, target_lang="en", show_hint=True)

    @discord.ui.button(label="Bulgarian", emoji="🇧🇬", style=discord.ButtonStyle.secondary)
    async def bulgarian(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await self.cog._perform_reactive_translation(interaction, self.original_message, target_lang="bg", show_hint=True)

    @discord.ui.button(label="Romanian", emoji="🇷🇴", style=discord.ButtonStyle.secondary)
    async def romanian(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await self.cog._perform_reactive_translation(interaction, self.original_message, target_lang="ro", show_hint=True)


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

        # Patterns for cleanup
        self.mentionable_pattern = re.compile(r"<(?:#|@&?)\d{18,20}>")
        self.emoji_pattern = re.compile(r"<:[a-zA-Z0-9_-]{2,32}:(\d{18,20})>")
        self.url_pattern = re.compile(
            r"https?:\/\/(www\.)?[-a-zA-Z0-9@:%._\+~#=]{1,256}\.[a-zA-Z0-9()]{1,6}\b([-a-zA-Z0-9()@:%_\+.~#?&//=]*)",
        )

        self.placeholder_check_pattern = re.compile(r"<span translate=\"no\">(%[emu])</span>")

        # Context menu must be created as a class instance in Cogs
        self.translate_ctx_menu = app_commands.ContextMenu(
            name="Translate",
            callback=self.translate_message,
        )
        self.bot.tree.add_command(self.translate_ctx_menu)

    async def cog_unload(self) -> None:
        """Clean up when the cog is unloaded."""
        self.bot.tree.remove_command(self.translate_ctx_menu.name, type=self.translate_ctx_menu.type)

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
            app_commands.Choice(name="Server Default (English)", value="none"),
            app_commands.Choice(name="English 🇬🇧", value="en"),
            app_commands.Choice(name="Bulgarian 🇧🇬", value="bg"),
            app_commands.Choice(name="Romanian 🇷🇴", value="ro"),
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

        user_id = UserId(interaction.user.id)
        guild_id = GuildId(interaction.guild.id)
        user_key = (user_id, guild_id)

        # Fetch server default language
        guild_config = await self.config_db.get_guild_config(guild_id)
        server_default = guild_config.default_language or "en"

        value = None if lang == "none" else lang
        await self.user_db.set_native_language(user_id, guild_id, value)

        # Smart auto-toggle: enable autotranslate if user language differs from server default
        effective_lang = value or server_default
        should_autotranslate = effective_lang != server_default

        await self.user_db.set_autotranslate(user_id, guild_id, should_autotranslate)

        # Update caches
        self.user_lang_cache[user_key] = value
        self.user_autotranslate_cache[user_key] = should_autotranslate

        # Build response message
        if value:
            if should_autotranslate:
                msg = (
                    f"✅ Language set to **{value.upper()}**. "
                    f"Auto-translation is now **ON** (server language is {server_default.upper()})."
                )
            else:
                msg = f"✅ Language set to **{value.upper()}**. Auto-translation is **OFF** since you speak the server language."
        else:
            msg = f"✅ Language reset to **Server Default ({server_default.upper()})**. Auto-translation is **OFF**."

        await interaction.followup.send(msg)

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

    async def translate_message(
        self,
        interaction: discord.Interaction,
        message: discord.Message,
    ) -> None:
        """Translate a message on demand via right-click context menu."""
        if not self.translator:
            await interaction.response.send_message(
                "Translation service is not available.",
                ephemeral=True,
            )
            return

        if not message.content:
            await interaction.response.send_message(
                "This message has no text to translate.",
                ephemeral=True,
            )
            return

        if not interaction.guild:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        # Check if user has a language preference
        user_lang = await self._get_user_native_language(
            UserId(interaction.user.id),
            GuildId(interaction.guild.id),
        )

        if user_lang:
            # Translate directly to their preferred language
            await self._perform_reactive_translation(interaction, message, target_lang=user_lang, show_hint=False)
        else:
            # Show language selection buttons
            view = TranslateSelectView(
                cog=self,
                original_message=message,
                timeout=60,
            )
            await interaction.response.send_message(
                "Translate to which language?",
                view=view,
                ephemeral=True,
            )

    async def _unwanted_aware_translate(
        self,
        text: str,
        *,
        target: str,
        source: str = "auto",
        bypass_ignore: bool = False,
    ) -> str | None:
        urls: list[str] = []
        emojis: list[str] = []
        mentionables: list[str] = []

        text = re.sub(
            self.mentionable_pattern,
            lambda m: (mentionables.append(m.group(0)), '<span translate="no">%m</span>')[1],
            text,
        )
        text = re.sub(self.emoji_pattern, lambda m: (emojis.append(m.group(0)), '<span translate="no">%e</span>')[1], text)
        text = re.sub(self.url_pattern, lambda m: (urls.append(m.group(0)), '<span translate="no">%u</span>')[1], text)

        # Check if string is made up only from those untranslatable entries, if it is, skip
        if not re.sub(self.placeholder_check_pattern, "", text):
            return None

        def desubstitute(regex_match: re.Match[str]) -> str:
            match regex_match.group(1):
                case "%u":
                    return urls.pop(0)
                case "%e":
                    return emojis.pop(0)
                case "%m":
                    return mentionables.pop(0)

            return ""  # Can't happen

        translated = await self.translator.translate(
            text=text,
            source=source,
            target=target,
            bypass_ignore=bypass_ignore,
        )

        if translated:
            translated = re.sub(self.placeholder_check_pattern, desubstitute, translated)

        return translated

    async def _perform_reactive_translation(
        self,
        interaction: discord.Interaction,
        message: discord.Message,
        target_lang: str,
        *,
        show_hint: bool = False,
    ) -> None:
        """Perform translation and respond/edit the ephemeral message."""
        # Defer if not already responded
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

        # Translate
        if translated := await self._unwanted_aware_translate(
            message.content,
            target=target_lang,
            bypass_ignore=True,
        ):  # User explicitly asked, so translate even short text
            breadcrumb = self.translator.get_breadcrumb_string("??", target_lang)
            response = f"{breadcrumb} {translated}"

            # Add onboarding hint if user has no language preference
            if show_hint:
                response += "\n\n💡 **Tip:** Set your language with `/language` to get automatic translations."
        else:
            response = "Could not translate this message (it may already be in the target language)."

        await interaction.edit_original_response(content=response, view=None)

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

        except discord.NotFound, discord.HTTPException:
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
        if translation := await self._unwanted_aware_translate(message.content, source=source_lang, target=target_lang):
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
        except discord.NotFound, discord.Forbidden:
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
        if translation := await self._unwanted_aware_translate(
            message.content,
            source=source_lang,
            target=target_lang,
            bypass_ignore=True,
        ):  # Explicit request, ignore length checks
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
