"""Format resolution: block, slab, or tile (section 7 Stage 6 category, moved
early because the branch selects the variants file in Stage 4).

The format should come from an explicit tag in the scraped data, set at scraper
level (the scrape is rarely clear about it otherwise). The adapter declares which
column carries that tag. This resolver is a trust-ordered ladder over the
available signals, with provenance on the result, and never blindly assumes a
default: when nothing is clear it flags format_unresolved so the scraper can be
fixed to emit the tag.

Ladder (highest trust first):
  1. override            manual_overrides format_value
  2. explicit tag        the adapter-mapped format field (the scraper's tag)
  3. name word           a Block/Slab/Tile word in the product name
  4. structural          a clean inference from slab-count / area / thickness
  5. unresolved          no signal -> flag, fall back to the slab branch so the
                         pipeline continues, but the call is visible in review

Each format routes to its own backend category via category_pcat_for_branch once
that category has a Medusa id (Tiles are now a first-class active category, no
longer folded into Slabs).
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from stone_pipeline.config.settings import CATEGORIES, Confidence, category
from stone_pipeline.core import logfmt
from stone_pipeline.core.schema import CanonicalRow, FlagCode, ReviewFlag
from stone_pipeline.reference.loaders import ReferenceData

log = logfmt.get_logger("format")

# a name-word regex derived from the registry (format detection in a product name)
_NAME_WORD = re.compile(r"\b(" + "|".join(c.name for c in CATEGORIES) + r")s?\b",
                        flags=re.IGNORECASE)
_DEFAULT_BRANCH = "slab"  # unresolved format falls back to slab (flagged separately)


def branch_of(row) -> str:
    """The format branch (a registry category name). Defaults to slab when
    unresolved (the unresolved case is flagged separately in resolve_format)."""
    value = (getattr(row, "format_value", None) or "").strip().casefold()
    return value if category(value) else _DEFAULT_BRANCH


def category_pcat_for_branch(branch: str, ref) -> str | None:
    """The backend category pcat for a branch: the attributes.csv mapping (by the
    category's label), falling back to the registry pcat (lets a config-supplied id
    activate a category not yet in attributes.csv, e.g. tiles)."""
    c = category(branch)
    if not c:
        return None
    return ref.attributes.category_pcat.get(c.label) or (c.pcat_id or None)


def _conf(c: Confidence) -> str:
    return Confidence(c).name


def _set(row: CanonicalRow, value: str, method: str, confidence: Confidence) -> None:
    # Canonicalise to the registry's SINGULAR name. A scraper may tag its format in the plural
    # ('Tiles'/'Blocks'); category() accepts that, but storing the raw plural broke downstream: the
    # dimension bucket compares format_value == 'tile' (so 'Tiles' fell to the slab dims/weight/freight)
    # and is_block compares == 'block' (so a 'Blocks' tag shipped a block as a slab). Resolve once here.
    c = category(value)
    canonical = c.name if c else value            # canonical singular; a truly unresolved value passes through
    row.format_value = canonical.title()          # Block / Slab / Tile -- never a plural
    row.is_block = canonical.casefold() == "block"
    row.format_method = method
    row.format_confidence = _conf(confidence)


def _structural_guess(row: CanonicalRow) -> str | None:
    """A clean, isolated structural inference. Slab bundles carry a slab count, a
    total area, or a thickness; those are unambiguous slab indicators. Block and
    tile detection from raw fields is not reliable without an explicit tag, so
    this only confirms slab and otherwise declines (returns None)."""
    has_slab_count = bool((row.raw_slab_count or "").strip())
    has_total_area = bool((row.raw_total_m2 or "").strip())
    has_thickness = bool((row.raw_thickness or "").strip())
    if has_slab_count or has_total_area or has_thickness:
        return "slab"
    return None


def resolve_format(row: CanonicalRow, ref: ReferenceData) -> None:
    overrides = ref.overrides

    # 1. override
    if overrides is not None:
        forced = overrides.get(row.src_site, row.surrogate_key or "", "format_value")
        if forced and category(forced):  # runtime registry, consistent with branch_of
            _set(row, forced, "override", Confidence.high)
            return

    # 2. explicit tag mapped by the adapter (the scraper's format field)
    tag = (row.raw_format or "").strip().casefold()
    if category(tag):
        _set(row, tag, "explicit_tag", Confidence.high)
        return

    # 3. format word in the product name
    name_hit = _NAME_WORD.search(row.raw_name or "")
    if name_hit:
        _set(row, name_hit.group(1), "name_word", Confidence.high)
        return

    # 4. structural inference (slab indicators)
    guess = _structural_guess(row)
    if guess:
        _set(row, guess, "structural", Confidence.medium)
        row.add_flag(ReviewFlag(field="format", code=FlagCode.format_inferred,
                                raw_value=row.raw_format, best_guess=guess.title(),
                                confidence=Confidence.medium, method="structural", src_url=row.src_url))
        return

    # 5. unresolved: do not assume. Flag it and fall back to the slab branch so
    # the pipeline continues; the flag makes the missing scraper tag visible.
    _set(row, "slab", "unresolved_default", Confidence.none)
    row.add_flag(ReviewFlag(field="format", code=FlagCode.format_unresolved,
                            raw_value=row.raw_format, best_guess="Slab",
                            confidence=Confidence.none, method="no_signal", src_url=row.src_url))


@dataclass
class FormatStats:
    rows: int = 0
    unresolved: int = 0
    by_value: dict | None = None


def run(rows: list[CanonicalRow], ref: ReferenceData) -> FormatStats:
    counts: Counter[str] = Counter()
    unresolved = 0
    for row in rows:
        resolve_format(row, ref)
        counts[row.format_value or "?"] += 1
        if row.format_method == "unresolved_default":
            unresolved += 1
    log.info("format resolved", extra={"extra_fields": {"by_value": dict(counts), "unresolved": unresolved}})
    return FormatStats(rows=len(rows), unresolved=unresolved, by_value=dict(counts))
