"""Stage 2: key integrity and dedup (section 7 Stage 2).

1. Surrogate key: natural key when present and unique, else a deterministic mint.
2. Exact dedup on surrogate_key, keep first by scrape order, record the drop count.
3. Near-duplicate flag: same normalized name + branch + colour. Never auto-merge.
4. Assert surrogate uniqueness after; a residual collision is a hard run error,
   not a row reject, because it means key minting is wrong.
"""

from __future__ import annotations

from dataclasses import dataclass

from stone_pipeline.config.settings import Confidence
from stone_pipeline.core import ids, logfmt
from stone_pipeline.core.schema import CanonicalRow, FlagCode, ReviewFlag
from stone_pipeline.matching import projections as proj

log = logfmt.get_logger("keys_dedupe")


class KeyCollisionError(RuntimeError):
    pass


@dataclass
class DedupResult:
    rows: list[CanonicalRow]
    minted: int
    dropped_exact: int
    near_duplicates: int


def assign_surrogates(rows: list[CanonicalRow]) -> int:
    """Set surrogate_key on every row; mint when the natural key is blank.
    Returns the count of minted keys."""
    minted = 0
    for ordinal, row in enumerate(rows):
        natural = (row.src_natural_key or "").strip()
        if natural:
            row.surrogate_key = natural
        else:
            row.surrogate_key = ids.mint_surrogate(
                row.src_site, row.src_url, row.raw_name, ordinal
            )
            minted += 1
            row.add_flag(
                ReviewFlag(
                    field="surrogate_key",
                    code=FlagCode.surrogate_minted,
                    raw_value=natural,
                    best_guess=row.surrogate_key,
                    confidence=Confidence.high,
                    method="mint",
                    src_url=row.src_url,
                )
            )
    return minted


def run(rows: list[CanonicalRow]) -> DedupResult:
    minted = assign_surrogates(rows)

    # exact dedup on surrogate_key, keep first by scrape order. Dedup in the CASE-FOLDED space
    # because the product SKU is `{source}-{surrogate}`.upper() (emit/medusa_client) -- two natural
    # keys differing only in case ('ab12' vs 'AB12') collapse to one SKU on upload, so they must
    # collapse here too or one silently overwrites the other in Medusa, evading this guard.
    seen: set[str] = set()
    kept: list[CanonicalRow] = []
    dropped_exact = 0
    for row in rows:
        sk = (row.surrogate_key or "").upper()
        if sk in seen:
            dropped_exact += 1
            continue
        seen.add(sk)
        kept.append(row)

    # near-duplicate flag: same normalized name + branch + colour
    near_index: dict[tuple, str] = {}
    near_duplicates = 0
    for row in kept:
        key = (proj.norm(row.raw_name or ""), bool(row.is_block), proj.norm(row.raw_color or ""))
        if key in near_index and key[0]:
            near_duplicates += 1
            row.add_flag(
                ReviewFlag(
                    field="raw_name",
                    code=FlagCode.near_duplicate,
                    raw_value=row.raw_name,
                    best_guess=near_index[key],
                    confidence=Confidence.low,
                    method="name_block_color",
                    src_url=row.src_url,
                )
            )
        else:
            near_index[key] = row.surrogate_key

    # assert uniqueness (a residual collision means minting is wrong)
    final_keys = [r.surrogate_key for r in kept]
    if len(final_keys) != len(set(final_keys)):
        raise KeyCollisionError("surrogate key collision after dedup; key minting is wrong")

    log.info(
        "keys/dedup done",
        extra={
            "extra_fields": {
                "in": len(rows),
                "out": len(kept),
                "minted": minted,
                "dropped_exact": dropped_exact,
                "near_duplicates": near_duplicates,
            }
        },
    )
    return DedupResult(rows=kept, minted=minted, dropped_exact=dropped_exact, near_duplicates=near_duplicates)
