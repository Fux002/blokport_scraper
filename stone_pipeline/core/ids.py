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


def mint_surrogate(src_site: str, src_url: str | None, raw_name: str | None, ordinal: int) -> str:
    """Deterministic surrogate when the natural key is missing (section 2, Stage 2).

    Keyed on sha1(src_url or raw_name + ordinal) so a blank key never flows into
    handles, image keys, or dedup, and the same blank row mints the same key on
    every run."""
    basis = src_url or raw_name or ""
    return "mint_" + sha1_hex(src_site, basis, str(ordinal))[:16]


def stable_seed(surrogate_key: str, field_name: str) -> int:
    """Seed for any synthetic fill (section 10.2, section 11.1)."""
    return int(sha256_hex(surrogate_key, field_name)[:12], 16)


def seeded_uniform(surrogate_key: str, field_name: str, low: float, high: float) -> float:
    rng = random.Random(stable_seed(surrogate_key, field_name))
    return low + (high - low) * rng.random()


def row_fingerprint(parts: Iterable[str]) -> str:
    """Hash of the ordered inputs that determine the output (section 11.2)."""
    return sha256_hex(*[str(p) for p in parts])[:24]
