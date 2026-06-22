"""Backend id fingerprint check (section 3.2).

All backend ids belong to one environment; ids change when the backend is
re-seeded. The pipeline computes a fingerprint of the live id set from the
reference data and compares it against the pinned value in settings. A drift
aborts with a clear message, which is the documented cause of the earlier
stale-id import failures. When nothing is pinned yet, the check pins instead of
aborting and returns the computed value so it can be recorded.
"""

from __future__ import annotations

import hashlib

from stone_pipeline.config.settings import CATEGORIES, SETTINGS
from stone_pipeline.reference.loaders import ReferenceData


class FingerprintMismatch(RuntimeError):
    pass


def compute_fingerprint(ref: ReferenceData) -> str:
    """Stable hash over the sorted set of all backend ids in the reference data."""
    ids = set(ref.attributes.all_ids())
    for table in ref.variants.values():   # every category (registry), tiles included
        ids.update(table.all_ids())
    ids.update(ref.ports.all_ids())
    # The static config ids are part of the environment too.
    ids.update(c.pcat_id for c in CATEGORIES if c.pcat_id)
    digest = hashlib.sha256()
    for value in sorted(ids):
        digest.update(value.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()[:16]


def check_fingerprint(ref: ReferenceData, pinned: str | None = None) -> str:
    """Return the live fingerprint. Raise if it drifted from a pinned value.

    pinned defaults to SETTINGS.backend_id_fingerprint; an empty pinned value
    means "not pinned yet" and the check passes (pin-on-first-run)."""
    pinned = SETTINGS.backend_id_fingerprint if pinned is None else pinned
    live = compute_fingerprint(ref)
    if pinned and pinned != live:
        raise FingerprintMismatch(
            f"backend id fingerprint drifted: pinned={pinned} live={live}. "
            "Reference data was re-seeded or points at a different environment. "
            "Refresh the ids and re-pin before running."
        )
    return live
