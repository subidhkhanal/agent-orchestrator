from __future__ import annotations

from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool

# A pool whose connections return rows as dicts.
Pool = AsyncConnectionPool[AsyncConnection[DictRow]]
Row = dict[str, Any]

__all__ = ["Pool", "Row", "open_pool"]


async def open_pool(
    url: str, *, min_size: int = 1, max_size: int = 10, max_idle_s: float = 600.0
) -> Pool:
    """A pool of autocommit connections returning dict rows.

    Autocommit means each statement commits unless we open `conn.transaction()` explicitly,
    which every multi-statement write in this package does. prepare_threshold=None keeps the
    pool usable by the LangGraph checkpointer too.
    """
    pool: Pool = AsyncConnectionPool(
        url,
        min_size=min_size,
        max_size=max_size,
        max_idle=max_idle_s,
        open=False,
        connection_class=AsyncConnection[DictRow],
        kwargs={"autocommit": True, "row_factory": dict_row, "prepare_threshold": None},
    )
    await pool.open(wait=min_size > 0, timeout=15)
    return pool
