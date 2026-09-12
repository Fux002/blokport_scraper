"""The declared Python floor (pyproject: >=3.11) must still import every module. CI runs 3.12, so a 3.12-only
construct (PEP 701 nested f-string quotes) ships unnoticed; this compiles the packages under a real 3.11
interpreter when one is on the PATH, and skips otherwise."""

from __future__ import annotations

import shutil
import subprocess
import sys

import pytest

_PY311 = shutil.which("python3.11")


@pytest.mark.skipif(not _PY311, reason="no python3.11 interpreter on this machine")
def test_packages_compile_under_the_declared_python_floor():
    if sys.version_info[:2] == (3, 11):
        pytest.skip("already running on the floor")
    proc = subprocess.run([_PY311, "-m", "compileall", "-q", "stone_pipeline", "scrapers", "deploy"],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
