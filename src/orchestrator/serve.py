"""API server entry point: `python -m orchestrator.serve [--port 8000]`.

Equivalent to `uvicorn orchestrator.api.app:app`, but also works on Windows, where uvicorn's
default event loop (proactor) is not supported by psycopg's async driver.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import uvicorn


async def main(host: str, port: int) -> None:
    config = uvicorn.Config(
        "orchestrator.api.app:app", host=host, port=port, loop="none", proxy_headers=True
    )
    await uvicorn.Server(config).serve()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    args = parser.parse_args()
    if sys.platform == "win32":
        asyncio.run(main(args.host, args.port), loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(main(args.host, args.port))
