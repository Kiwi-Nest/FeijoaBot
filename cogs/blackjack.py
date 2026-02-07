from __future__ import annotations

import asyncio
from collections import defaultdict
from enum import Enum, auto
from typing import TYPE_CHECKING, ClassVar, Final

import discord
from blackjack21 import DEFAULT_SUITS, Action, Card, Dealer, Deck, GameState, Player, Table
from blackjack21 import GameResult as LibGameResult
from blackjack21.exceptions import InvalidActionError, PlayFailure
from discord import app_commands
from discord.ext import commands

from modules.dtypes import GuildId, PositiveInt, UserId
from modules.enums import StatName
from modules.exceptions import UserError
from modules.guild_cog import GuildOnlyHybridCog

if TYPE_CHECKING:
    from discord import Interaction

    from modules.CurrencyLedgerDB import CurrencyLedgerDB, EventReason
    from modules.KiwiBot import KiwiBot
    from modules.UserDB import UserDB

SECOND_COOLDOWN: Final[int] = 1


# This enum is for mapping results to payouts
class GameResult(Enum):
    """Represents the final outcome of a hand for stat tracking and payouts."""

    WIN = auto()
    LOSS = auto()
    PUSH = auto()
    BLACKJACK = auto()
    SURRENDER = auto()


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
        *,
        user_db: UserDB,
        ledger_db: CurrencyLedgerDB,
    ) -> None:
        super().__init__(timeout=self.GAME_TIMEOUT)
        self.bot = bot
        self.user_db = user_db
        self.ledger_db = ledger_db
        self.user = user
        self.initial_bet = bet
        self.last_action: str | None = None

        # Using 6 decks is common in casinos.
        deck = Deck(suits=DEFAULT_SUITS, count=6)

        self.table = Table(players=[(user.display_name, bet)], deck=deck)
        self.player: Player = self.table.players[0]
        self.dealer: Dealer = self.table.dealer
        self.outcome_message: str | None = None

        try:
            self.table.start_game()
        except (InvalidActionError, RuntimeError) as e:
            # This can happen if the deck is empty even after a reset
            self.outcome_message = f"Error: Could not start game. {e}"
            self.disable_all_buttons(True)
            self.stop()
            return

        if self.table.state == GameState.ROUND_OVER:
            # Run _end_game to parse results
            asyncio.create_task(self._end_game())  # noqa: RUF006
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
        return sum(hand.bet for hand in self.player.hands)

    # --- Button and UI State Management ---
    def _update_buttons(self) -> None:
        """Clear and adds buttons based on the current game state."""
        self.clear_items()

        if self.table.state == GameState.ROUND_OVER:
            self.add_item(PlayAgainButton(self.initial_bet))
            self.add_item(NewBetButton())
        elif self.table.state == GameState.PLAYERS_TURN:
            actions = self.table.available_actions()
            if Action.HIT in actions:
                self.add_item(HitButton())
            if Action.STAND in actions:
                self.add_item(StandButton())
            if Action.DOUBLE in actions:
                self.add_item(DoubleDownButton())
            if Action.SURRENDER in actions:
                self.add_item(SurrenderButton())
            if Action.SPLIT in actions:
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

        new_balance = await self.user_db.burn_currency(
            user_id=user_id,
            guild_id=guild_id,
            amount=PositiveInt(amount),
            event_reason=reason,
            ledger_db=self.ledger_db,
            initiator_id=user_id,
        )

        if new_balance is None:
            # Get the balance *after* the failed burn for the error message
            balance = await self.user_db.get_stat(
                user_id,
                guild_id,
                StatName.CURRENCY,
            )

            msg = f"You don't have enough credits to {action_name}. You need ${amount:,} but only have ${balance:,}."
            raise UserError(msg)

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

        stats = blackjack_stats[guild_id][user_id]  # This will now work automatically

        stats[config["stat"]] += 1
        stats["net_credits"] += int(bet_amount * config["net_mult"])

        # --- 2. Process Database Payout ---
        payout = int(bet_amount * config["payout_mult"])
        payout_reason = config.get("reason")  # Get the new reason from our config

        if payout > 0 and payout_reason:
            await self.user_db.mint_currency(
                user_id=user_id,
                guild_id=guild_id,
                amount=PositiveInt(payout),
                event_reason=payout_reason,
                ledger_db=self.ledger_db,
                initiator_id=user_id,
            )

    def _map_result_to_outcome(  # noqa: PLR0911
        self,
        lib_result: LibGameResult,
        bet: int,
        hand_name: str,
    ) -> tuple[GameResult, str]:
        """Map the library's GameResult to the cog's GameResult and a message."""
        if lib_result == LibGameResult.PLAYER_BUST:
            return (GameResult.LOSS, f"{hand_name}: Busted! You lose ${bet:,}.")
        if lib_result == LibGameResult.DEALER_WIN:
            return (
                GameResult.LOSS,
                f"{hand_name}: Dealer wins. You lose ${bet:,}.",
            )
        if lib_result == LibGameResult.PUSH:
            return (GameResult.PUSH, f"{hand_name}: It's a push! Bet returned.")
        if lib_result == LibGameResult.BLACKJACK:
            return (
                GameResult.BLACKJACK,
                f"{hand_name}: Blackjack! You win ${int(bet * 1.5):,}.",
            )
        if lib_result in (LibGameResult.PLAYER_WIN, LibGameResult.DEALER_BUST):
            return (GameResult.WIN, f"{hand_name}: You win! You get ${bet:,}.")
        if lib_result == LibGameResult.SURRENDER:
            # Payout config handles returning 0.5x bet
            return (
                GameResult.SURRENDER,
                f"{hand_name}: Surrendered. Half your bet (${bet // 2:,}) returned.",
            )

        # Fallback, should not be reached
        return (GameResult.PUSH, f"{hand_name}: Push (unknown result).")

    async def _end_game(self) -> None:
        """Parse results from the table, sets outcome message, and calls for payout/stat updates."""
        # We just need to read the results from each hand.

        messages = []
        for i, hand in enumerate(self.player.hands):
            bet = hand.bet
            lib_result = hand.result

            hand_name = "Main Hand"
            if len(self.player.hands) > 1:
                hand_name = "Split Hand 1" if i == 0 else f"Split Hand {i + 1}"

            payout_reason, message_fragment = self._map_result_to_outcome(
                lib_result,
                bet,
                hand_name,
            )
            messages.append(message_fragment)
            await self.resolve_payout_and_stats(payout_reason, bet)

        self.outcome_message = "\n".join(messages)
        self._update_buttons()

    async def _handle_stand_or_dd(self, interaction: Interaction) -> None:
        """Show dealer's turn after player's turn is over."""
        self.disable_all_buttons()
        # Edit message to show the final player state before dealer plays
        await interaction.response.edit_message(embed=self.create_embed(), view=self)
        await asyncio.sleep(1.5)

        # inside the .stand() or .double_down() call
        # We just need to call _end_game() to parse and display results.
        await self._end_game()
        await interaction.edit_original_response(embed=self.create_embed(), view=self)

    # --- Embed Creation ---
    def create_embed(self) -> discord.Embed:
        is_game_over = self.table.state == GameState.ROUND_OVER

        color = discord.Colour.blue()  # Default for ongoing game
        if is_game_over and self.outcome_message:
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

        # We use table.dealer_visible_hand, which already handles hiding cards.
        dealer_hand = self.table.dealer_visible_hand
        dealer_hand_str = format_hand(dealer_hand)

        # Get the correct total to display
        dealer_hand_val: int | str
        if is_game_over:
            dealer_hand_val = self.dealer.total  # Show final total
        elif dealer_hand:
            dealer_hand_val = dealer_hand[0].value  # Show up-card's value
        else:
            dealer_hand_val = "?"  # Fallback

        embed.add_field(
            name="Dealer's Hand",
            value=f"{dealer_hand_str}\n**Total: {dealer_hand_val}**",
            inline=False,
        )

        for i, hand in enumerate(self.player.hands):
            # Highlight the hand that is currently being played
            is_active = (hand is self.table.current_hand) and not is_game_over
            active_marker = "► " if is_active else ""

            hand_name = f"{self.user.display_name}'s Hand"
            if len(self.player.hands) > 1:
                hand_name = "Split Hand 1" if i == 0 else f"Split Hand {i + 1}"

            embed.add_field(
                name=f"{active_marker}{hand_name} (Bet: ${hand.bet:,})",
                value=f"{format_hand(list(hand))}\n**Total: {hand.total}**",
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

        # Capture the current hand before the action
        current_hand = view.table.current_hand

        try:
            card = view.table.hit()
            view.last_action = f"You hit and drew a {card}."

            if current_hand and current_hand.bust:
                view.last_action += " You busted!"

        except (PlayFailure, InvalidActionError) as e:
            await interaction.response.send_message(f"Error: {e}", ephemeral=True)
            return

        if view.table.state == GameState.ROUND_OVER:  # Check for ROUND_OVER
            await view._handle_stand_or_dd(interaction)
        else:
            # Still player's turn
            view._update_buttons()
            await interaction.response.edit_message(
                embed=view.create_embed(),
                view=view,
            )


class StandButton(discord.ui.Button["BlackjackView"]):
    def __init__(self) -> None:
        super().__init__(label="Stand", style=discord.ButtonStyle.primary, emoji="✋")

    async def callback(self, interaction: Interaction) -> None:
        view = self.view

        try:
            view.last_action = f"You stood with a total of {view.table.current_hand.total}."
            view.table.stand()
        except (PlayFailure, InvalidActionError) as e:
            await interaction.response.send_message(f"Error: {e}", ephemeral=True)
            return

        if view.table.state == GameState.ROUND_OVER:  # Check for ROUND_OVER
            await view._handle_stand_or_dd(interaction)
        else:
            # Still player's turn (this 'else' is only hit if a player stands
            # on one hand and still has another split hand to play)
            view._update_buttons()
            await interaction.response.edit_message(
                embed=view.create_embed(),
                view=view,
            )


class DoubleDownButton(discord.ui.Button["BlackjackView"]):
    def __init__(self) -> None:
        super().__init__(
            label="Double Down",
            style=discord.ButtonStyle.success,
            emoji="💰",
        )

    async def callback(self, interaction: Interaction) -> None:
        view = self.view
        # Bet for DD is the bet on the *current hand*
        bet_amount = view.table.current_hand.bet

        if not await view.check_and_charge(
            interaction,
            bet_amount,
            "double down",
        ):
            return

        # Capture the current hand before the action
        current_hand = view.table.current_hand

        try:
            card = view.table.double_down()
            view.last_action = f"You doubled down and drew a {card}. Final total: {current_hand.total}."
        except (PlayFailure, InvalidActionError) as e:
            await interaction.response.send_message(f"Error: {e}", ephemeral=True)
            return

        if view.table.state == GameState.ROUND_OVER:  # Check for ROUND_OVER
            await view._handle_stand_or_dd(interaction)
        else:
            # Still player's turn (e.g., doubled on first split hand)
            view._update_buttons()
            await interaction.response.edit_message(
                embed=view.create_embed(),
                view=view,
            )


class SplitButton(discord.ui.Button["BlackjackView"]):
    def __init__(self) -> None:
        super().__init__(label="Split", style=discord.ButtonStyle.success, emoji="✌️")

    async def callback(self, interaction: Interaction) -> None:
        view = self.view
        # Bet for split is the bet on the *current hand*
        bet_amount = view.table.current_hand.bet

        if not await view.check_and_charge(
            interaction,
            bet_amount,
            "split",
        ):
            return

        try:
            view.table.split()
            view.last_action = "You split your hand!"

            if view.table.state == GameState.ROUND_OVER:
                # This happens if you split Aces
                await view._handle_stand_or_dd(interaction)
            else:
                view._update_buttons()
                await interaction.response.edit_message(
                    embed=view.create_embed(),
                    view=view,
                )
        except (PlayFailure, InvalidActionError) as e:
            await interaction.response.send_message(f"Error: {e}", ephemeral=True)


class SurrenderButton(discord.ui.Button["BlackjackView"]):
    def __init__(self) -> None:
        super().__init__(label="Surrender", style=discord.ButtonStyle.danger, emoji="🏳️")

    async def callback(self, interaction: Interaction) -> None:
        view = self.view

        try:
            view.table.surrender()
            view.last_action = "You surrendered this hand."
        except (PlayFailure, InvalidActionError) as e:
            await interaction.response.send_message(f"Error: {e}", ephemeral=True)
            return

        # or ends the turn. The result is calculated in _end_game
        if view.table.state == GameState.ROUND_OVER:  # Check for ROUND_OVER
            await view._handle_stand_or_dd(interaction)
        else:
            # Still player's turn (e.g., surrendered first split hand)
            view._update_buttons()
            await interaction.response.edit_message(
                embed=view.create_embed(),
                view=view,
            )


class PlayAgainButton(discord.ui.Button["BlackjackView"]):
    def __init__(self, bet: int) -> None:
        super().__init__(label="Play Again", style=discord.ButtonStyle.success)
        self.bet = bet

    async def callback(self, interaction: Interaction) -> None:
        view = self.view
        user_id = UserId(interaction.user.id)
        guild_id = GuildId(interaction.guild.id)
        new_balance = await view.user_db.burn_currency(
            user_id=user_id,
            guild_id=guild_id,
            amount=PositiveInt(self.bet),
            event_reason="BLACKJACK_BET",
            ledger_db=view.ledger_db,
            initiator_id=user_id,
        )

        if new_balance is None:
            balance = await view.user_db.get_stat(
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

        view.outcome_message = None
        view.last_action = "New round started."

        try:
            # start_game() on an existing table resets it for a new round
            view.table.start_game()
        except (InvalidActionError, RuntimeError) as e:
            await interaction.response.edit_message(
                content=f"Error starting new round: {e}",
                embed=None,
                view=None,
            )
            return

        # Re-assign the player reference, as table.start_game()
        # creates a new player object when it resets the round.
        view.player = view.table.players[0]

        # Check for instant blackjack again
        if view.table.state == GameState.ROUND_OVER:
            await view._end_game()
        else:
            view._update_buttons()

        await interaction.response.edit_message(
            embed=view.create_embed(),
            view=view,
        )


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
def format_hand(hand: list[Card]) -> str:
    """Format cards with suit emojis for a richer display."""
    suits = {"Hearts": "♥️", "Diamonds": "♦️", "Spades": "♠️", "Clubs": "♣️"}

    def format_card(card: Card) -> str:
        return f"`{card.rank}{suits.get(card.suit, card.suit)}`"

    return " ".join(format_card(c) for c in hand) if hand else "`Empty`"


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
blackjack_stats: defaultdict[int, defaultdict[int, dict[str, int]]] = defaultdict(
    lambda: defaultdict(user_stats_factory),
)


class BlackjackCog(GuildOnlyHybridCog):
    def __init__(self, bot: KiwiBot, *, user_db: UserDB, ledger_db: CurrencyLedgerDB) -> None:
        self.bot = bot
        self.user_db = user_db
        self.ledger_db = ledger_db

    @commands.hybrid_command(
        name="blackjack",
        description="Start a game of Blackjack.",
    )
    @commands.cooldown(1, SECOND_COOLDOWN * 5, commands.BucketType.user)
    @app_commands.describe(bet="The amount of credits you want to bet.")
    async def blackjack(
        self,
        ctx: commands.Context,
        bet: commands.Range[int, 1],
    ) -> None:
        user_id = UserId(ctx.author.id)
        guild_id = GuildId(ctx.guild.id)
        new_balance = await self.user_db.burn_currency(
            user_id=user_id,
            guild_id=guild_id,
            amount=PositiveInt(bet),
            event_reason="BLACKJACK_BET",
            ledger_db=self.ledger_db,
            initiator_id=user_id,
        )

        if new_balance is None:
            balance = await self.user_db.get_stat(
                user_id,
                guild_id,
                StatName.CURRENCY,
            )
            await ctx.send(
                f"Insufficient funds! You tried to bet ${bet:,} but only have ${balance:,}.",
                ephemeral=True,
            )
            return

        view = BlackjackView(
            self.bot,
            ctx.author,
            bet,
            user_db=self.user_db,
            ledger_db=self.ledger_db,
        )
        await ctx.send(embed=view.create_embed(), view=view, ephemeral=False)

    @commands.hybrid_command(
        name="blackjack-stats",
        description="View your blackjack statistics for this server.",
    )
    @commands.cooldown(1, SECOND_COOLDOWN * 10, commands.BucketType.user)
    async def blackjack_stats(self, ctx: commands.Context) -> None:
        stats = blackjack_stats.get(ctx.guild.id, {}).get(ctx.author.id)
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
        guild_stats = blackjack_stats.get(ctx.guild.id)
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
    await bot.add_cog(BlackjackCog(bot, user_db=bot.user_db, ledger_db=bot.ledger_db))
