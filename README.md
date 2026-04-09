# Feijoa

Feijoa is a comprehensive Discord bot with an economy, games, moderation tools, and automated server management.

---

## Features for Server Members

This section details all commands and automated features that a typical server member can interact with.

#### 💰 Economy & Currency

This category covers commands related to earning, spending, and viewing virtual currency.

- **`/daily`**: Claim your daily currency reward.
  - Includes a random chance to hit a **jackpot** for a massive payout.
  - Provides interactive buttons to **set reminder preferences** (Once, Always, or Never).
  - Includes a button to **share your winnings** to the channel.
- **`/bal [member]`**: Check your (or another's) wallet balance and total bump count.
- **`/donate <member> <amount>`**: Give currency to another user (aliased as `/give`).
- **`/leaderboard <stat>`**: View the server's top users.
  - **Stat options**: 💰 Currency, ⬆️ Bumps, ⭐ Level, ✨ XP.
- **`/take [limb]`** & **`/harvest [organ]`**: (Guild-Specific) A high-risk, high-reward command to attempt to... _acquire_... and sell items for cash.
- **`/blackjack <bet>`**: Start an interactive game of Blackjack (21) against the bot.

---

#### ✨ Leveling & Activity

The bot tracks user activity and rewards it with XP and levels.

- **Automatic XP**: Earn XP automatically by sending messages in the server.
- **`/level rank [member]`**: Check your (or another's) current level, total XP, and see a progress bar to the next level.
- **`/level opt-out`**: Exclude yourself from the leveling system and stop gaining XP.
- **`/level opt-in`**: Re-join the leveling system and start gaining XP again.

---

#### 🤝 Social & Server Utilities

- **`/invites top`**: See the server leaderboard for who has invited the most members.
- **`/invites mylist`**: Show a list of all the members you have personally invited.
- **`/listroles`**: Lists all roles in the server, sorted by permissions and hierarchy.

---

#### 🤖 Automated Features (What Happens for You)

- **Bump Rewards**: When you successfully use `/bump` (for Disboard), the bot will automatically reward you with a random amount of currency, increment your bump stat, and post a "thank you" message.
- **Reaction Roles**: You can get roles by adding a reaction to specific messages set up by admins. Removing your reaction also removes the role.

---

#### 📈 Paper Trading

This is a full-featured paper trading simulation, allowing users to buy and sell leveraged stocks with their server currency.

- **`/stocks`**: Lists all available stocks (leveraged ETFs) with descriptions of what they track.
- **`/price`**: Gets the latest cached prices for all tradable stocks.
- **`/portfolio`**: View your complete trading portfolio.
  - Shows cash balance, total P&L, and a detailed breakdown of all open positions (long and short).
- **`/buy <ticker> <amount>`**: Open a "long" position, betting that a stock's price will rise.
- **`/short <ticker> <amount>`**: Open a "short" position, betting that a stock's price will fall.
- **`/close <position_id> [amount]`**: Close all (or a partial dollar amount) of an open position to lock in your profit or loss.

---

## Features for Staff & Administrators

This section details the commands and automated systems for server management, moderation, and configuration.

#### 🛡️ Moderation Suite

- **`/moderate ban <member> [reason] [delete_messages]`**: Bans a user with options to delete their recent message history.
- **`/moderate kick <member> [reason]`**: Kicks a user from the server.
- **`/moderate timeout <member> <duration> [reason]`**: Times out a user for a specified duration (e.g., `10m`, `1h`, `7d`).
- **`/moderate untimeout <member> [reason]`**: Removes an active timeout from a user.
- **`/moderate mute <member> [reason]`**: Mutes a user by assigning the configured Muted role.
- **`/moderate unmute <member> [reason]`**: Removes the Muted role from a user.

---

#### ⚙️ Server Configuration

- **`/config autodiscover`**: **(Recommended Setup)** Scans server channels and roles to intelligently suggest settings (e.g., finds a "mod-log" channel and "Muted" role) for you to approve.
- **`/config view`**: Displays all current bot settings for the server in a clean embed.
- **`/config channel <feature> <channel>`**: Manually sets a channel for a specific feature (e.g., `mod_log_channel_id`, `level_up_channel_id`).
- **`/config role <feature> <role>`**: Manually sets a role for a specific feature (e.g., `bumper_role_id`, `muted_role_id`, `xp_opt_out_role_id`).
- **`/config forward ...`**: A subgroup of commands to set up automatic embed forwarding from a source bot to a target channel.
- **`/config prune ...`**: A subgroup of commands to configure automatic role pruning for inactive members (setting days and roles to prune).
- **Reaction Role Debug (Context Menu)**: Right-click a message > Apps > "Debug Reaction Role" to get a detailed DM report on its validity, security (checking for permissions), and role/emoji mapping.

---

#### 🤖 Automated Backend Management

- **Mod Logging**: All staff actions executed via `/moderate` are automatically logged to the configured `mod_log_channel_id`, including the moderator, target, and reason.
- **Join/Leave Logging**: A clean, embed-based log of members joining, rejoining, or leaving is sent to the `join_leave_log_channel_id`.
- **Activity Tracking**: The bot passively monitors `on_message` and `on_interaction` events to keep a `last_active_timestamp` for all users, powering the inactivity pruner.
- **Smart Bump Reminders**: The bot listens for Disboard bumps, waits 2 hours, and then pings the `bumper_role_id`. If another 10 minutes pass, it pings the `backup_bumper_role_id`.
- **Server Stats Channels**: Automatically updates the names of designated voice channels to display live server statistics (e.g., "All members: 123", "Tag Users: 45").
- **Invite Tracking**: On `on_member_join`, the bot compares current invite uses against a cache to determine which invite was used and credits the correct inviter in the database.
- **Role Pruning**:
  - **Inactivity Pruner**: (Runs hourly) Checks all members against the `inactivity_days` setting. If a member is inactive, it removes any roles specified in the `roles_to_prune` config list.
  - **Custom Role Pruner**: (Runs hourly) Automatically deletes any roles with a name starting with `Custom: ` (configurable) that are older than 30 days.

---

#### 🖥️ Game Server Administration

(Requires `MC_GUILD_ID` and `SERVERS_PATH` to be set)

- **`/server start <name>`**: Starts a game server via its `tmux.sh` script.
- **`/server stop <name>`**: Stops a game server via its `tmux.sh` script.
- **`/server rcon <name> <command>`**: Sends an RCON command to an online server.
- **`/server list`**: Shows the status (Online/Offline) of all managed servers.
- **`/server status <name>`**: Shows detailed info for a specific server.
- **`/server refresh`**: Forces the bot to re-scan all server statuses.
