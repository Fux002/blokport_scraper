"""Environment-variable resolution with a brand-neutral prefix.

The pipeline's env vars use the neutral `SCRAPER_` prefix (the tool is brand- and product-agnostic, so its
config is not one brand's property). The legacy `BLOKPORT_` prefix is still read as a fallback, so a running
deployment -- or a task def that has not been re-applied yet -- keeps working unchanged during the migration.
Precedence: SCRAPER_<X> wins over BLOKPORT_<X>. One place, so no per-call-site drift.
"""

from __future__ import annotations

import os

PREFIX = "SCRAPER_"
LEGACY_PREFIX = "BLOKPORT_"


def _suffix(name: str) -> str:
    """Accept either a bare suffix ('S3_BUCKET') or a full legacy name ('BLOKPORT_S3_BUCKET')."""
    if name.startswith(LEGACY_PREFIX):
        return name[len(LEGACY_PREFIX):]
    if name.startswith(PREFIX):
        return name[len(PREFIX):]
    return name


def getenv(name: str, default: str | None = None) -> str | None:
    """Value of the env var under the neutral prefix, else the legacy prefix, else `default`. Mirrors
    os.environ.get semantics (returns `default` -- None by default -- when neither is set)."""
    suffix = _suffix(name)
    val = os.environ.get(PREFIX + suffix)
    if val is None:
        val = os.environ.get(LEGACY_PREFIX + suffix)
    return val if val is not None else default


def require(name: str) -> str:
    """Like getenv but raises KeyError when neither prefix is set (for the os.environ[...] call sites)."""
    val = getenv(name)
    if val is None:
        raise KeyError(PREFIX + _suffix(name))
    return val
