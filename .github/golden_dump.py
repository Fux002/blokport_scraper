"""Opt-in row dump for the run_source golden (the Golden rewrite workflow's `dump` input). Loaded with `-p golden_dump`
from this folder, so it works on ANY checkout of test_run_source_golden.py, a base sha that predates it included: it
wraps that module's `_scenario` and, after each scenario has run, writes the rows the golden only digests.

RUN_SOURCE_GOLDEN_DUMP=<dir> names the folder. Per scenario, <dir>/<scenario>/ holds:
- LEDGER_<table>.csv: every ledger table, header first, rows sorted, wall-clock timestamps masked as <ts>;
- alias_writeback.csv: the learned-alias write-back, if the run wrote one;
- <source run>/tree_gaps.csv and <source run>/products_rejects.csv: each source run's review files of those names.
Without the variable the plugin does nothing. No em dashes.
"""
from __future__ import annotations

import csv
import functools
import os
import re
import shutil
import sqlite3
from pathlib import Path

DUMP_ENV = "RUN_SOURCE_GOLDEN_DUMP"
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(\+00:00|Z)?")


def _masked(value) -> str:
    return _TIMESTAMP.sub("<ts>", "" if value is None else str(value))


def _dump_ledger(ledger: Path, into: Path) -> None:
    con = sqlite3.connect(ledger)
    try:
        tables = [t for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        for table in tables:
            cur = con.execute(f'SELECT * FROM "{table}"')
            header = [d[0] for d in cur.description]
            rows = sorted([_masked(v) for v in row] for row in cur)
            with (into / f"LEDGER_{table}.csv").open("w", newline="", encoding="utf-8") as h:
                csv.writer(h).writerows([header, *rows])
    finally:
        con.close()


def _copy_masked(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(_masked(src.read_text(encoding="utf-8-sig")), encoding="utf-8")


def _dump(scenario_dir: Path, root: Path) -> None:
    into = root / scenario_dir.name
    if into.exists():
        shutil.rmtree(into)
    into.mkdir(parents=True)
    _dump_ledger(scenario_dir / "scenario.ledger", into)
    out = scenario_dir / "out"
    for writeback in sorted(out.rglob("alias_writeback.csv")):
        _copy_masked(writeback, into / writeback.relative_to(out))
    for name in ("tree_gaps.csv", "products_rejects.csv"):
        for path in sorted(out.glob(f"*/review/{name}")):
            _copy_masked(path, into / path.parent.parent.name / name)


def pytest_collection_modifyitems(items):
    root = os.environ.get(DUMP_ENV)
    if not root:
        return
    for item in items:
        module = getattr(item, "module", None)
        scenario = getattr(module, "_scenario", None)
        if scenario is None or getattr(scenario, "_golden_dump", False):
            continue

        @functools.wraps(scenario)
        def dumping(base, *args, _scenario=scenario, **kwargs):
            digests = _scenario(base, *args, **kwargs)
            _dump(Path(base), Path(root))
            return digests

        dumping._golden_dump = True
        module._scenario = dumping
