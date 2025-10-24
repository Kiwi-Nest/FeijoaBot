from __future__ import annotations

import asyncio
from collections import defaultdict
from enum import Enum, auto
from typing import TYPE_CHECKING, ClassVar, Final

import discord
from blackjack21 import Card, Dealer, Player, PlayerBase, Table
from discord import app_commands
from discord.ext import commands

from modules.dtypes import GuildId, PositiveInt, UserId
from modules.enums import StatName

if TYPE_CHECKING:
    from discord import Interaction

    from modules.CurrencyLedgerDB import EventReason
    from modules.KiwiBot import KiwiBot

SECOND_COOLDOWN: Final[int] = 1


# --- Enums and Constants ---
class ActiveHand(Enum):
    """Cleanly manages which hand is active during a split."""

    PRIMARY = auto()
    SPLIT = auto()


class GameResult(Enum):
    """Represents the final outcome of a hand for stat tracking and payouts."""

    WIN = auto()
    LOSS = auto()
    PUSH = auto()
    BLACKJACK = auto()
    SURRENDER = auto()


# Constants for magic values
BLACKJACK_VALUE = 21
BUST_RESULT = -2
DEALER_WIN_RESULT = -1

# --- Result Configuration ---
RESULT_CONFIG = {
    GameResult.WIN: {
        "stat": "wins",
        "net_mult": 1.0,
        "payout_mult": 2.0,
        "reason": "BLACKJACK_WIN",
    },
    GameResult.BLACKJACK: {
        "stat": "blackjacks",
        "net_mult": 1.5,
        "payout_mult": 2.5,
        "reason": "BLACKJACK_BLACKJACK",
    },
    GameResult.LOSS: {
        "stat": "losses",
        "net_mult": -1.0,
        "payout_mult": 0.0,
        "reason": None,
    },
    GameResult.SURRENDER: {
        "stat": "losses",
        "net_mult": -0.5,
        "payout_mult": 0.5,
        "reason": "BLACKJACK_SURRENDER_RETURN",
    },
    GameResult.PUSH: {
        "stat": "pushes",
        "net_mult": 0.0,
        "payout_mult": 1.0,
        "reason": "BLACKJACK_PUSH",
    },
}


# --- UI View: The Core of the Game ---
class BlackjackView(discord.ui.View):
    """Manages the entire game state, logic, and UI components for a single game."""

    GAME_TIMEOUT: ClassVar[float] = 180.0  # 3 minutes

    def __init__(
        self,
        bot: KiwiBot,
        user: discord.User | discord.Member,
        bet: int,
    ) -> None:
        super().__init__(timeout=self.GAME_TIMEOUT)
        self.bot = bot
        self.user = user
        self.initial_bet = bet
        self.active_hand_state: ActiveHand = ActiveHand.PRIMARY
        self.last_action: str | None = None

        self.table = Table(players=[(user.display_name, bet)], auto_deal=True)
        self.player: Player = self.table.players[0]
        self.dealer: Dealer = self.table.dealer
        self.outcome_message: str | None = None

        # Handle instant blackjack on deal
        if BLACKJACK_VALUE in (self.player.total, self.dealer.total):
            # We cannot `await` in __init__, so we run _end_game synchronously.
            # The async payout logic within _end_game will be spun off as a task.
            self._end_game()
        else:
            self._update_buttons()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Ensure that only the user who started the game can interact with the view."""
        # self.user is the player who started the game
        if interaction.user.id != self.user.id:
            await interaction.response.send_message(
                "This is not your game of blackjack. Use the `/blackjack` command to start your own.",
                ephemeral=True,
            )
            return False
        return True

    @property
    def total_bet_at_risk(self) -> int:
        """Calculates the total bet across all hands."""
        bet = self.player.bet  # Base bet, includes double down
        if self.player.split:
            bet += self.player.split.bet
        return bet

    @property
    def active_hand(self) -> Player | PlayerBase:
        """Returns the hand object that is currently being played."""
        if self.active_hand_state is ActiveHand.SPLIT and self.player.split:
            return self.player.split
        return self.player

    # --- Button and UI State Management ---
    def _update_buttons(self) -> None:
        """Clear and adds buttons based on the current game state."""
        self.clear_items()
        if self.outcome_message:  # Game is over
            self.add_item(PlayAgainButton(self.initial_bet))
            self.add_item(NewBetButton())
        else:  # Player's turn
            self.add_item(HitButton())
            self.add_item(StandButton())
            if len(self.player.hand) == 2 and not self.player.split:
                self.add_item(SurrenderButton())
            if self.player.can_double_down:
                self.add_item(DoubleDownButton())
            if self.active_hand_state is ActiveHand.PRIMARY and self.player.can_split:
                self.add_item(SplitButton())

    def disable_all_buttons(self, is_disabled: bool = True) -> None:
        """Disables or enables all buttons in the view."""
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = is_disabled

    async def check_and_charge(
        self,
        interaction: Interaction,
        amount: int,
        action_name: str,
    ) -> bool:
        """Check if the user can afford an action and deduct the amount."""
        user_id = UserId(interaction.user.id)
        guild_id = GuildId(interaction.guild.id)

        # --- DETERMINE LEDGER REASON ---
        reason: EventReason
        if action_name == "double down":
            reason = "BLACKJACK_DOUBLE_DOWN"
        elif action_name == "split":
            reason = "BLACKJACK_SPLIT"
        else:
            # Fallback, though this shouldn't be hit
            reason = "BLACKJACK_BET"

        new_balance = await self.bot.user_db.burn_currency(
            user_id=user_id,
            guild_id=guild_id,
            amount=PositiveInt(amount),
            event_reason=reason,
            ledger_db=self.bot.ledger_db,
            initiator_id=user_id,
        )

        if new_balance is None:
            # Get the balance *after* the failed burn for the error message
            balance = await self.bot.user_db.get_stat(
                user_id,
                guild_id,
                StatName.CURRENCY,
            )
            await interaction.response.send_message(
                f"You don't have enough credits to {action_name}. You need ${amount:,} but only have ${balance:,}.",
                ephemeral=True,
            )
            return False

        return True

    # --- Game Flow & Logic ---
    async def resolve_payout_and_stats(
        self,
        result: GameResult,
        bet_amount: int,
    ) -> None:
        """Update stats and process database transactions for the game's outcome."""
        # This check ensures we don't process a non-existent result
        if not (config := RESULT_CONFIG.get(result)):
            return

        # --- 1. Update In-Memory Stats ---
        guild_id = GuildId(self.user.guild.id)
        user_id = UserId(self.user.id)

        # These lines are no longer needed thanks to defaultdict:
        # if guild_id not in self.bot.blackjack_stats: ...
        # if user_id not in self.bot.blackjack_stats[guild_id]: ...

        stats = self.bot.blackjack_stats[guild_id][user_id]  # This will now work automatically

        stats[config["stat"]] += 1
        stats["net_credits"] += int(bet_amount * config["net_mult"])

        # --- 2. Process Database Payout ---
        payout = int(bet_amount * config["payout_mult"])
        payout_reason = config.get("reason")  # Get the new reason from our config

        if payout > 0 and payout_reason:
            await self.bot.user_db.mint_currency(
                user_id=user_id,
                guild_id=guild_id,
                amount=PositiveInt(payout),
                event_reason=payout_reason,
                ledger_db=self.bot.ledger_db,
                initiator_id=user_id,
            )

    def _end_game(self) -> None:
        """Determine winner, sets outcome message, and calls for payout/stat updates."""
        if not (self.dealer.stand or self.dealer.bust):
            self.dealer.play_dealer()

        def get_result(hand: Player | PlayerBase, bet: int) -> tuple[str, GameResult]:
            result = hand.result
            hand_name = "Split Hand" if hand is not self.player else "Main Hand"

            if result == BUST_RESULT:
                return (f"{hand_name}: Busted! You lose ${bet:,}.", GameResult.LOSS)
            if result == DEALER_WIN_RESULT:
                return (
                    f"{hand_name}: Dealer wins. You lose ${bet:,}.",
                    GameResult.LOSS,
                )
            if result == 0:
                return (f"{hand_name}: It's a push! Bet returned.", GameResult.PUSH)
            if result == 1:
                return (
                    f"{hand_name}: Blackjack! You win ${int(bet * 1.5):,}.",
                    GameResult.BLACKJACK,
                )
            if result in (2, 3):
                return (f"{hand_name}: You win! You get ${bet:,}.", GameResult.WIN)
            return ("", GameResult.PUSH)

        main_text, main_result = get_result(self.player, self.player.bet)
        asyncio.create_task(  # noqa: RUF006
            self.resolve_payout_and_stats(main_result, self.player.bet),
        )

        if self.player.split:
            split_text, split_result = get_result(
                self.player.split,
                self.player.split.bet,
            )
            asyncio.create_task(  # noqa: RUF006
                self.resolve_payout_and_stats(split_result, self.player.split.bet),
            )
            self.outcome_message = f"{main_text}\n{split_text}"
        else:
            self.outcome_message = main_text

        self._update_buttons()

    async def _handle_stand_or_dd(self, interaction: Interaction) -> None:
        self.disable_all_buttons()
        await interaction.response.edit_message(embed=self.create_embed(), view=self)
        await asyncio.sleep(1.5)
        self._end_game()
        await interaction.edit_original_response(embed=self.create_embed(), view=self)

    # --- Embed Creation ---
    def create_embed(self) -> discord.Embed:
        is_game_over = self.outcome_message is not None
        color = discord.Colour.blue()  # Default for ongoing game
        if is_game_over:
            # Determine color based on game outcome
            if "push" in self.outcome_message.lower():
                color = discord.Colour.light_grey()
            elif "win" in self.outcome_message.lower() or "blackjack" in self.outcome_message.lower():
                color = discord.Colour.green()
            else:
                color = discord.Colour.red()

        embed = discord.Embed(
            title=f"Blackjack | Total Bet: ${self.total_bet_at_risk:,}",
            color=color,
        )
        dealer_hand_val = self.dealer.total if is_game_over else self.dealer.hand[0].value
        dealer_hand_str = format_hand(self.dealer.hand, not is_game_over)
        embed.add_field(
            name="Dealer's Hand",
            value=f"{dealer_hand_str}\n**Total: {dealer_hand_val}**",
            inline=False,
        )

        p_active = "► " if self.active_hand_state is ActiveHand.PRIMARY and not is_game_over else ""
        embed.add_field(
            name=f"{p_active}{self.user.display_name}'s Hand",
            value=f"{format_hand(self.player.hand)}\n**Total: {self.player.total}**",
        )

        if self.player.split:
            s_active = "► " if self.active_hand_state is ActiveHand.SPLIT and not is_game_over else ""
            embed.add_field(
                name=f"{s_active}Split Hand",
                value=f"{format_hand(self.player.split.hand)}\n**Total: {self.player.split.total}**",
            )

        if self.outcome_message:
            embed.description = f"**{self.outcome_message}**"

        footer = "Game Over" if is_game_over else "It's your turn!"
        if self.last_action:
            footer += f" | {self.last_action}"
        embed.set_footer(text=footer)
        return embed


# --- Buttons ---
class HitButton(discord.ui.Button["BlackjackView"]):
    def __init__(self) -> None:
        super().__init__(
            label="Hit",
            style=discord.ButtonStyle.secondary,
            emoji="➕",  # noqa: RUF001
        )

    async def callback(self, interaction: Interaction) -> None:
        view = self.view
        active_hand = view.active_hand
        card = active_hand.play_hit()
        view.last_action = f"You hit and drew a {card}."

        if active_hand.bust:
            view.last_action += " You busted!"
            if view.player.split and view.active_hand_state is ActiveHand.PRIMARY:
                view.active_hand_state = ActiveHand.SPLIT
                view._update_buttons()  # noqa: SLF001
                await interaction.response.edit_message(
                    embed=view.create_embed(),
                    view=view,
                )
            else:
                view._end_game()  # noqa: SLF001
                await interaction.response.edit_message(
                    embed=view.create_embed(),
                    view=view,
                )
        else:
            await interaction.response.edit_message(
                embed=view.create_embed(),
                view=view,
            )


class StandButton(discord.ui.Button["BlackjackView"]):
    def __init__(self) -> None:
        super().__init__(label="Stand", style=discord.ButtonStyle.primary, emoji="✋")

    async def callback(self, interaction: Interaction) -> None:
        view = self.view
        view.last_action = f"You stood with a total of {view.active_hand.total}."
        view.active_hand.play_stand()

        if view.player.split and view.active_hand_state is ActiveHand.PRIMARY:
            view.active_hand_state = ActiveHand.SPLIT
            view._update_buttons()  # noqa: SLF001
            await interaction.response.edit_message(
                embed=view.create_embed(),
                view=view,
            )
        else:
            await view._handle_stand_or_dd(interaction)  # noqa: SLF001


class DoubleDownButton(discord.ui.Button["BlackjackView"]):
    def __init__(self) -> None:
        super().__init__(
            label="Double Down",
            style=discord.ButtonStyle.success,
            emoji="💰",
        )

    async def callback(self, interaction: Interaction) -> None:
        view = self.view
        if not await view.check_and_charge(
            interaction,
            view.initial_bet,
            "double down",
        ):
            return

        card = view.player.play_double_down()
        view.last_action = f"You doubled down and drew a {card}. Final total: {view.player.total}."
        await view._handle_stand_or_dd(interaction)  # noqa: SLF001


class SplitButton(discord.ui.Button["BlackjackView"]):
    def __init__(self) -> None:
        super().__init__(label="Split", style=discord.ButtonStyle.success, emoji="✌️")

    async def callback(self, interaction: Interaction) -> None:
        view = self.view
        if not await view.check_and_charge(
            interaction,
            view.initial_bet,
            "split",
        ):
            return

        if view.player.can_split:
            view.last_action = "You split your hand!"
            view.player.play_split()
            view._update_buttons()  # noqa: SLF001
            await interaction.response.edit_message(
                embed=view.create_embed(),
                view=view,
            )


class SurrenderButton(discord.ui.Button["BlackjackView"]):
    def __init__(self) -> None:
        super().__init__(label="Surrender", style=discord.ButtonStyle.danger, emoji="🏳️")

    async def callback(self, interaction: Interaction) -> None:
        view = self.view
        surrender_return = view.initial_bet // 2
        view.outcome_message = f"You surrendered. Half your bet (${surrender_return:,}) was returned."
        view.player._bust = True  # Internal way to mark a loss  # noqa: SLF001
        view.disable_all_buttons(True)

        await view.resolve_payout_and_stats(
            GameResult.SURRENDER,
            view.initial_bet,
        )
        view._update_buttons()  # noqa: SLF001
        await interaction.response.edit_message(embed=view.create_embed(), view=view)


class PlayAgainButton(discord.ui.Button["BlackjackView"]):
    def __init__(self, bet: int) -> None:
        super().__init__(label="Play Again", style=discord.ButtonStyle.success)
        self.bet = bet

    async def callback(self, interaction: Interaction) -> None:
        view = self.view
        user_id = UserId(interaction.user.id)
        guild_id = GuildId(interaction.guild.id)
        new_balance = await view.bot.user_db.burn_currency(
            user_id=user_id,
            guild_id=guild_id,
            amount=PositiveInt(self.bet),
            event_reason="BLACKJACK_BET",
            ledger_db=view.bot.ledger_db,
            initiator_id=user_id,
        )

        if new_balance is None:
            balance = await view.bot.user_db.get_stat(
                user_id,
                guild_id,
                StatName.CURRENCY,
            )
            await interaction.response.edit_message(
                content=f"You can't play again. You need ${self.bet:,} but only have ${balance:,}.",
                embed=None,
                view=None,
            )
            return
        new_view = BlackjackView(view.bot, interaction.user, self.bet)
        await interaction.response.edit_message(
            embed=new_view.create_embed(),
            view=new_view,
        )
        # --- END OF REPLACEMENT ---


class NewBetButton(discord.ui.Button["BlackjackView"]):
    def __init__(self) -> None:
        super().__init__(label="New Bet", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: Interaction) -> None:
        self.view.stop()
        await interaction.response.edit_message(
            content="Use the `/blackjack` command to start a new game with a new bet.",
            embed=None,
            view=None,
        )


# --- Helper & Cog ---
def format_hand(hand: list[Card], is_dealer_hidden: bool = False) -> str:
    """Format cards with suit emojis for a richer display."""
    suits = {"Hearts": "♥️", "Diamonds": "♦️", "Spades": "♠️", "Clubs": "♣️"}

    def format_card(card: Card) -> str:
        return f"`{card.rank}{suits.get(card.suit, card.suit)}`"

    if is_dealer_hidden and len(hand) > 1:
        return f"{format_card(hand[0])} `[?]`"
    return " ".join(format_card(c) for c in hand) if hand else "`Empty`"


class BlackjackCog(commands.Cog):
    def __init__(self, bot: KiwiBot) -> None:
        self.bot = bot

        # This factory creates a default stat dict for a new user
        def user_stats_factory() -> dict[str, int]:
            return {
                "wins": 0,
                "losses": 0,
                "pushes": 0,
                "blackjacks": 0,
                "net_credits": 0,
            }

        # Initialize as a nested defaultdict
        self.bot.blackjack_stats: defaultdict[int, defaultdict[int, dict[str, int]]] = defaultdict(
            lambda: defaultdict(user_stats_factory),
        )

    @commands.hybrid_command(
        name="blackjack",
        description="Start a game of Blackjack.",
    )
    @commands.cooldown(1, SECOND_COOLDOWN * 10, commands.BucketType.user)
    @app_commands.describe(bet="The amount of credits you want to bet.")
    async def blackjack(
        self,
        ctx: commands.Context,
        bet: commands.Range[int, 1],  # ty: ignore [invalid-type-form]
    ) -> None:  # ty: ignore [invalid-type-form]
        user_id = UserId(ctx.author.id)
        guild_id = GuildId(ctx.guild.id)
        new_balance = await self.bot.user_db.burn_currency(
            user_id=user_id,
            guild_id=guild_id,
            amount=PositiveInt(bet),
            event_reason="BLACKJACK_BET",
            ledger_db=self.bot.ledger_db,
            initiator_id=user_id,
        )

        if new_balance is None:
            balance = await self.bot.user_db.get_stat(
                user_id,
                guild_id,
                StatName.CURRENCY,
            )
            await ctx.send(
                f"Insufficient funds! You tried to bet ${bet:,} but only have ${balance:,}.",
                ephemeral=True,
            )
            return

        view = BlackjackView(self.bot, ctx.author, bet)
        await ctx.send(embed=view.create_embed(), view=view, ephemeral=False)

    @commands.hybrid_command(
        name="blackjack-stats",
        description="View your blackjack statistics for this server.",
    )
    @commands.cooldown(1, SECOND_COOLDOWN * 10, commands.BucketType.user)
    async def blackjack_stats(self, ctx: commands.Context) -> None:
        stats = self.bot.blackjack_stats.get(ctx.guild.id, {}).get(ctx.author.id)
        if not stats:
            await ctx.send("You haven't played any games yet!", ephemeral=True)
            return

        embed = discord.Embed(
            title=f"{ctx.author.display_name}'s Blackjack Stats",
            color=discord.Colour.gold(),
        )
        total_games = stats["wins"] + stats["losses"] + stats["pushes"] + stats["blackjacks"]
        win_rate = ((stats["wins"] + stats["blackjacks"]) / total_games * 100) if total_games > 0 else 0

        embed.add_field(name="Total Games", value=f"{total_games}")
        embed.add_field(name="Win Rate", value=f"{win_rate:.2f}%")
        embed.add_field(name="Pushes", value=f"{stats['pushes']}")
        embed.add_field(name="Wins", value=f"{stats['wins']}")
        embed.add_field(name="Losses", value=f"{stats['losses']}")
        embed.add_field(name="Blackjacks", value=f"{stats['blackjacks']}")
        embed.add_field(name="Net Credits", value=f"{stats['net_credits']:,}")
        await ctx.send(embed=embed, ephemeral=True)

    @commands.hybrid_command(
        name="blackjack-leaderboard",
        description="View the server's blackjack leaderboard.",
    )
    @commands.cooldown(1, SECOND_COOLDOWN * 10, commands.BucketType.user)
    async def blackjack_leaderboard(self, ctx: commands.Context) -> None:
        guild_stats = self.bot.blackjack_stats.get(ctx.guild.id)
        if not guild_stats:
            await ctx.send(
                "No one has played any games on this server yet!",
                ephemeral=True,
            )
            return

        sorted_players = sorted(
            guild_stats.items(),
            key=lambda item: item[1]["net_credits"],
            reverse=True,
        )
        embed = discord.Embed(
            title="Blackjack Leaderboard",
            description="Top players by net credits won.",
            color=discord.Colour.gold(),
        )
        for i, (user_id, stats) in enumerate(sorted_players[:10]):
            embed.add_field(
                name=f"{i + 1}. <@{user_id}>",
                value=f"**Net Credits:** {stats['net_credits']:,}",
                inline=False,
            )

        await ctx.send(embed=embed, ephemeral=True)


async def setup(bot: KiwiBot) -> None:
    await bot.add_cog(BlackjackCog(bot))
