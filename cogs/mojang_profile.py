import logging
import re
from typing import TYPE_CHECKING, Final
from uuid import UUID

import discord
from async_mojang import API
from discord import app_commands
from discord.ext import commands

from modules.exceptions import UserError

if TYPE_CHECKING:
    from modules.KiwiBot import KiwiBot

log = logging.getLogger(__name__)

_MC_NAME_RE: Final = re.compile(r"^[a-zA-Z0-9_]{3,16}$")
_HEADS: Final = "https://mc-heads.net"


def _validate_player(value: str) -> UUID | str:
    """Parse *value* as a UUID or validate it as a Minecraft username."""
    try:
        return UUID(value)
    except ValueError as err:
        if not _MC_NAME_RE.fullmatch(value):
            msg = f"`{value}` is not a valid Minecraft username or UUID."
            raise app_commands.TransformerError(msg) from err
        return value


class PlayerTransformer(app_commands.Transformer):
    """Client-side validation: accept a Minecraft username *or* UUID."""

    async def transform(self, _interaction: discord.Interaction, value: str) -> UUID | str:
        return _validate_player(value.strip())


Player = app_commands.Transform[UUID | str, PlayerTransformer]


class MojangProfile(commands.Cog):
    bot: KiwiBot

    def __init__(self, bot: KiwiBot) -> None:
        self.bot = bot

    @commands.hybrid_command(
        name="mojangprofile",
        description="Look up a Minecraft player profile.",
    )
    @commands.cooldown(3, 10, commands.BucketType.user)
    @app_commands.describe(player="Minecraft username or UUID")
    async def mojangprofile(self, ctx: commands.Context, *, player: Player) -> None:
        async with API() as api:
            match player:
                case UUID() as uuid:
                    profile = await api.get_profile(uuid)
                case str() as username:
                    player_uuid = await api.get_uuid(username)
                    if player_uuid is None:
                        msg = f"Player `{username}` not found."
                        raise UserError(msg)
                    profile = await api.get_profile(player_uuid)
                case _:
                    msg = "Invalid input. Provide an MC username or UUID."
                    raise UserError(msg)

        if profile is None:
            msg = f"Profile not found for `{player}`."
            raise UserError(msg)

        uid = str(profile.id)

        embed = discord.Embed(
            title=profile.name,
            color=discord.Colour.green(),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_author(name=profile.name, icon_url=f"{_HEADS}/avatar/{profile.id.hex}/64")
        embed.set_thumbnail(url=f"{_HEADS}/body/{profile.id.hex}")
        embed.add_field(name="UUID", value=f"`{uid}`", inline=False)
        embed.add_field(
            name="Skin",
            value=f"[{profile.skin_variant}]({profile.skin_url})",
            inline=True,
        )

        if profile.cape_url:
            embed.add_field(name="Cape", value=f"[View]({profile.cape_url})", inline=True)

        if profile.is_legacy_profile:
            embed.set_footer(text="Legacy Profile")

        await ctx.send(embed=embed)
        log.info("Mojang profile lookup by %s for %s", ctx.author.display_name, profile.name)


async def setup(bot: KiwiBot) -> None:
    await bot.add_cog(MojangProfile(bot=bot))
