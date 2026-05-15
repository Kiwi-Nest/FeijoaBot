from typing import TYPE_CHECKING

import discord
from discord import app_commands

if TYPE_CHECKING:
    from modules.BotCore import BotCore


@app_commands.context_menu(name="Who started this?")
async def who_started(interaction: discord.Interaction, message: discord.Message) -> None:
    """Appears when right-clicking a message under 'Apps'."""
    invoker = None

    if message.interaction_metadata:
        invoker = message.interaction_metadata.user
    elif message.reference and message.reference.resolved:
        invoker = message.reference.resolved.author  # type: ignore[union-attr]

    if not invoker:
        await interaction.response.send_message(
            "I couldn't find an interaction or a reply reference for this message.",
            ephemeral=True,
        )
        return

    display_name = invoker.global_name or invoker.name
    await interaction.response.send_message(
        f"This was started by: **{display_name}** (`{invoker}` | ID: {invoker.id})",
        ephemeral=True,
    )


async def setup(bot: BotCore) -> None:
    bot.tree.add_command(who_started)
