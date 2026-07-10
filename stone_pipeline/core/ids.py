"""Determinism and idempotency helpers (section 11).

No uuid, no unseeded randomness anywhere in keys or values. Surrogate keys and
synthetic fills are stable functions of stable inputs, so a re-run on the same
input produces byte-identical output.
"""

from __future__ import annotations

import hashlib
import random
from typing import Iterable


def sha1_hex(*parts: str) -> str:
    digest = hashlib.sha1()
    for part in parts:
        digest.update(str(part).encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


def sha256_hex(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(str(part).encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


def mint_surrogate(src_site: str, src_url: str | None, raw_name: str | None, ordinal: int,
                   raw_format: str | None = None) -> str:
    """Deterministic surrogate when the natural key is missing (section 2, Stage 2).

    Keyed on a STABLE basis -- the product url, else its raw name -- so the same product mints the
    same surrogate (and thus the same SKU) on every re-scrape, even if the supplier reorders its
    listing. This is what keeps the inventory lane's existing-product link intact for blank-key
    rows. The scrape position (ordinal) is NOT reproducible across runs, so it is used ONLY as a
    last resort when there is no url and no name at all (keys_dedupe still asserts uniqueness, so a
    genuine collision surfaces loudly rather than silently drifting).

    The format/branch is part of the basis: a supplier that lists the SAME variety as both a slab
    and a block under the same url/name (blank natural key) must mint DISTINCT surrogates -- they are
    two different products and must not collapse to one SKU and get silently de-duped away."""
    basis = (src_url or "").strip() or (raw_name or "").strip()
    fmt = (raw_format or "").strip().casefold()
    if basis:
        return "mint_" + sha1_hex(src_site, basis, fmt)[:16]
    return "mint_" + sha1_hex(src_site, str(ordinal), fmt)[:16]


def stable_seed(surrogate_key: str, field_name: str) -> int:
    """Seed for any synthetic fill (section 10.2, section 11.1)."""
    return int(sha256_hex(surrogate_key, field_name)[:12], 16)


def seeded_uniform(surrogate_key: str, field_name: str, low: float, high: float) -> float:
    rng = random.Random(stable_seed(surrogate_key, field_name))
    return low + (high - low) * rng.random()


def row_fingerprint(parts: Iterable[str]) -> str:
    """Hash of the ordered inputs that determine the output (section 11.2)."""
    return sha256_hex(*[str(p) for p in parts])[:24]
