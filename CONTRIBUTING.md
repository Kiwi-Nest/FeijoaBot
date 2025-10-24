# Contributing to the Project

To ensure a smooth development process, please follow these guidelines.

## Tooling & Setup

- **IDE**: PyCharm is recommended for its excellent type checking and database tools.
- **Package Management**: `uv` is the preferred tool for managing dependencies and virtual environments.
- **Linting & Formatting**: `ruff` is used for both linting and code formatting.
- **Type Checking**: `ty` (or `mypy`/`pyright`) should be used to statically check types.

Before each commit, please run the following terminal commands to ensure code quality:

```bash
uvx ruff format .
uvx ruff check --fix .
uvx ty check .
```

---

## Core Architectural Principles

This project follows a strict separation of concerns to keep the codebase clean, reusable, and easy to maintain. Please adhere to these patterns.

### 1\. Separation of Concerns: `cogs/` vs. `modules/`

The project is divided into two main directories:

- **`cogs/` (The Frontend)**: This contains all Discord-facing logic. Each file is a `Cog` that implements slash commands, listeners, and UI components (like buttons and views). Cogs are responsible for _how_ a feature is presented to the user.
- **`modules/` (The Backend)**: This contains the core backend logic. It includes database abstractions (`UserDB`, `ConfigDB`), shared utilities, and data structures (`dtypes.py`). Modules handle the actual work and are completely independent of Discord.

**Golden Rule:** A cog should handle user interaction and then call a method from a module to perform the underlying action. **Never put core logic or raw SQL queries inside a cog.**

### 2\. Database Abstraction

All database interactions **must** go through the abstraction layer provided in `modules/`.

- To interact with user stats (currency, XP), use methods from `self.bot.user_db`.
- To manage guild-specific settings, use methods from `self.bot.config_db`.
- These modules handle atomic transactions and data integrity, so you don't have to.

### 3\. Strong and Specific Typing

We use Python's typing features extensively to prevent bugs. Always use them.

- **Use `NewType` for IDs**: In `modules/dtypes.py`, we define types like `UserId` and `GuildId`. Use these instead of `int` to prevent accidentally mixing up different types of IDs. A static type checker will catch mismatches.
- **Use `StrEnum` for Choices**: For fixed sets of options (like stat names in `modules/enums.py`), use a `StrEnum`. This avoids typos and makes the code self-documenting.

### 4\. Asynchronous Programming & State Management

Proper async patterns are essential for a responsive bot.

- **Use `tasks.loop` for Recurring Actions**: For actions that need to run on a schedule (e.g., flushing a cache, pruning roles), use the `tasks.loop` decorator.
  - **Example**: `cogs/activity.py` caches user activity in a `set` and flushes it to the database every 60 seconds in a background task. This is far more efficient than a database write on every message.
- **Graceful Shutdown**: Always implement the `cog_unload` method in your cog to cancel any running background tasks. This prevents errors during hot-reloading.
- **Use `asyncio.create_task` for "Fire-and-Forget" Operations**: If an action doesn't need to block the main flow (like updating stats after a game), run it as a new task.

### 5\. Robust Error Handling

- **Define Custom Exceptions**: For backend logic in `modules/`, create specific exceptions (e.g., `InsufficientFundsError`).
- **Catch Specific Exceptions**: In the `cogs/`, catch these specific, custom exceptions and provide clear, user-friendly error messages. Avoid catching generic `Exception`.
- **Use Pre-Action Checks**: Before performing an action that might fail due to permissions (e.g., moderation), validate the state first. The `_pre_action_checks` function in `cogs/moderate.py` is a prime example of this pattern.

### 6\. Configuration Management

There are two types of configuration:

1.  **Static Config (`modules/config.py`)**: Loaded from environment variables (`.env`). This is for bot-wide settings needed on startup (e.g., `TOKEN`).
2.  **Dynamic Config (`modules/ConfigDB.py`)**: Stored in the database. This is for guild-specific settings that admins can change with commands (e.g., `mod_log_channel_id`).

---

## Merging Changes

- Always test your changes in a development environment before creating a pull request.
- Use **squash and merge** for pull requests to maintain a clean commit history.
