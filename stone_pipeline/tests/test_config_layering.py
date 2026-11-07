"""C11 pins: the config package is a base layer plus a control sub-package. This is a PURE MOVE; the pins
are structural only (no behaviour is asserted here -- admission's verdict and run.auto_sources_blocked are
unchanged from before the move and stay covered by their own tests).

1. No base config module imports the control layer (no base -> control edge), counting absolute, relative,
   and `from stone_pipeline.config import control` forms.
2. stone_pipeline.config.server is a THIN deployed entry: a bare import exposes no server implementation
   names and loads no control module, and running it as __main__ delegates to control.server.main. The task
   def and CI smoke run it as `python -m stone_pipeline.config.server`.
"""
from __future__ import annotations

import ast
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
PKG_ROOT = CONFIG_DIR.parent            # .../stone_pipeline
REPO_ROOT = PKG_ROOT.parent
CONTROL_PREFIX = "stone_pipeline.config.control"


def _is_control(mod: str) -> bool:
    return mod == CONTROL_PREFIX or mod.startswith(CONTROL_PREFIX + ".")


def _pkg_of(path: Path) -> str:
    """The containing package of a module file, dotted from the repo root (its __package__)."""
    return ".".join(path.parent.relative_to(REPO_ROOT).parts)


def _base_module_files() -> list[Path]:
    """Every base config module: config/*.py (incl __init__.py) and config/decisions_store/*.py, minus the
    control package and the thin server entry (which legitimately references control only inside __main__ and
    is pinned by its own tests below)."""
    files = [p for p in CONFIG_DIR.glob("*.py") if p.name != "server.py"]
    files += list((CONFIG_DIR / "decisions_store").glob("*.py"))
    return files


def _control_edges(path: Path) -> set[str]:
    """Control modules this file imports, resolving absolute, relative, and submodule-name imports."""
    pkg = _pkg_of(path)
    edges: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if isinstance(node, ast.Import):
            edges.update(a.name for a in node.names if _is_control(a.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                base = node.module or ""
            else:
                anchor = pkg.split(".")
                if node.level - 1:
                    anchor = anchor[: -(node.level - 1)]
                base = ".".join(anchor) + ("." + node.module if node.module else "")
            if _is_control(base):
                edges.add(base)
            for a in node.names:                       # catches `from stone_pipeline.config import control`
                full = f"{base}.{a.name}" if base else a.name
                if _is_control(full):
                    edges.add(full)
    return edges


def test_no_base_to_control_import_edge():
    offenders = {p.name: sorted(e) for p in _base_module_files() if (e := _control_edges(p))}
    assert not offenders, f"base config modules import the control layer: {offenders}"


def test_config_server_is_a_thin_entry():
    import stone_pipeline.config.server as entry
    for name in ("ConfigHandler", "dispatch", "serve", "main", "_MAX_BODY_BYTES"):
        assert not hasattr(entry, name), f"thin entry must not expose {name!r}; the impl lives in control.server"


def test_config_server_bare_import_loads_no_control():
    # In a clean interpreter a plain `import stone_pipeline.config.server` (the deploy smoke, any importer)
    # must not pull the control layer in: the base layer stays free of control at import time.
    # -I gives a clean interpreter (no cwd/PYTHONPATH/site); inject this checkout so it imports THIS code.
    code = (
        f"import sys; sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        "import stone_pipeline.config.server\n"
        "bad = [m for m in sys.modules if m == 'stone_pipeline.config.control' "
        "or m.startswith('stone_pipeline.config.control.')]\n"
        "assert not bad, bad\n"
    )
    proc = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_config_server_main_delegates_to_control_server(monkeypatch):
    from stone_pipeline.config.control import server as impl
    called = {"n": 0}
    monkeypatch.setattr(impl, "main", lambda argv=None: (called.__setitem__("n", called["n"] + 1), 7)[1])
    # run the entry as __main__ afresh (it may already be imported by another test in this module)
    monkeypatch.delitem(sys.modules, "stone_pipeline.config.server", raising=False)
    with pytest.raises(SystemExit) as exc:
        runpy.run_module("stone_pipeline.config.server", run_name="__main__")
    assert exc.value.code == 7
    assert called["n"] == 1
