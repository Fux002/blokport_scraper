"""Regenerate the paired variety-key vectors (stone_pipeline/tests/fixtures/variety_key_vectors.json).

PAIRED FILE: a byte-identical copy lives in the backend as src/model/catalog/variety-key.vectors.json. A
change to the Key rule on either side is a paired PR in both repos, each green on these vectors. The valid
keys are minted by THIS repo's real _slug_us (stones stages/curate/keys) through phase1.variety_key.new_key,
so the backend proves its varietyKeyProblem and varietyKeyType against real scraper Keys. The uuid5 is
deterministic, so the file is stable. Run from the repo root:

    python3 -m stone_pipeline.tests.gen_variety_key_vectors

Three case groups:
  mint   {type,name} minted to a Key; problem null, parsed_type = the type slug. Covers the shapes easy to
         get wrong: a multi-word type, a type slug that is a prefix of another, an accented name, a bracketed
         name, an ampersand (the slug reads '&' as 'and', as the match key does: owner D1).
  parse  a literal valid Key; problem null, parsed_type the type before the first `__` (multi-underscore
         type slugs round-trip).
  refuse a literal malformed Key; the varietyKeyProblem value and parsed_type null.
"""
import json
from pathlib import Path

from stone_pipeline.phase1.variety_key import new_key, variety_key_problem, variety_key_type
from stone_pipeline.stages.curate.keys import _slug_us

OUT = Path(__file__).parent / "fixtures" / "variety_key_vectors.json"

# (type name, variety name) pairs minted to real Keys. The comment on each names the shape it pins.
_MINT = [
    ("Marble", "Absolute Black"),                 # the plain case
    ("Granite", "Tan Brown"),                     # the plain case
    ("Dolomite Marble", "Statuario"),             # multi-word type -> multi-underscore type slug
    ("Semi-Precious Stone", "Amethyst"),          # multi-word, hyphen and the word 'precious'
    ("Dolomite", "Perla"),                        # type slug 'dolomite' is a prefix of 'dolomite_marble'
    ("Dolomite Marble", "Perla"),                 # same name, the longer type: a DIFFERENT new Key
    ("Granite", "Rosa Porrino"),                  # ascii control for the accented pair below
    ("Granite", "Rosa Porriño"),             # accented name folds to the ascii slug (same Key as above)
    # RAW input, NOT a canonical identity: new_key does not strip brackets; the migration applies
    # split_bracket_alias upstream, so the real identity is "G562" with "Maple Red" as an alias (see header).
    ("Granite", "G562 (Maple Red)"),
    ("Onyx", "Black and Gold"),                   # D1: '&' reads 'and' ...
    ("Onyx", "Black & Gold"),                     # ... so this is the SAME Key as the line above
    ("Quartzite", "Taj Mahal"),                   # two words
    ("Limestone", "Crème Brûlée"), # multiple accents
]

# Literal valid Keys, to pin the parse on multi-underscore type slugs.
_PARSE = [
    "dolomite_marble__statuario_8a1b2c3d",
    "dolomite__perla_8a1b2c3d",
    "semi_precious_stone__amethyst_8a1b2c3d",
    "marble__absolute_black_8a1b2c3d",
]

# Literal malformed Keys -> the expected varietyKeyProblem.
_REFUSE = [
    "slab_marble_absolute_black_8a1b2c3d",   # a prefixed old Key: no `__`
    "marble_absolute_black_8a1b2c3d",        # no `__`
    "",                                      # empty string
    "nounderscorekey",                       # no `__`
    "__absolute_black_8a1b2c3d",             # empty type
    "marble__",                              # empty name
    "marble____8a1b2c3d",                    # two `__` -> a second separator (empty middle)
    "a__b__c",                               # a second separator
    "dolomite__marble__statuario",           # a second separator (ambiguous old parse)
]


def build():
    cases = []
    for type_name, name in _MINT:
        slug_type, slug_name = _slug_us(type_name), _slug_us(name)
        key = new_key(type_name, name)
        problem = variety_key_problem(key)
        assert problem is None, f"minted an invalid Key for {type_name!r},{name!r}: {problem}"
        cases.append({"group": "mint", "type": type_name, "name": name, "slug_type": slug_type,
                      "slug_name": slug_name, "key": key, "problem": None,
                      "parsed_type": variety_key_type(key)})
    for key in _PARSE:
        problem = variety_key_problem(key)
        assert problem is None, f"parse vector is not valid: {key!r} -> {problem}"
        cases.append({"group": "parse", "type": None, "name": None, "slug_type": None, "slug_name": None,
                      "key": key, "problem": None, "parsed_type": variety_key_type(key)})
    for key in _REFUSE:
        problem = variety_key_problem(key)
        assert problem is not None, f"refuse vector unexpectedly valid: {key!r}"
        cases.append({"group": "refuse", "type": None, "name": None, "slug_type": None, "slug_name": None,
                      "key": key, "problem": problem, "parsed_type": None})
    return cases


def main():
    doc = {
        "_header": (
            "PAIRED FILE. Byte-identical copies: BE src/model/catalog/variety-key.vectors.json and SC "
            "stone_pipeline/tests/fixtures/variety_key_vectors.json. A change to the variety Key rule on "
            "either side is a PAIRED PR in both repos, each green on these vectors. Generated by SC "
            "stone_pipeline/tests/gen_variety_key_vectors.py from the scraper's _slug_us (SC "
            "stages/curate/keys) through phase1.variety_key.new_key. The KEY is `{type slug}__{name slug}_"
            "{uuid5}` (D1, section 2a): the type slug is _slug_us(type), the name slug is _slug_us(name), the "
            "uuid5 is in the key namespace over `slug_type:slug_name`. varietyKeyProblem (field `problem`): "
            "null for a Key of the form; otherwise `no_type_separator` (no `__`), `empty_type` (nothing before "
            "the first `__`), `empty_name` (nothing between the `__` and the uuid), or `second_type_separator` "
            "(a second `__`). varietyKeyType (field `parsed_type`): the part before the first `__`, for a Key "
            "that passes; null for a refused one. The `mint` cases are real Keys from _slug_us; `parse` and "
            "`refuse` are literal Keys. A prefixed Key (slab_/block_/tile_) has no `__`, so it is "
            "`no_type_separator` by the same single rule, which is what the DoD grep asks for. NOTE: new_key "
            "does NOT apply split_bracket_alias; a mint case's `name` is the RAW string, so a bracketed name "
            "(for example `G562 (Maple Red)`) slugs to the bracketed form here. The migration cleans the Name "
            "first, so a real identity's canonical name is the clean one; do not read a bracketed mint case as "
            "a canonical identity."),
        "generator": {"separator": "__", "namespace": "6f9619ff-8b86-d011-b42d-00cf4fc964ff"},
        "cases": build(),
    }
    OUT.write_text(json.dumps(doc, ensure_ascii=True, indent=2) + "\n", encoding="ascii")
    print(f"{len(doc['cases'])} cases -> {OUT}")


if __name__ == "__main__":
    main()
