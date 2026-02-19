from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import ClassVar

import aiosqlite


class Database:
    DB_FILENAME: ClassVar[str] = "database.db"

    def __init__(self) -> None:
        pass

    async def _configure_connection(self, conn: aiosqlite.Connection) -> None:
        """Apply essential PRAGMA settings for integrity and performance."""
        await conn.execute("PRAGMA foreign_keys = ON;")
        await conn.execute("PRAGMA busy_timeout = 5000;")  # Wait 5s if locked

    @asynccontextmanager
    async def get_cursor(self) -> AsyncGenerator[aiosqlite.Cursor]:
        async with aiosqlite.connect(self.DB_FILENAME) as conn:
            await self._configure_connection(conn)
            async with conn.cursor() as cursor:
                yield cursor

    @asynccontextmanager
    async def get_conn(self) -> AsyncGenerator[aiosqlite.Connection]:
        async with aiosqlite.connect(self.DB_FILENAME) as conn:
            await self._configure_connection(conn)
            yield conn
