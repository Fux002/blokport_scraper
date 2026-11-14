"""P2-10 C4 (with C2): no variants file's Aliases is an input. Emit derives every variety's aliases from the alias
stores (D1) and never reads a file's Aliases cells (D3); curate proposes a spelling for an EXISTING variety to
learned_alias (C2) and its delta holds only Keys the export lacks (D4), so its delta writer reads no row's aliases.
These keep emit's base-as-input alias merge and curate's global learned-alias write gone (the plan's C7 list). No em
dashes."""
from __future__ import annotations

import ast
from pathlib import Path

from stone_pipeline.phase1.variety_key import new_key
from stone_pipeline.stages import emit_catalog

PACKAGE = Path(__file__).resolve().parents[1]


def test_emit_reads_no_variants_file_s_aliases(tmp_path):
    base = tmp_path / "variants_export_base.csv"
    base.write_text(f"Key,Name,Type,Aliases\n{new_key('Marble', 'Casa Blue')},Casa Blue,Marble,Azul Casa|Casablue\n",
                    encoding="utf-8")
    assert [r["Aliases"] for r in emit_catalog._rows(base)] == [""]


def test_curate_s_delta_reads_no_alias():
    tree = ast.parse((PACKAGE / "stages" / "curate" / "output.py").read_text(encoding="utf-8"))
    found = [node.lineno for node in ast.walk(tree)
             if (isinstance(node, ast.Constant) and node.value == "Aliases")
             or (isinstance(node, ast.Attribute) and node.attr == "aliases")]
    assert found == []
