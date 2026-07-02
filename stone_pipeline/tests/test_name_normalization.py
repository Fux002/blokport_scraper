"""The canonical name-match key: a separator, case, or accent difference must NEVER fork the
identity of the same name anywhere in the pipeline.

Regression for the Semi-Precious Stone bug -- the stone type 'Semi-Precious Stone' (hyphen) did
not match the Key slug 'semi_precious_stone' (underscore/space), so 36 varieties stayed untyped
and their products never synced. The fix is one shared normalizer (core.text.match_key) that all
resolvers key on; the matching-engine projection (proj.norm) folds the same way.
"""

from __future__ import annotations

from stone_pipeline.core.text import match_key
from stone_pipeline.matching import projections as proj

# every spelling of the same name -- hyphen, underscore, spaces, unicode en/em dash, slash, tabs
_SAME = [
    "Semi-Precious Stone",
    "semi_precious_stone",
    "semi precious stone",
    "SEMI-PRECIOUS  STONE",
    "Semi–Precious Stone",   # en dash
    "Semi—Precious/Stone",   # em dash + slash
    "semi\tprecious\nstone",
]


def test_match_key_folds_separators_case_accents():
    assert len({match_key(v) for v in _SAME}) == 1, "a separator/case difference forked the name"
    assert match_key("Rosa Porriño") == match_key("Rosa Porrino"), "accent must fold"
    assert match_key("") == "" and match_key(None) == ""  # type: ignore[arg-type]


def test_proj_norm_folds_the_same_way():
    # the matching-engine key must not diverge: underscore (a \w char) and unicode dashes included
    assert len({proj.norm(v) for v in _SAME}) == 1, "proj.norm forked the name on a separator"
