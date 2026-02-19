
import discord
from discord import app_commands
from discord.ext import commands

from modules.dtypes import GuildId, NonNegativeInt, UserId
from modules.enums import StatName
from modules.KiwiBot import KiwiBot
from modules.UserDB import UserDB


class LeaderboardView(discord.ui.View):
    """A view for paginating through the server leaderboard."""

    def __init__(
        self,
        bot: KiwiBot,
        data: list[tuple[int, UserId, NonNegativeInt]],
        stat_choice: app_commands.Choice[str],
        per_page: int = 10,
    ) -> None:
        super().__init__(timeout=180)
        self.bot = bot
        self.data = data
        self.stat_choice = stat_choice
        self.per_page = per_page
        self.current_page = 0
        self.max_page = (len(self.data) - 1) // self.per_page

    async def get_page_embed(self) -> discord.Embed:
        """Generate the embed for the current page."""
        self.previous_button.disabled = self.current_page == 0
        self.next_button.disabled = self.current_page >= self.max_page

        start = self.current_page * self.per_page
        end = start + self.per_page
        page_data = self.data[start:end]

        embed = discord.Embed(
            title=f"{self.stat_choice.name} Leaderboard",
            color=discord.Color.gold(),
        )

        description = []
        for rank, user_id, value in page_data:
            user_mention = f"<@{user_id}>"
            prefix = {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, f"**{rank}.**")

            if self.stat_choice.value == "level":
                description.append(f"{prefix} {user_mention} - Level {value}")
            else:
                description.append(f"{prefix} {user_mention} - `{value:,}`")

        if not description:
            description.append("The leaderboard is empty!")

        embed.description = "\n".join(description)
        embed.set_footer(text=f"Page {self.current_page + 1} / {self.max_page + 1}")
        return embed

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary, emoji="⬅️")
    async def previous_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        self.current_page = max(0, self.current_page - 1)
        await interaction.response.edit_message(embed=await self.get_page_embed(), view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary, emoji="➡️")
    async def next_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        self.current_page = min(self.max_page, self.current_page + 1)
        await interaction.response.edit_message(embed=await self.get_page_embed(), view=self)


class Leaderboard(commands.Cog):
    def __init__(self, bot: KiwiBot, user_db: UserDB) -> None:
        self.bot = bot
        self.user_db = user_db

    @app_commands.command(
        name="leaderboard",
        description="Displays the server leaderboard for a specific stat.",
    )
    @app_commands.choices(
        stat=[
            app_commands.Choice(name="💰 Currency", value=StatName.CURRENCY.value),
            app_commands.Choice(name="⬆️ Bumps", value=StatName.BUMPS.value),
            app_commands.Choice(name="⭐ Level", value=StatName.LEVEL.value),
            app_commands.Choice(name="✨ XP", value=StatName.XP.value),
        ],
    )
    async def leaderboard(self, interaction: discord.Interaction, stat: app_commands.Choice[str]) -> None:
        await interaction.response.defer()

        # Fetch the leaderboard data from the database
        stat_enum = StatName(stat.value)

        top_users = await self.user_db.get_leaderboard(
            GuildId(interaction.guild.id),
            stat_enum,
            limit=200,
        )

        if not top_users:
            await interaction.followup.send(f"Nobody is on the {stat.name} leaderboard yet!", ephemeral=True)
            return

        view = LeaderboardView(self.bot, top_users, stat)
        embed = await view.get_page_embed()
        await interaction.followup.send(embed=embed, view=view)


async def setup(bot: KiwiBot) -> None:
    await bot.add_cog(Leaderboard(bot=bot, user_db=bot.user_db))
