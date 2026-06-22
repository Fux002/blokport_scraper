"""Per-source certification gate — the trust foundation for safe auto-loading.

A data source (a web scraper OR any other product connection) is NOT trusted to
auto-load into Medusa until it passes certification. A source's `mode` (in
sources.yaml) is "review" by default — its output stages for human sign-off;
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
from stone_pipeline.adapters.selftest import run_fixture
from stone_pipeline.config.contracts import load_contract
from stone_pipeline.config.sources import load_source, load_sources

_VALID_MODES = ("review", "auto")


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
