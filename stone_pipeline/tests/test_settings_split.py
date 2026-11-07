"""Names split out of config/settings.py live in ONE home and are read only from there (Wave C9): no module imports, reads
or patches a moved name on settings, settings keeps no copy (an alias would go stale: the registry's helpers read module
globals that refresh_category_pcats rebinds, and a test patching settings' copy would test nothing), and the new module
depends on settings one way, so importing settings never builds it."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

from stone_pipeline.config import settings

REPO = Path(__file__).resolve().parents[2]
SETTINGS_MODULE = "stone_pipeline.config.settings"
SKIP_DIRS = {".venv", ".git", ".claude", "__pycache__", "node_modules"}
MOVED = {
    "stone_pipeline.config.categories": (
        "Category", "CATEGORIES", "_BY_NAME", "_BY_LABEL_CF", "category", "category_for_key", "default_form_name",
        "bulk_form_name", "active_categories", "refresh_category_pcats", "_ENV_PCATS", "_pcat", "_env_category_pcats",
        "_build_categories"),
}
ALL_MOVED = {name for names in MOVED.values() for name in names}


def _python_files():
    for path in sorted(REPO.rglob("*.py")):
        if not SKIP_DIRS.intersection(path.relative_to(REPO).parts):
            yield path


def _settings_aliases(tree: ast.AST) -> set[str]:
    """The local names a module binds to the settings module (`from stone_pipeline.config import settings [as x]`,
    `import stone_pipeline.config.settings [as x]`)."""
    aliases = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "stone_pipeline.config":
            aliases |= {a.asname or a.name for a in node.names if a.name == "settings"}
        elif isinstance(node, ast.Import):
            aliases |= {a.asname or a.name for a in node.names if a.name == SETTINGS_MODULE}
    return aliases


def _old_home_reads(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    aliases = _settings_aliases(tree)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == SETTINGS_MODULE:
            found += [f"{node.lineno} import {a.name}" for a in node.names if a.name in ALL_MOVED]
        elif isinstance(node, ast.Attribute) and node.attr in ALL_MOVED and ast.unparse(node.value) in aliases | {
                SETTINGS_MODULE}:
            found.append(f"{node.lineno} {ast.unparse(node)}")
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and node.func.attr in ("setattr", "delattr") and len(node.args) > 1
              and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str)):
            target, name = ast.unparse(node.args[0]), node.args[1].value
            if target in aliases and name in ALL_MOVED or name.rpartition(".")[::2] in {
                    (SETTINGS_MODULE, moved) for moved in ALL_MOVED}:
                found.append(f"{node.lineno} patches {target} {name}")
    return found


def test_no_module_reads_a_moved_name_from_settings():
    reads = {str(path.relative_to(REPO)): hits for path in _python_files() if (hits := _old_home_reads(path))}
    assert reads == {}


@pytest.mark.parametrize("name", sorted(ALL_MOVED))
def test_settings_keeps_no_copy(name):
    assert not hasattr(settings, name)


def _fresh_python(code: str, **env_overrides: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AWS_", "FAL_", "SCRAPER_", "BLOKPORT_"))}
    env.update({"PYTHONPATH": str(REPO), "SCRAPER_ENV": "development", **env_overrides})
    return subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env, capture_output=True, text=True, timeout=120)


@pytest.mark.parametrize("module", sorted(MOVED))
def test_importing_settings_never_loads_a_split_module(module):
    done = _fresh_python(f"import sys, stone_pipeline.config.settings; print({module!r} in sys.modules)")
    assert (done.returncode, done.stdout.strip()) == (0, "False"), done.stderr[-2000:]


def test_the_registry_is_built_from_the_pack_active_at_its_first_import():
    done = _fresh_python("from stone_pipeline.config.categories import CATEGORIES\n"
                         "print(','.join(c.name for c in CATEGORIES))", SCRAPER_DOMAIN_PACK="wood")
    assert (done.returncode, done.stdout.strip()) == (0, "plank,beam,panel"), done.stderr[-2000:]
