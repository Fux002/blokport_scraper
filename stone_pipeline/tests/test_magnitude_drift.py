"""The canonical magnitude-drift gate: catches a unit/normalization rescale of the physical fields
(weight/dims) post-derive, before emit. Source-agnostic (checks only the canonical magnitude fields),
bucketed by product FORMAT so a product-mix shift can never false-fail, and self-tuning per source."""

from __future__ import annotations

from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.stages import magnitude_drift as md


def _rows(weights, source="zucchi", fmt="Slab"):
    rows = []
    for i, w in enumerate(weights):
        r = CanonicalRow(src_site=source, surrogate_key=f"{fmt}{i}")
        r.weight, r.length, r.width, r.height = w, 2.0, 0.02, 3.0
        r.format_value = fmt
        rows.append(r)
    return rows


def test_medians_bucket_by_format_and_cover_only_magnitude_fields():
    m = md.medians(_rows([0.3, 0.4, 0.5] * 4))            # 12 slabs
    assert set(m) == {"slab"}
    assert set(m["slab"]) <= set(md.MAGNITUDE_FIELDS)      # never a price/age/id
    assert m["slab"]["weight"] == 0.4


def test_a_too_small_format_bucket_is_skipped():
    assert md.medians(_rows([0.3] * 3)) == {}             # 3 rows < _MIN_SAMPLE -> too noisy, skipped


def test_first_run_ok_and_seeds_per_format_baseline(tmp_path):
    p = tmp_path / "mag.json"
    status, _ = md.check(_rows([0.3, 0.5, 0.4] * 4), "zucchi", path=p)
    assert status == md.OK
    assert md.load_baseline("zucchi", p).medians["slab"]["weight"] > 0


def test_healthy_variation_stays_ok(tmp_path):
    p = tmp_path / "mag.json"
    md.check(_rows([0.3, 0.5, 0.4] * 4), "zucchi", path=p)            # seed
    status, _ = md.check(_rows([0.32, 0.48, 0.41] * 4), "zucchi", path=p)
    assert status == md.OK


def test_unit_rescale_fails(tmp_path):
    p = tmp_path / "mag.json"
    md.check(_rows([0.4] * 10), "zucchi", path=p)                    # per-piece tonnes ~0.4
    status, report = md.check(_rows([400.0] * 10), "zucchi", path=p)  # kg (1000x) -> unit swap
    assert status == md.FAILED
    assert any(d["field"] == "weight" and d["format"] == "slab" for d in report["drift"])


def test_product_mix_shift_does_not_false_fail(tmp_path):
    # THE robustness property: a source (marenostone) that mixes slabs + blocks must not false-fail when
    # the mix shifts. Per-format medians are mix-invariant -- a slab weighs the same whether or not blocks
    # are also present; the block bucket is new so it is seeded, not compared.
    p = tmp_path / "mag.json"
    md.check(_rows([0.3] * 10, fmt="Slab"), "marenostone", path=p)   # seed slab ~0.3
    mixed = _rows([0.3] * 10, fmt="Slab") + _rows([20.0] * 10, fmt="Block")
    status, _ = md.check(mixed, "marenostone", path=p)
    assert status == md.OK
    assert md.load_baseline("marenostone", p).medians["block"]["weight"] == 20.0  # block now learned


def test_moderate_drift_is_degraded_not_failed(tmp_path):
    p = tmp_path / "mag.json"
    md.check(_rows([0.4] * 10), "zucchi", path=p)
    status, _ = md.check(_rows([2.0] * 10), "zucchi", path=p)         # 5x -> WARN band
    assert status == md.DEGRADED


def test_a_drifted_run_does_not_poison_the_baseline(tmp_path):
    p = tmp_path / "mag.json"
    md.check(_rows([0.4] * 10), "zucchi", path=p)                    # OK -> baseline 0.4
    md.check(_rows([2.0] * 10), "zucchi", path=p)                    # DEGRADED -> not saved
    assert md.load_baseline("zucchi", p).medians["slab"]["weight"] == 0.4


def test_a_transient_small_bucket_does_not_wipe_the_baseline(tmp_path):
    # regression: a format briefly dropping below _MIN_SAMPLE must NOT erase its last-good median (which
    # would silently DEFEAT the gate next run). The OK save MERGES, carrying the absent format forward.
    p = tmp_path / "mag.json"
    md.check(_rows([0.3] * 10), "zucchi", path=p)                    # seed slab @ 0.3
    md.check(_rows([0.3] * 3), "zucchi", path=p)                     # slab transiently 3 rows (< 8) -> skipped
    assert md.load_baseline("zucchi", p).medians["slab"]["weight"] == 0.3   # carried forward, not wiped
    status, _ = md.check(_rows([3.0] * 10), "zucchi", path=p)        # 10x -> still caught
    assert status == md.FAILED
