"""Regenerate the shared name-key vectors (stone_pipeline/tests/fixtures/name_key_vectors.json).

The fixture is a PAIRED file: a byte-identical copy lives in the backend as
src/model/catalog/__tests__/name-key.vectors.json, and a change to either normaliser is a paired PR
in both repos. Expected values come from THIS repo's match_key, so run it after a change to
stone_pipeline/core/text.py, at the Unicode floor (Python 3.11 = Unicode 14.0.0), from the repo root:

    python3.11 -m stone_pipeline.tests.gen_name_key_vectors

Every input uses only code points assigned at the floor, so the expected keys are the same on every
runtime at or above it (each repo's test asserts its runtime is at least the floor).
"""
import json
import sys
import unicodedata
from pathlib import Path

from stone_pipeline.core.text import (_TRANSLIT, AliasRefused, UnfoldableName, alias_key, clean_alias,
                                      clean_identifier, clean_name, match_key)

OUT = Path(__file__).parent / "fixtures" / "name_key_vectors.json"
FLOOR = "14.0.0"


def assigned_by_floor(s):
    # the generator runs AT the floor (asserted in main), so category 'Cn' means unassigned in that version
    return all(unicodedata.category(c) != "Cn" for c in s)


def _cleaned(clean, s):
    # the cleaned value, or None when it is refused (a letter with no ASCII form)
    try:
        return clean(s)
    except UnfoldableName:
        return None


def _alias(s):
    # the alias display (clean_alias, case kept), or None when it is refused (mojibake) or nothing is left
    try:
        return clean_alias(s) or None
    except AliasRefused:
        return None


def build():
    cases, seen = [], set()

    def add(group, s):
        if s in seen:
            return
        assert assigned_by_floor(s), f"unassigned code point in {s!r}"
        seen.add(s)
        cases.append({"group": group, "input": s, "expected": match_key(s), "display": _cleaned(clean_name, s),
                      "identifier": _cleaned(clean_identifier, s), "alias": _alias(s), "alias_key": alias_key(s)})

    for ch in sorted({chr(k) for k in _TRANSLIT}):
        add("translit", ch); add("translit", f"Stone {ch}x {ch.lower()}{ch}")
    for s in ["\u00df", "\u1e9e", "Stra\u00dfe", "STRASSE", "Gro\u00dfer Wei\u00df", "\u00df\u00df"]:
        add("eszett", s)
    for s in ["\u01e2", "\u01e3", "\u01fc", "\u01fd", "\u01fe", "\u01ff", "\u01e2gean", "\u01festfold",
              "Bl\u01ffy"]:
        add("translit_with_mark", s)
    for s in ["Rosa Porri\u00f1o", "S\u00e3o Gabriel", "Cr\u00e8me Br\u00fbl\u00e9e", "\u00c7anakkale",
              "\u00c6gean Blue", "\u0152il de Perdrix", "M\u00e1rmol Crema Marfil", "Dalma\u021bia", "\u0160ivec",
              "\u017dula", "\u0106ao", "T\u01ceo", "\u00cblva", "\u0130zmir", "I\u011fd\u0131r", "Fa\u00e7ade",
              "Na\u00efve", "\u00c5ngstr\u00f6m", "Sm\u00f8rrebr\u00f8d", "\u0141\u00f3d\u017a", "\u00d1and\u00fa",
              "\u016ala", "\u01c4emal"]:
        add("accents", s)
    for s in ["Rosa Porrin\u0303o", "Cre\u0300me Brule\u0301e", "a\u0301b", "\u0301lead", "trail\u0301",
              "Sa\u0303o Gabriel", "C\u0327anakkale", "e\u0301\u0301", "o\u0308\u0304", "Ma\u0331rmol Crema",
              "e\u0301\u0331te", "a\u0331", "x\u0331y", "Nero\u20dd", "a\u0489b"]:
        add("combining_nfd", s)
    for s in ["\ufb01ne", "\ufb02ow", "\uff27\uff16\uff10\uff13", "\u2163 Grey", "m\u00b2", "x\u00b3",
              "\u00bd slab", "\u24b6bc", "\u210c", "\u2113", "\u2122 Stone", "\u2116 5",
              "\uff76\uff80\uff76\uff85", "Absolute Black\u2122", "Silestone\u00ae", "\u00bc", "Para\u0140lel",
              "Film 120\u00b5m", "a\u1fefb", "a\uff40b", "AA\uff0fBB", "Black\uff06Gold", "D\uff07Oro", "a\u210fb",
              "a\u2474b", "a\u2488b"]:
        add("compat", s)
    for c in "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~":
        add("ascii_punct", f"a{c}b")
    for s in ["Semi-Precious Stone", "semi_precious_stone", "Black & Gold", "St. Laurent", "O'Brien",
              "(Star Galaxy)", "[alt]", "a\u2014b", "a\u2013b", "a\u2010b", "a\u2026b", "a\u00b7b", "a\u2022b",
              "50%", "\u00bfQu\u00e9?", "\u00abNero\u00bb", "\u201eNero\u201c", "\u2018Nero\u2019", "a\u2044b",
              "a\u2215b", "a\u00b7b", "a\u00a7b", "a\u00b6b", "a\u00b0b", "a\u00b1b"]:
        add("punct", s)
    for s in ["  Lead  ", "a\tb", "a\nb", "a\rb", "a\vb", "a\fb", "a\u00a0b", "a\u1680b", "a\u2003b", "a\u2028b",
              "a\u2029b", "a\u202fb", "a\u205fb", "a\u3000b", "a\u200bb", "a\u200cb", "a\u200db", "a\u2060b",
              "a\u0085b", "a\u001cb", "a\u001db", "a\u001eb", "a\u001fb", "a\ufeffb", "\ufeffLead", "_lead_",
              "a__b", "a - b", "a\u00adb", "a\u0092b", "Semi\u0096Precious", "a\u0000b", "a\u0080b"]:
        add("whitespace", s)
    for s in ["\u9ed1\u91d1\u7802", "Nero \u9ed1 Marquina", "Nero\u9ed1Marquina", "\u0393\u03ba\u03c1\u03b9",
              "\u0421\u0435\u0440\u044b\u0439 \u0433\u0440\u0430\u043d\u0438\u0442",
              "\u0448\u043b\u0438\u0444\u043e\u0432\u0430\u043d\u043d\u044b\u0439", "\u05e9\u05d7\u05d5\u05e8",
              "\u0623\u0633\u0648\u062f", "\u0e20\u0e32\u0e29\u0e32", "\ud55c\uad6d", "\u65e5\u672c\u306e\u77f3",
              "\U0001f600 Nero", "Nero\U0001f600Marquina", "Nero\u0301"]:
        add("non_latin", s)
    for s in ["", " ", "-", "_", "...", "G603", "G 603", "g-603", "G603 ", "ABC123def", "007", "2cm 3 cm",
              "\u216b", "a", "A", "1", "aB1", "x" * 64, "'Nero'", "O''Brien", "Rock 'n' Roll", "''", "a-'b",
              "Black--Gold", "a`b", "a\u02bcb", "a\u02bbb", "Black\uf005Gold",
              "a&b&c", "Black\ufe60Gold", "&Gold", "Rosa Porrin\u0303o & Gold"]:
        add("edge", s)
    for s in ["Absolute Black", "absolute  black", "ABSOLUTE-BLACK", "Bianco Carrara C/D", "Nero Marquina (Spain)",
              "Tan Brown", "Kashmir White", "Juparana Colombo", "Blue Pearl GT", "Emperador Dark", "Calacatta Oro",
              "Crema Marfil Coto", "Verde Ubatuba", "Giallo Veneziano", "Labrador Antique"]:
        add("names", s)
    # the alias kind: refused (mojibake only), kept and cleaned cases
    for s in ["C?te D", "K?r?ehir Nero Picasso", "K?r?ehir Nero Picasso Marble", "C\ufffdte", "C\u00f4te d'Azur",
              "Hu\u00e1ngji\u0101 H\u01d4p\u00f2", "\u5df4\u6bd4\u4f26\u91d1", "3D\u7070", "Giallo D`istria",
              "Celestial Splendor\u200b", "Cel\u200bestial", "Black & White", "Bianco Carrara C/d", "China St.laurent",
              "In Russian:nigrozero", "Who? Me", "Picass?", "Cafe\u0301", "x\u1dc0y", "x\u0331y", "\u00b4Nero\u00b4",
              "Verde \u2013 Lapponia", "Kir\u015fehir Nero Picasso", "Bianco (Carrara) #1/2", "Bian\u200bco",
              "x\u0331'y", "Kir\u015fehir's", "'Nero' Marquina", "D''Oro", "\u5df4'\u91d1", "1'2", "a' b",
              # the paired alias key: pure CJK; mixed (never its Latin part's key); "3D\u7070" never
              # "3D"'s; Greek with a final sigma (lower(), not casefold); a Common-script letter (U+210C, the second
              # path); Devanagari vowel signs (marks kept in the key); Arabic-Indic digits (Nd); German sharp s on
              # the second path; dev's mixed value; a dash alone (an empty key)
              "Wratza \u7ef4\u7eb3\u591a", "Wratza", "3D", "\u039c\u0391\u03a1\u039c\u0391\u03a1\u039f\u03a3",
              "\u210c", "\u0928\u093f\u0930\u094d\u092e\u0932", "\u0663\u0664 \u0645", "Stra\u00dfe \u7ef4",
              "\u767d\u73ab\u7470 - B\u00e1i M\u00e9igu\u012b", "\u2014"]:
        add("alias", s)
    return cases


def main():
    assert unicodedata.unidata_version == FLOOR, f"generate at Unicode {FLOOR}, got {unicodedata.unidata_version}"
    cases = build()
    doc = {
      "_header": (
        "PAIRED FILE. Byte-identical copies: BE src/model/catalog/__tests__/name-key.vectors.json and SC "
        "stone_pipeline/tests/fixtures/name_key_vectors.json. A change to either normaliser is a PAIRED PR in both "
        "repos, each green on the same vectors. Generated by SC stone_pipeline/tests/gen_name_key_vectors.py from "
        "the scraper's match_key (SC stone_pipeline/core/text.py). PREPARATION, shared by the key and both display "
        "rules (the key skips it for ASCII text, where it never changes the key): (a) typographic forms unified "
        "(curly quotes, the backtick, the acute accent and the modifier apostrophe to ', curly double quotes to "
        "\\\", the minus sign to -, the zero-width space to a space, the micro sign to u); (b) on the ORIGINAL "
        "characters, a non-ASCII dash (Pd) to -, a non-ASCII punctuation or symbol (P*, S*) whose NFKD, mapped "
        "through (a), is one ASCII punctuation character (a fullwidth, small, vertical, superscript or subscript "
        "form) to that character, any other non-ASCII punctuation, symbol, private-use (Co), unassigned (Cn) or "
        "surrogate (Cs) character to a space; (c) NFKD-decompose and drop every mark (M*); (d) step (b) again on "
        "what decomposition yields; (e) every whitespace or control (Cc) character to a space, every format (Cf) "
        "character removed. KEY (`expected`, match_key): (0) every '&' becomes ' and ' (owner D1, 2026-11-04); (1) "
        "every [^\\w\\s] (Python unicode \\w, \\s) and '_' becomes a space; (2) ascii_fold: unless plain ASCII, "
        "NFKD-decompose, drop every mark, transliterate the letters with no decomposition (_TRANSLIT), drop "
        "remaining non-ASCII; (3) casefold, which after step 2 is ASCII lower-case; (4) split on whitespace, join "
        "with one space. DISPLAY, two kinds (owner 2026-11-03): REFUSED (null) if a letter other than a modifier "
        "letter (Lm) has no ASCII form; otherwise ascii_fold. NAME (`display`, clean_name): '&' to ' and '; every "
        "character outside [A-Za-z0-9 '-] to a space; apostrophe runs to one; an apostrophe not between two "
        "letters or digits to a space; hyphen runs and spaces around a hyphen collapse to one hyphen; whitespace "
        "collapsed; leading and trailing hyphens and spaces stripped. IDENTIFIER (`identifier`, clean_identifier): "
        "printable ASCII kept, whitespace collapsed and trimmed. Case is kept. ALIAS (`alias`, clean_alias; 34's "
        "OWNED-NAMES-CLEAN row, 13 Nov; Mas: mojibake is the ONLY refusal, no length bound): REFUSED (null) first if "
        "the RAW value holds '?' or U+FFFD with a letter (any script, L*) on both sides (mojibake: 'K?r?ehir'; 'Who? "
        "Me' and 'Picass?' are not). Otherwise, in order: (1) every format (Cf) character removed (a zero-width "
        "space goes, never a space); (2) the alias typographic table (the backtick, the acute accent, the curly "
        "single quotes and the modifier apostrophe to ', curly double quotes to \\\", the minus sign to -) and every "
        "dash (Pd) to -; (3) NFC (accents kept, never folded; no script dropped); (4) '&' to ' and '; (5) every "
        "character that is not a letter (L*), a combining mark in U+0300-U+036F, an ASCII digit, a space, an "
        "apostrophe or a hyphen to a space; (6) apostrophe runs to one; an apostrophe is kept only when a letter "
        "(L*), a combining mark in U+0300-U+036F or an ASCII digit is before it AND a letter (L*) or an ASCII digit "
        "is after it, else it becomes a space; hyphen runs and spaces around a hyphen collapse to one hyphen; "
        "whitespace collapsed; leading and trailing hyphens and spaces stripped; nothing left is REFUSED (null). "
        "Case is kept. The database class it equals: ^[[:alpha:]\\u0300-\\u036f0-9 '-]+$. ALIAS KEY (`alias_key`, "
        "alias_key; Mas, 13 Nov): the key of an alias value. If the RAW value has no letter (L*) outside the Latin "
        "script (Scripts.txt sc=Latn, JS \\p{Script=Latin}; a letter of script Common is outside), it is the "
        "matching key (`expected`). Otherwise: every format (Cf) character removed; NFKC; the default lowercase "
        "mapping (Python str.lower, JS toLowerCase; NOT casefold, so a final sigma stays); every character that is "
        "not a letter (L*), a mark (M*) or a decimal digit (Nd, any script) to a space; whitespace collapsed and "
        "trimmed. An empty key is refused by the callers. UNICODE: every input uses only code "
        "points assigned in Unicode " + FLOOR + ", the floor across the runtimes in use (SC deploy and CI: Python "
        "3.12 = Unicode 15.0.0; SC CI syntax floor: Python 3.11 = 14.0.0). Both repos must agree on every case; "
        "each repo's test asserts its runtime Unicode version is at least " + FLOOR + "."),
      "generator": {"python": sys.version.split()[0], "unicodedata": unicodedata.unidata_version, "floor": FLOOR},
      "cases": cases,
    }
    OUT.write_text(json.dumps(doc, ensure_ascii=True, indent=2) + "\n", encoding="ascii")
    print(f"{len(cases)} cases -> {OUT}")


if __name__ == "__main__":
    main()
