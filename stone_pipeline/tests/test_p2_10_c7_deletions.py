"""P2-10 C7, the deletions (P2-10-SCRAPER-PLAN.md section 6): each deleted path stays gone. The learned guesses are
config.db proposals (C2) and the old alias_writeback.csv is the learned-alias migration's input alone, read from S3 by
aliases.migrate_learned (SC #706), so the write-back module, its task-disk snapshot and its boot restore have no job
left. The rule a guess is judged by (is_learned) is the matcher's, and the run's proposal list is the matcher stage's.
No em dashes."""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

from stone_pipeline.ledger import snapshot

PACKAGE = Path(__file__).resolve().parents[1]
REPO = PACKAGE.parent
GONE_MODULE = "stone_pipeline.state.writeback"
GONE_SNAPSHOT = ("save_state", "restore_state", "_writeback_path")


def _sources():
    """Every non-test module of the package and of deploy/, with its parsed tree."""
    for path in sorted([*PACKAGE.rglob("*.py"), *(REPO / "deploy").rglob("*.py")]):
        if "tests" not in path.relative_to(REPO).parts:
            yield path.relative_to(REPO), ast.parse(path.read_text(encoding="utf-8"))


def test_the_write_back_module_is_gone():
    assert importlib.util.find_spec(GONE_MODULE) is None


def test_no_module_imports_the_write_back():
    found = []
    for rel, tree in _sources():
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith(GONE_MODULE):
                found.append(f"{rel}:{node.lineno}")
            elif isinstance(node, ast.ImportFrom) and node.module == "stone_pipeline.state" and \
                    any(a.name == "writeback" for a in node.names):
                found.append(f"{rel}:{node.lineno}")
            elif isinstance(node, ast.Import) and any(a.name.startswith(GONE_MODULE) for a in node.names):
                found.append(f"{rel}:{node.lineno}")
    assert found == []


@pytest.mark.parametrize("name", GONE_SNAPSHOT)
def test_the_task_disk_write_back_snapshot_is_gone(name):
    assert not hasattr(snapshot, name)


def test_nothing_calls_the_write_back_snapshot():
    calls = [f"{rel}:{node.lineno} {node.attr}" for rel, tree in _sources() for node in ast.walk(tree)
             if isinstance(node, ast.Attribute) and node.attr in GONE_SNAPSHOT]
    assert calls == []


def test_the_guess_rule_and_the_proposal_list_have_their_homes():
    from stone_pipeline.aliases import learned
    from stone_pipeline.stages import match_variation
    assert learned.is_learned("clean_variety_fuzzy") and not learned.is_learned("exact")
    assert match_variation.WriteBack.__module__ == match_variation.__name__


def test_the_review_decisions_doc_names_no_scoped_alias_table():
    # the vendor alias is a vendor `is` statement (config.db `statement`); the scoped_alias table is a legacy one the
    # store migrates away (config/store.py _LEGACY_DECISION_TABLES)
    assert "scoped_alias" not in (REPO / "REVIEW_DECISIONS.md").read_text(encoding="utf-8")
