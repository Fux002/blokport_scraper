"""Tier-7 alias resolver: an entity-resolution model that decides whether a new variety name is
an ALIAS of an existing variant or a genuinely NEW stone — with a calibrated probability instead
of a flat fuzzy threshold (which mis-files siblings like 'Cristallo Divine' vs 'Cristallo Bianco').

Trained per run on the backbone's own aliases (positives) + each variety's fuzzy-nearest DISTINCT
varieties (hard negatives), using multi-field features: name similarity, token structure, the
'difference is only generic descriptor words' pattern, stone type, and colour. Pure numpy +
rapidfuzz logistic regression; train-time held-out accuracy is reported so it is never a black box.

Decision is a confidence dial: P >= hi -> alias, P <= lo -> mint, the uncertain middle -> review.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np
from rapidfuzz import distance, fuzz, process

from stone_pipeline.core import logfmt
from stone_pipeline.core.text import match_key

log = logfmt.get_logger("alias_resolver")
# words that, when they are the ONLY difference between two names, signal an alias rather than a
# distinct variety (base + descriptor): 'Bardiglio Nuvolato' -> 'Marmo Bardiglio Nuvolato Apuano'.
_GENERIC = frozenset({
    "marble", "granite", "slab", "slabs", "stone", "tile", "tiles", "block", "blocks",
    "natural", "polished", "honed", "brazilian", "brazil", "italian", "turkish", "indian",
    "premium", "classic", "extra", "standard", "the", "and",
})


# de-spaced char similarity above which a would-be 'mint' is downgraded to review (typo net)
_CHAR_REVIEW_FLOOR = 0.90


def _norm(s: str) -> str:
    # the shared canonical key: ascii-fold accents too, so 'Porriño'/'Porrino' don't look different
    # to the alias-vs-mint model (which would otherwise mint a duplicate variety).
    return match_key(s)


def _toks(s: str) -> set[str]:
    return set(_norm(s).split())


def _features(a: str, b: str, ta: str, tb: str, ca: set[str], cb: set[str]) -> list[float]:
    A, B = set(a.split()), set(b.split())
    inter, uni = A & B, A | B
    extra = uni - inter
    return [
        distance.JaroWinkler.normalized_similarity(a, b),
        fuzz.token_sort_ratio(a, b) / 100,
        fuzz.token_set_ratio(a, b) / 100,
        fuzz.WRatio(a, b) / 100,
        len(inter) / max(1, len(uni)),                          # token Jaccard
        1.0 if (A and B and (A <= B or B <= A)) else 0.0,       # one is a token-subset of the other
        len(inter) / max(1, min(len(A), len(B))),               # shared / smaller
        min(len(a), len(b)) / max(1, max(len(a), len(b))),      # length ratio
        1.0 if a.split()[:1] == b.split()[:1] else 0.0,         # first token equal
        len(set(a) & set(b)) / max(1, len(set(a) | set(b))),    # char-set Jaccard
        1.0 if (extra and extra <= _GENERIC) else 0.0,          # only-difference is generic words -> alias
        1.0 if (ta and tb and ta == tb) else 0.0,               # same stone type
        1.0 if (ca and cb and (ca & cb)) else 0.0,              # colour overlap
    ]


@dataclass
class Decision:
    verdict: str   # "alias" | "mint" | "review"
    prob: float


@dataclass
class AliasResolver:
    w: np.ndarray
    b: float
    mu: np.ndarray
    sd: np.ndarray
    hi: float
    lo: float
    accuracy: float    # held-out, within the confident band
    coverage: float    # fraction auto-decided at the confident band
    n_pos: int
    n_neg: int

    def prob(self, a: str, ta: str, ca, b: str, tb: str, cb) -> float:
        x = (np.array(_features(_norm(a), _norm(b), _norm(ta), _norm(tb),
                                {_norm(c) for c in (ca or [])}, {_norm(c) for c in (cb or [])}))
             - self.mu) / self.sd
        return float(1.0 / (1.0 + np.exp(-(x @ self.w + self.b))))

    def decide(self, a: str, ta: str, ca, b: str, tb: str, cb) -> Decision:
        return self.decide_against(a, ta, ca, [b], tb, cb)

    def decide_against(self, a: str, ta: str, ca, surfaces: list[str], tb: str, cb) -> Decision:
        """Score the new name against ALL surfaces of the candidate variety — its canonical name
        AND its known aliases — and take the strongest. A scrape that matches an existing alias is
        an alias, even if it's far from the canonical name (e.g. 'Ice Burg' ~ alias 'Ice Berg').

        Typo safety net: the token-based model can miss a misspelling that changes tokenization
        ('Ice Burg' vs 'Iceberg' share no whole token), so a would-be 'mint' whose DE-SPACED
        characters are near-identical to a surface is downgraded to review rather than minting a
        near-duplicate. Distinct prefixes ('Cristallo Divine' vs 'Cristallo Bianco') stay below it."""
        da = _norm(a).replace(" ", "")
        best_p = best_char = 0.0
        for s in surfaces:
            if not s:
                continue
            best_p = max(best_p, self.prob(a, ta, ca, s, tb, cb))
            best_char = max(best_char,
                            distance.JaroWinkler.normalized_similarity(da, _norm(s).replace(" ", "")))
        if best_p >= self.hi:
            verdict = "alias"
        elif best_p <= self.lo:
            verdict = "review" if best_char >= _CHAR_REVIEW_FLOOR else "mint"
        else:
            verdict = "review"
        return Decision(verdict, best_p)

    @classmethod
    def train(cls, varieties: list[tuple[str, list[str], str, list[str]]],
              hi: float = 0.9, lo: float = 0.2, sample: int = 6000, seed: int = 7
              ) -> "AliasResolver | None":
        """varieties: (name, aliases, stone_type, colors). Returns None when there is too little
        labelled signal to train a meaningful model (then the caller falls back to the old rule)."""
        rng = np.random.default_rng(seed)
        clean = [(_norm(n), [_norm(a) for a in al if a], _norm(t), {_norm(c) for c in (co or [])})
                 for n, al, t, co in varieties if (n or "").strip()]
        meta = {n: (t, c) for n, _, t, c in clean}
        pool = list(meta)
        if len(pool) < 50 or sum(len(al) for _, al, _, _ in clean) < 100:
            return None

        rows = clean if len(clean) <= sample else [clean[i] for i in rng.choice(len(clean), sample, replace=False)]
        X: list[list[float]] = []
        y: list[int] = []
        for n, al, t, c in rows:
            aset = set(al)
            for a in al[:5]:
                if a and a != n:
                    X.append(_features(n, a, t, t, c, c))
                    y.append(1)
            neg = 0
            for cand, _, _ in process.extract(n, pool, scorer=fuzz.token_set_ratio, limit=6):
                if cand != n and cand not in aset:
                    ct, cc = meta.get(cand, ("", set()))
                    X.append(_features(n, cand, t, ct, c, cc))
                    y.append(0)
                    neg += 1
                    if neg >= 2:
                        break
        Xa, ya = np.array(X, dtype=float), np.array(y)
        idx = rng.permutation(len(ya))
        cut = int(len(ya) * 0.8)
        tr, te = idx[:cut], idx[cut:]
        mu, sd = Xa[tr].mean(0), Xa[tr].std(0) + 1e-9
        Z = (Xa - mu) / sd
        w = np.zeros(Xa.shape[1])
        b = 0.0
        for _ in range(1500):
            p = 1.0 / (1.0 + np.exp(-(Z[tr] @ w + b)))
            g = p - ya[tr]
            w -= 0.5 * (Z[tr].T @ g / len(tr) + 1e-3 * w)
            b -= 0.5 * g.mean()
        pte = 1.0 / (1.0 + np.exp(-(Z[te] @ w + b)))
        band = (pte >= hi) | (pte <= lo)
        coverage = float(band.mean()) if len(te) else 0.0
        if band.any():
            pred = (pte[band] >= hi).astype(int)
            accuracy = float((pred == ya[te][band]).mean())
        else:
            accuracy = 0.0
        return cls(w=w, b=b, mu=mu, sd=sd, hi=hi, lo=lo, accuracy=accuracy, coverage=coverage,
                   n_pos=int(ya.sum()), n_neg=int((ya == 0).sum()))


def from_backbones():
    """Train the resolver on every category's backbone (name + aliases + type + colour) and return
    (resolver, name_norm -> (stone_type, colors, aliases)) — the lookup the mint decision uses for
    the nearest variety. Returns (None, {}) when there's too little labelled data, so the caller
    falls back to the fuzzy floor."""
    from stone_pipeline.config.settings import CATEGORIES, SETTINGS
    from stone_pipeline.matching import projections as proj
    from stone_pipeline.reference.loaders import load_backbone

    varieties: list[tuple] = []
    meta: dict[str, tuple] = {}
    seen: set = set()
    for c in CATEGORIES:
        path = c.backbone_path
        if not path.exists() or path in seen:
            continue
        seen.add(path)
        for vlist in load_backbone(path).by_norm_name.values():
            for v in vlist:
                varieties.append((v.variant, v.aliases, v.stone_type, v.colors))
                meta[proj.norm(v.variant)] = (v.stone_type, v.colors, v.aliases)
    cur = SETTINGS.curation
    resolver = AliasResolver.train(varieties, hi=cur.alias_model_hi, lo=cur.alias_model_lo)
    if resolver is not None:
        log.info("alias model trained", extra={"extra_fields": {
            "pos_pairs": resolver.n_pos, "neg_pairs": resolver.n_neg,
            "held_out_accuracy": round(resolver.accuracy, 3),
            "auto_coverage": round(resolver.coverage, 3), "hi": resolver.hi, "lo": resolver.lo}})
    else:
        log.info("alias model not trained (too little labelled data); using fuzzy floor")
    return resolver, meta
