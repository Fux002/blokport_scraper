"""Small deterministic text helpers for generation (section 10.3, 10.4)."""

from __future__ import annotations

import re

_NON_SLUG = re.compile(r"[^a-z0-9]+")
_WS = re.compile(r"\s+")


def collapse_ws(text: str) -> str:
    return _WS.sub(" ", (text or "").strip())


def title_case(text: str) -> str:
    """Title-case and collapse whitespace, preserving existing all-caps acronyms
    minimally (good enough for stone names)."""
    return collapse_ws(" ".join(w.capitalize() if not w.isupper() else w for w in (text or "").split()))


def slugify(text: str) -> str:
    return _NON_SLUG.sub("-", (text or "").casefold()).strip("-")
