"""The operator's decisions as ONE object every consumer reads.

A decision is about a LISTING: for vendor S the spelling X is variety N of type T (a `Statement` with verdict
'is'), or X is not a variety (verdict 'reject'). Vendor '' is the GLOBAL level: what the spelling means for
every vendor. A vendor-scoped BINDING ("for S, X is the existing variety N") and a vendor ORIGIN ("for S, N
of type T comes from ISO") ride alongside.

`Decisions` is loaded once (decisions_store.load_decisions) and answers every question the stages used to ask
six separate maps: the resolution order is ONE rule, here, vendor first then global and at each level the
scraped spelling first then the cleaned identity. Pure: no store, no settings, so it can be built in a test
from the legacy dict shapes (`from_legacy`) exactly as production builds it."""

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
    asked_by: str = ""          # the vendor whose card produced a global statement

    @property
    def is_mint(self) -> bool:
        return self.verdict == "is"

    @property
    def renamed(self) -> bool:
        return bool(self.name) and _norm(self.name) != self.spelling


@dataclass(frozen=True)
class Decisions:
    statements: dict[Key, Statement] = field(default_factory=dict)
    bindings: dict[Key, tuple[str, str]] = field(default_factory=dict)      # (vendor, spelling) -> (name, type or '')
    vendor_origins: dict[tuple[str, str, str], str] = field(default_factory=dict)  # (vendor, name, type) -> ISO
    widened: dict[tuple[str, str, str], str] = field(default_factory=dict)         # the ones the operator widened

    @classmethod
    def empty(cls) -> "Decisions":
        return cls()

    @classmethod
    def from_legacy(cls, actions: dict, scoped: dict | None = None, origins: dict | None = None,
                    widen: dict | None = None) -> "Decisions":
        """Build from the legacy accessor shapes: `actions` = decisions_store.variety_actions() ({(source,
        spelling): {'action', 'seed_*', 'spelling', 'source', 'asked_by'}}), `scoped` = scoped_aliases(),
        `origins` = origin_decisions(), `widen` = origin_widen(). Missing seed fields are None."""
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

    def seed_type(self, source: str, spelling: str, clean: str) -> str | None:
        st = self._first(source, spelling, clean, lambda s: s.is_mint and bool(s.stone_type))
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
