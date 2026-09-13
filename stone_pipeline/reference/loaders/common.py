"""Shared by every loader: the ONE name normalization, the logger, and the facade hook.

`env()` returns the loaders PACKAGE: the facade is the one place an operator harness or a test replaces
SETTINGS, IS_PRODUCTION or a cached helper (`loaders.SETTINGS = ...`, `loaders.load_synonyms`), so every
loader that reads environment-dependent state resolves it through the facade at call time. Lazy, because the
facade imports these modules first."""

from __future__ import annotations

from stone_pipeline.core import logfmt
from stone_pipeline.core.text import match_key

log = logfmt.get_logger("reference")


def env():
    from stone_pipeline.reference import loaders
    return loaders


def norm(value: str) -> str:
    """The shared normalization for every name lookup -> delegates to core.text.match_key: ascii-fold
    accents, casefold, AND fold every separator run (space/underscore/hyphen/dash/slash) to one space.
    Folding separators too is what lets a hyphenated vocab value ('Semi-Precious Stone') match an
    underscore/space slug, so a punctuation difference never splits the same name. Accent folding
    keeps backbone lookups consistent with the variation index, so 'Porrino' == 'Porriño'."""
    return match_key(value)
