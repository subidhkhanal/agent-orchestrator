"""Postgres persistence. Importing this package makes libpq loadable on Windows dev machines.

Every module in this package imports the package first (Python runs a package's __init__
before any of its submodules), so the PATH fix below always precedes `import psycopg`.
"""

from __future__ import annotations

import os

# psycopg's binary wheel can be blocked by Windows Application Control; its pure-Python
# implementation then needs libpq.dll on PATH.
_libpq = os.environ.get("LIBPQ_DIR")
if os.name == "nt" and _libpq and _libpq not in os.environ.get("PATH", ""):
    os.environ["PATH"] = _libpq + os.pathsep + os.environ.get("PATH", "")
