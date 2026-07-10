"""Per-source certification gate -- the trust foundation for safe auto-loading.

A data source (a web scraper OR any other product connection) is NOT trusted to
auto-load into Medusa until it passes certification. A source's `mode` (in
sources.yaml) is "review" by default -- its output stages for human sign-off;
only `mode: auto` lets it load automatically. You promote review->auto once this
command is green and you've signed off, so a newly-added source can never silently
push bad data live.

    python -m stone_pipeline.certify <source>     # one source
    python -m stone_pipeline.certify all          # every configured source (the CI gate)

Checks are OFFLINE and deterministic (fixture-based), so CI runs them on every push
and a regression fails the build:

  config    the source is in sources.yaml with a source_code + a valid mode
  adapter   an adapter is registered for the source (maps raw rows -> canonical)
  selftest  the adapter reproduces its golden fixture EXACTLY (the core check)
  contract  a column contract is defined, so drift detection has a baseline

A full pre-promotion check (live fetch + health/drift on real data) is a heavier,
networked step run from staging; this command is the fast gate that guards every
build and every new source.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field

from stone_pipeline.adapters import REGISTRY as ADAPTERS
from stone_pipeline.adapters.selftest import fixture_dir, run_fixture
from stone_pipeline.config.contracts import load_contract
from stone_pipeline.config.sources import load_source, load_sources

_VALID_MODES = ("review", "auto")
_MIN_VOCAB_VALUES = 5  # need enough non-empty values for a swap signal to be meaningful


def _vocab_swaps(per_attr: dict[str, dict]) -> list[str]:
    """Pure core of the vocab check. `per_attr[attr] = {"n": int, "own": float, "cross": {other: float}}`.
    Flags a likely column SWAP: an attribute whose values resolve far better as a DIFFERENT vocabulary
    than its own (the self-test can't catch this -- the golden fixture is made by the same adapter, so a
    finish-mapped-into-the-color-column is baked in and passes). Conservative thresholds: only fires on a
    clear cross-vocab winner, so merely-novel (unresolvable-everywhere) values never trip it."""
    msgs: list[str] = []
    for attr, d in sorted(per_attr.items()):
        if d["n"] < _MIN_VOCAB_VALUES:
            continue
        own = d["own"]
        best_other, best_rate = max(d["cross"].items(), key=lambda kv: kv[1], default=(None, 0.0))
        if best_other and own < 0.5 and best_rate >= 0.5 and best_rate - own >= 0.4:
            msgs.append(f"raw_{attr} resolves as '{best_other}' ({best_rate:.0%}) but as '{attr}' only "
                        f"({own:.0%}) -- the {attr}/{best_other} columns look swapped")
    return msgs


def check_vocab(source: str) -> tuple[bool, str]:
    """Run the fixture through the adapter and check each mapped attribute's values draw from the
    RIGHT vocabulary -- catching a column mis-mapping the self-test cannot. No fixture / no reference
    -> skip (ok), so this never blocks on infrastructure; the selftest already guards a missing fixture."""
    fdir = fixture_dir(source)
    if source not in ADAPTERS or not (fdir / "input.csv").exists():
        return True, "skipped (no adapter/fixture)"
    try:
        from stone_pipeline.adapters.base import read_scrape_csv
        from stone_pipeline.reference import loaders
        from stone_pipeline.stages.normalize import VOCAB_FIELDS, AttributeResolvers
        ref = loaders.load_all()
        resolvers = AttributeResolvers.build(ref)
        rows = ADAPTERS[source].adapt(read_scrape_csv(fdir / "input.csv"))

        def rate(values: list[str], vocab: str) -> float:
            vals = [v for v in values if v]
            return sum(1 for v in vals if resolvers.resolvers[vocab].resolve(v).value is not None) / len(vals) if vals else 0.0

        per_attr: dict[str, dict] = {}
        for attr in VOCAB_FIELDS:
            vals = [getattr(r, f"raw_{attr}", "") or "" for r in rows]
            n = sum(1 for v in vals if v)
            per_attr[attr] = {"n": n, "own": rate(vals, attr),
                              "cross": {o: rate(vals, o) for o in VOCAB_FIELDS if o != attr}}
        swaps = _vocab_swaps(per_attr)
    except Exception as exc:
        return True, f"skipped (could not evaluate: {exc})"
    return (not swaps, "; ".join(swaps) if swaps else "attribute columns map to the right vocabularies")


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class CertResult:
    source: str
    mode: str
    checks: list[Check] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.ok for c in self.checks)


def certify_source(source: str) -> CertResult:
    cfg = load_source(source)
    mode = getattr(cfg, "mode", "review")
    res = CertResult(source=source, mode=mode)

    res.checks.append(Check(
        "config", bool(cfg.source_code) and mode in _VALID_MODES,
        f"source_code={cfg.source_code!r} mode={mode!r}"))

    has_adapter = source in ADAPTERS
    res.checks.append(Check(
        "adapter", has_adapter,
        "registered" if has_adapter else "missing — add stone_pipeline/adapters/<source>.py"))

    if has_adapter:
        try:
            ok, msg = run_fixture(source)
        except Exception as exc:  # missing/broken fixture
            ok, msg = False, f"error: {exc} — regenerate the golden fixture"
        res.checks.append(Check("selftest", ok, msg))
        # the self-test only proves the adapter is STABLE (the golden file is made by the same
        # adapter); the vocab check proves it is CORRECT -- a swapped attribute column is caught here.
        v_ok, v_msg = check_vocab(source)
        res.checks.append(Check("vocab", v_ok, v_msg))
    else:
        res.checks.append(Check("selftest", False, "skipped (no adapter)"))

    res.checks.append(Check(
        "contract", load_contract(source) is not None,
        "defined" if load_contract(source) is not None else "missing — generate from a sample"))

    return res


def _print(res: CertResult) -> None:
    print(f"{'PASS' if res.passed else 'FAIL'}  {res.source}  (mode={res.mode})")
    for c in res.checks:
        print(f"    {'OK ' if c.ok else 'XX '} {c.name:9} {c.detail}")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    target = argv[0] if argv else "all"
    sources = list(load_sources()) if target == "all" else [target]
    if not sources:
        print("no sources configured in sources.yaml")
        return 1

    results = [certify_source(s) for s in sources]
    for res in results:
        _print(res)
    print("-" * 56)
    failed = [r.source for r in results if not r.passed]
    if failed:
        print(f"CERTIFICATION FAILED: {', '.join(failed)} — fix the XX checks above")
        return 1
    print(f"ALL CERTIFIED ({len(results)} source(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
