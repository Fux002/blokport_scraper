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
    """Value of the env var under the neutral prefix, else the legacy prefix, else `default`. An EMPTY
    string counts as unset for the neutral read, so `SCRAPER_X=""` (e.g. an unset-defaulting TF var) does NOT
    shadow a real legacy `BLOKPORT_X` -- it falls through. Returns `default` (None by default) when neither
    prefix carries a non-empty value."""
    suffix = _suffix(name)
    val = os.environ.get(PREFIX + suffix) or os.environ.get(LEGACY_PREFIX + suffix)
    return val if val else default


def require(name: str) -> str:
    """Like getenv but raises KeyError when neither prefix carries a non-empty value (for the os.environ[...]
    call sites, so a set-but-EMPTY var fails loud instead of returning '')."""
    val = getenv(name)
    if not val:
        raise KeyError(PREFIX + _suffix(name))
    return val


def env_bool(name: str, default: bool = False) -> bool:
    """True/False from a boolean env var (1/true/yes/on), read via getenv (so the neutral SCRAPER_ prefix
    wins over the legacy BLOKPORT_ fallback). Returns `default` when neither prefix carries a value. The ONE
    boolean-env parser: every call site delegates here so the truthy vocabulary never drifts."""
    raw = getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")
