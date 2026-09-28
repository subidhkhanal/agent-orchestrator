from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Callable, Mapping

import pytest
from dotenv import load_dotenv

# Pick up LIBPQ_DIR / TEST_DATABASE_URL from a local .env (never overrides real env vars).
load_dotenv()
# Windows dev machines may block psycopg's binary wheel; make the system libpq findable.
if os.name == "nt" and (libpq_dir := os.environ.get("LIBPQ_DIR")):
    os.environ["PATH"] = libpq_dir + os.pathsep + os.environ["PATH"]

from orchestrator.clock import FakeClock  # noqa: E402


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def pytest_asyncio_loop_factories(
    config: pytest.Config, item: pytest.Item
) -> Mapping[str, Callable[[], asyncio.AbstractEventLoop]] | None:
    # psycopg's async mode cannot use Windows' default proactor loop.
    if sys.platform == "win32":
        return {"selector": asyncio.SelectorEventLoop}
    return {"default": asyncio.new_event_loop}
