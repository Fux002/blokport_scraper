"""The operator's decisions as ONE object every consumer reads.

A decision is a STATEMENT about a listing: for vendor S the spelling X is variety N of type T (verdict 'is',
optionally with a colour, an origin and the widen flag), or X is not a variety (verdict 'reject'). Vendor ''
is the GLOBAL level: what the spelling means for every vendor.

Whether an 'is' statement MINTS a new variety or BINDS the listing to an existing one is not stored: it is
derived when the object is built (`from_statements`) from what exists at that moment, so a statement stays
correct as the catalog changes under it (a stated name that later exists binds instead of minting twice).
  * a global 'is' statement is the spelling's meaning for everyone: a mint statement (curate creates the
    variety; the existing-variety guard makes a repeat a no-op);
  * a vendor 'is' statement always yields that vendor's binding (the matcher's override tier, which skips a
    target that does not resolve), and a vendor-level mint statement while its variety does not exist yet;
  * an origin on a statement is that vendor's origin for the variety (a global statement's for the vendor
    that asked); a mint's origin also documents the variety's origin (mint_origin_rules).

`Decisions` answers every question the stages used to ask six separate maps: the resolution order is ONE
rule, here, vendor first then global and at each level the scraped spelling first then the cleaned identity.
Pure: no store, no settings, so a test builds it from the same row shapes production does."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable

from stone_pipeline.matching import projections as proj

Key = tuple[str, str]           # (norm vendor or '', norm spelling)


def _norm(s: str | None) -> str:
    return proj.norm(s or "")


@dataclass(frozen=True)
class Statement:
    source: str                 # the vendor the statement was made FOR ('' = every vendor)
    spelling: str               # normalized scraped spelling (the key)
    display: str                # the spelling as scraped
    verdict: str                # 'is' | 'reject'
    name: str | None = None     # verdict 'is': the variety the listing IS (None = the spelling itself)
    stone_type: str | None = None
    color: str | None = None
    origin_iso: str | None = None
    widen: bool = False
    asked_by: str = ""          # the vendor whose card produced a global statement

    @property
    def is_mint(self) -> bool:
        return self.verdict == "is"

    @property
    def renamed(self) -> bool:
        return bool(self.name) and _norm(self.name) != self.spelling


@dataclass(frozen=True)
class Decisions:
    statements: dict[Key, Statement] = field(default_factory=dict)          # mint and reject statements
    bindings: dict[Key, tuple[str, str]] = field(default_factory=dict)      # (vendor, spelling) -> (name, type or '')
    vendor_origins: dict[tuple[str, str, str], str] = field(default_factory=dict)  # (vendor, name, type) -> ISO
    widened: dict[tuple[str, str, str], str] = field(default_factory=dict)         # the ones the operator widened

    @classmethod
    def empty(cls) -> "Decisions":
        return cls()

    # -- builders ------------------------------------------------------------------------------------------
    @classmethod
    def from_statements(cls, rows: Iterable[dict], exists_as: Callable[[str, str], bool],
                        alias_target: Callable[[str, str], str | None]) -> "Decisions":
        """Build from `statement` rows ({source, spelling_norm, spelling, verdict, name, stone_type, color,
        origin_iso, widen, asked_by}) and the two variety lookups the derivation needs (config.varieties on
        the ledger; injected so the rule is testable without one)."""
        statements: dict[Key, Statement] = {}
        bindings: dict[Key, tuple[str, str]] = {}
        origins: dict[tuple[str, str, str], str] = {}
        widened: dict[tuple[str, str, str], str] = {}
        for r in rows:
            src, sp = _norm(r["source"]), _norm(r["spelling_norm"])
            display = r.get("spelling") or r["spelling_norm"]
            if r["verdict"] == "reject":
                statements[(src, sp)] = Statement(source=src, spelling=sp, display=display, verdict="reject",
                                                  asked_by=r.get("asked_by") or "")
                continue
            name, stone_type = r.get("name") or display, r.get("stone_type") or ""
            # a stated name that is an existing variety's alias resolves to that variety, so the listing binds
            # to it rather than minting a duplicate of a known alias
            canonical = None
            if src and not exists_as(name, stone_type):
                canonical = alias_target(name, stone_type)
            target = canonical or name
            iso, widen = (r.get("origin_iso") or None), bool(r.get("widen"))
            if src:
                bindings[(src, sp)] = (target, stone_type)
                if iso:
                    origins[(src, _norm(target), _norm(stone_type))] = iso
                    if widen:
                        widened[(src, _norm(target), _norm(stone_type))] = iso
                if exists_as(target, stone_type) or canonical:
                    continue                                  # the vendor's listing binds; nothing to mint
            elif iso and r.get("asked_by"):
                origins[(_norm(r["asked_by"]), _norm(name), _norm(stone_type))] = iso
                if widen:
                    widened[(_norm(r["asked_by"]), _norm(name), _norm(stone_type))] = iso
            statements[(src, sp)] = Statement(
                source=src, spelling=sp, display=display, verdict="is",
                name=(r.get("name") or None), stone_type=stone_type or None, color=r.get("color") or None,
                origin_iso=iso, widen=widen, asked_by=r.get("asked_by") or "")
        return cls(statements=statements, bindings=bindings, vendor_origins=origins, widened=widened)

    @classmethod
    def from_legacy(cls, actions: dict, scoped: dict | None = None, origins: dict | None = None,
                    widen: dict | None = None) -> "Decisions":
        """Build from the legacy accessor shapes (variety_actions / scoped_aliases / origin_decisions /
        origin_widen): the projection the legacy tables carried, used by tests and by the migration's
        dual-read check. Missing seed fields are None."""
        statements: dict[Key, Statement] = {}
        for (source, spelling), d in (actions or {}).items():
            key = (_norm(source), _norm(spelling))
            action = d.get("action")
            if action == "reject":
                statements[key] = Statement(source=key[0], spelling=key[1], display=d.get("spelling") or spelling,
                                            verdict="reject", asked_by=d.get("asked_by") or "")
            elif action == "mint":
                statements[key] = Statement(
                    source=key[0], spelling=key[1], display=d.get("spelling") or spelling, verdict="is",
                    name=d.get("seed_name") or None, stone_type=d.get("seed_type") or None,
                    color=d.get("seed_color") or None, origin_iso=d.get("seed_country") or None,
                    asked_by=d.get("asked_by") or "")
        return cls(statements=statements,
                   bindings={(_norm(s), _norm(sp)): (t, tt or "") for (s, sp), (t, tt) in (scoped or {}).items()},
                   vendor_origins={(_norm(s), _norm(v), _norm(t)): iso for (s, v, t), iso in (origins or {}).items()},
                   widened={(_norm(s), _norm(v), _norm(t)): iso for (s, v, t), iso in (widen or {}).items()})

    # -- the ONE resolution rule ---------------------------------------------------------------------------
    @staticmethod
    def _keys(source: str, spelling: str, clean: str | None) -> Iterable[Key]:
        src = _norm(source)
        for level in ((src,) if src else ()) + ("",):
            for s in dict.fromkeys((_norm(spelling), _norm(clean if clean is not None else spelling))):
                if s:
                    yield (level, s)

    def _first(self, source: str, spelling: str, clean: str | None,
               pred: Callable[[Statement], bool]) -> Statement | None:
        """The first statement, vendor level first then global, scraped spelling then cleaned identity, that
        satisfies `pred`. Each question below asks with its own predicate, so a vendor statement that does not
        carry the asked aspect (no colour) still lets the global one answer, exactly as the per-aspect maps did."""
        for key in self._keys(source, spelling, clean):
            st = self.statements.get(key)
            if st is not None and pred(st):
                return st
        return None

    def for_listing(self, source: str, spelling: str, clean: str | None = None) -> Statement | None:
        """The standing statement for a listing, whatever its verdict."""
        return self._first(source, spelling, clean, lambda s: True)

    def confirm(self, source: str, spelling: str, clean: str) -> str | None:
        """'yes' for a mint statement, 'no' for a reject, None when undecided."""
        st = self.for_listing(source, spelling, clean)
        return None if st is None else ("yes" if st.is_mint else "no")

    def is_rejected(self, source: str, spelling: str, clean: str) -> bool:
        return self._first(source, spelling, clean, lambda s: s.verdict == "reject") is not None

    def seed_type(self, source: str, spelling: str, clean: str, valid: set | None = None) -> str | None:
        """The operator's minted stone type for this listing, or None. `valid` (the canonical type vocabulary,
        normalized) gates it: a mint type that is not a real Medusa type is NOT an identity, so it is skipped
        and the scan falls through to the next level -- the ONE place this rule lives, so every caller (curate's
        variety_identity, the matcher's operator-type fallback) resolves the same type from the same statement."""
        st = self._first(source, spelling, clean,
                         lambda s: s.is_mint and bool(s.stone_type)
                         and (valid is None or _norm(s.stone_type) in valid))
        return st.stone_type if st else None

    def seed_color(self, source: str, spelling: str, clean: str) -> str | None:
        st = self._first(source, spelling, clean, lambda s: s.is_mint and bool(s.color))
        return st.color if st else None

    def seed_name(self, source: str, spelling: str, clean: str) -> str | None:
        st = self._first(source, spelling, clean, lambda s: s.is_mint and bool(s.name))
        return st.name if st else None

    def scope(self, source: str, spelling: str, clean: str) -> str:
        """The level a mint on this listing lands on: the vendor (norm) when that vendor made its OWN mint on the
        spelling (its products bind through its binding, no global alias is attached), else ''."""
        st = self._first(source, spelling, clean, lambda s: s.is_mint and bool(s.source))
        return st.source if st else ""

    # -- the derived views the reference and the matcher consume ---------------------------------------------
    def seed_types(self) -> dict[Key, str]:
        """(vendor, spelling) -> operator stone type, for every mint statement that set one."""
        return {k: s.stone_type for k, s in self.statements.items() if s.is_mint and s.stone_type}

    def mint_origin_rules(self) -> dict[tuple[str, str], str]:
        """(norm variety NAME, norm type) -> ISO, for every mint statement that set an origin: keyed by the name
        the variety is CREATED under (the corrected name for a rename, else the spelling), type-scoped."""
        return {(_norm(s.name) if s.name else s.spelling, _norm(s.stone_type)): s.origin_iso
                for s in self.statements.values() if s.is_mint and s.origin_iso}
