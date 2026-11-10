"""Variety identity: the ONE rule the whole pipeline dedups by.

A variety is (stone-type, name), with no category (PHASE-1-SPEC rev 8, 2a): one record per (type, name). Two Keys
with the same identity are the SAME variety twice -- the class that ships duplicate products (a Key whose
name-slug baked in an alias: `..._verde_ubatuba_ubatuba_...` vs the clean `..._verde_ubatuba_...`). Type comes
from the Key through the ONE parser (phase1.variety_key: the part before the first `__`), so `sodalite_syenite` is
one type and a genuine multi-type homonym like Aqua Blue as gneiss/granite/marble/onyx stays distinct. Name is the DISPLAY Name accent-folded + slugged, so an
alias in a legacy Key's slug never splits the variety. This single rule is used by emit (collapse
before write), the matcher reference (exclude non-survivors), and the ledger reconcile -- one identical
rule everywhere, so a dup can never enter from any path."""

from __future__ import annotations

import csv
from functools import lru_cache

from stone_pipeline.adapters.tokens import explicit_type_word
from stone_pipeline.core.text import slugify
from stone_pipeline.phase1.variety_key import variety_key_name_slug, variety_key_type
from stone_pipeline.reference.loaders.common import env, norm

def type_slug_from_key(key: str) -> str:
    """The type slug of a variety Key: the part before the first `__` (phase1.variety_key, the one parser; no type
    list). Raises ValueError on a Key that is not of the form, an old prefixed Key included, never a guessed type."""
    return variety_key_type(key)


def _name_slug(name: str) -> str:
    return slugify(name, "_")


def variety_identity(key: str, name: str) -> tuple[str, str]:
    """(type-slug, folded name-slug) -- the dedup identity of a variety. See the module docstring."""
    return (type_slug_from_key(key), _name_slug(name))


def survivor_of(keys, seed_keys) -> str:
    """The one Key to keep for an identity group: prefer a committed-seed Key, then the cleanest name-slug
    (shortest -- an alias-laden slug loses), then lexical. Deterministic and stable across runs."""
    return min(keys, key=lambda k: (k not in seed_keys, len(variety_key_name_slug(k)), k))


@lru_cache(maxsize=1)
def pristine_seed_keys() -> frozenset:
    """The Key set of the committed CLEAN seed -- the survivor authority for the identity collapse. Read
    from the read-only baked pristine copy; falls back to the live base (which IS the committed seed on a
    clean checkout). Empty if neither exists (collapse then just prefers the clean-slug Key)."""
    paths = env().SETTINGS.paths
    for p in (paths.variants_export_base_seed_csv, paths.variants_export_base_csv):
        if p.exists():
            with p.open(encoding="utf-8-sig", newline="") as h:
                return frozenset((r.get("Key") or "").strip()
                                 for r in csv.DictReader(h) if (r.get("Key") or "").strip())
    return frozenset()


def collapse_to_survivors(rows, seed_keys=None, key_col="Key", name_col="Name",
                          alias_col="Aliases", image_col="Image"):
    """Collapse every `variety_identity` group in `rows` to ONE survivor: fold the losers' name + aliases
    into the survivor (nothing searchable lost) and inherit a loser's image when the survivor has none,
    then drop the losers. Order-preserving on survivors. This is the load-bearing dedup -- run on the
    emitted variant set it makes the whole catalog a fixed point, so a duplicate can never ship regardless
    of a dirty base or a stale live export."""
    seed_keys = env().pristine_seed_keys() if seed_keys is None else seed_keys
    groups: dict = {}
    for r in rows:
        groups.setdefault(variety_identity(r.get(key_col, ""), r.get(name_col, "")), []).append(r)
    survivors: dict = {}
    for grp in groups.values():
        if len(grp) == 1:
            survivors[grp[0][key_col]] = grp[0]
            continue
        keep_key = survivor_of([r[key_col] for r in grp], seed_keys)
        keep = dict(next(r for r in grp if r[key_col] == keep_key))
        aliases = [a for a in (keep.get(alias_col) or "").split("|") if a]
        seen = {a.casefold() for a in aliases}
        own = (keep.get(name_col) or "").casefold()
        for r in grp:
            if r[key_col] == keep_key:
                continue
            for a in [r.get(name_col)] + (r.get(alias_col) or "").split("|"):
                a = (a or "").strip()
                if a and a.casefold() != own and a.casefold() not in seen:
                    aliases.append(a)
                    seen.add(a.casefold())
            if not (keep.get(image_col) or "").strip() and (r.get(image_col) or "").strip():
                keep[image_col] = r[image_col]
        keep[alias_col] = "|".join(aliases)
        survivors[keep_key] = keep
    out, emitted = [], set()
    for r in rows:                       # preserve original order on the surviving keys
        k = r[key_col]
        if k in survivors and k not in emitted:
            out.append(survivors[k])
            emitted.add(k)
    return out


def is_mistyped_variant(key: str, name: str) -> bool:
    """An existing variant whose NAME carries an unambiguous stone-type word NOT present in its own
    Key's type is mis-typed -- e.g. 'Azul White Quartzite' under a slab_ONYX_ key. Listed for
    deletion so the cleaning flow re-mints it correctly. The Key's type may be MULTI-WORD
    ('slab_dolomite_marble_...'), so we match the longest known type slug that prefixes the Key's
    post-branch portion rather than taking parts[1] (which falsely flagged 'Dolomite Marble' /
    'Sodalite Syenite' variants named with their second word)."""
    ntw = explicit_type_word(name)
    if not ntw:
        return False
    key_type = type_slug_from_key(key)   # the one parser, multi-word aware
    # Resolve the name's type word to its CANONICAL type before comparing (the same synonym map
    # resolve_id uses), so 'Agata' -> 'Agate' and 'Sodalite' -> 'Sodalite Syenite' are judged against
    # the canonical, not the literal foreign spelling -- else 'Agata Blue' keyed 'agate' would falsely
    # flag ('agata' != 'agate'). Mis-typed iff the canonical type shares NO token with the Key's type
    # (token overlap, so a name 'Marble' is consistent with a 'dolomite_marble' Key).
    canonical = env().load_synonyms("type").get(norm(ntw), ntw)
    return set(norm(canonical).split()).isdisjoint(key_type.split("_"))
