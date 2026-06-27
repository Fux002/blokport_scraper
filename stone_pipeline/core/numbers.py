"""Locale-robust numeric parsing, shared so every stage reads a number the SAME way.

Suppliers format numbers inconsistently: US '77,746.504', EU '77.746,504', a bare EU decimal '2,80',
or a plain '2.80'. Parsing these ad-hoc (e.g. just stripping commas) silently mis-scales EU decimals
('2,80' -> 280). This is the one place that disambiguation lives.
"""

from __future__ import annotations

import re

# a numeric token: a run of digits that may contain grouping/decimal separators
_NUM_RE = re.compile(r"-?\d[\d.,]*\d|-?\d")


def parse_number(text: str | float | int | None) -> float | None:
    """Parse the first number out of a possibly-formatted string. Returns float or None.

    Disambiguation:
      * BOTH ',' and '.' present -> the RIGHTMOST is the decimal point, the other is thousands
        grouping ('1,234.56' and '1.234,56' both -> 1234.56).
      * ONLY ',' present -> a single comma followed by 1-2 digits is a decimal ('2,80' -> 2.8);
        otherwise commas are thousands grouping ('1,234' -> 1234, '1,234,567' -> 1234567).
      * ONLY '.' present -> standard decimal point.
    """
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    match = _NUM_RE.search(str(text))
    if not match:
        return None
    s = match.group(0)
    neg = s.startswith("-")
    s = s.lstrip("-")
    has_comma, has_dot = "," in s, "." in s
    if has_comma and has_dot:
        dec = "," if s.rfind(",") > s.rfind(".") else "."
        s = s.replace("." if dec == "," else ",", "").replace(dec, ".")
    elif has_comma:
        frac = s.rsplit(",", 1)[1]
        s = s.replace(",", ".") if (s.count(",") == 1 and 1 <= len(frac) <= 2) else s.replace(",", "")
    elif s.count(".") > 1:
        # more than one dot cannot be a decimal point -> EU thousands grouping ('2.500.000' -> 2500000).
        # A single dot stays a decimal point (the documented default), so '2.80'/'1.234' are unchanged.
        s = s.replace(".", "")
    try:
        value = float(s)
    except ValueError:
        return None
    return -value if neg else value


# quote glyphs that mean inch/foot, normalised to the unit tokens units.csv knows
_UNIT_ALIASES = {'"': "in", "″": "in", "”": "in", "''": "in",
                 "'": "ft", "′": "ft", "’": "ft"}


def normalize_unit(raw: str | None, default: str = "m") -> str:
    """Map a raw unit token (incl. inch/foot quote marks) to a units.csv token. Empty -> default."""
    u = (raw or "").strip()
    if u in _UNIT_ALIASES:
        return _UNIT_ALIASES[u]
    return u.lower().strip("\"'′″’”") or default
