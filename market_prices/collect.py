"""One run for one brand: every site in the register, each isolated, written as two CSVs."""

from __future__ import annotations

import collections
import csv
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from pydantic import BaseModel, ValidationError

from market_prices import config
from market_prices.brand import require_run_brand
from market_prices.collectors import COLLECTORS
from market_prices.fetching import (
    SiteBlocked, SiteDisallowed, SiteFetcher, Transport, TransportError, UnexpectedResponse,
)
from market_prices.register import Register, Site
from market_prices.schema import (
    DeliveryIncluded, ListingFacts, MalformedListing, Observation, Provenance, SiteRunStatus, SiteStatus, Source,
    VatBasis, columns, csv_values,
)
from market_prices.specs import UNSTATED_NOTE, Countries, Specs, load_countries, read_specs
from market_prices.vocabulary import Vocabulary, VocabularyError, load_vocabulary

_SITE_STATEMENT_FIELDS = {"vat_basis": VatBasis.unknown, "unit_text": None, "delivery_text": None,
                          "delivery_included": DeliveryIncluded.unknown}
_LISTING_FIELDS = ("grade_text", "price_amount", "currency", "min_qty_text")
# A statement a listing may make for itself; it wins over the site's, being the more specific (manager's ruling 13 Nov)
_LISTING_STATEMENT_FIELDS = ("vat_basis",)
_VAT_CONFLICT = "listing VAT differs from the site statement"


@dataclass(frozen=True)
class Stamp:
    run_id: str
    observed_at: str


@dataclass(frozen=True)
class SiteResult:
    observations: tuple[Observation, ...]
    status: SiteStatus


@dataclass(frozen=True)
class RunResult:
    brand: str
    run_id: str
    observations: tuple[Observation, ...]
    statuses: tuple[SiteStatus, ...]


def stamp_for(moment: datetime) -> Stamp:
    """The run id sorts by time, so a late publish of an older run can be recognised."""
    if moment.tzinfo is None:
        raise ValueError("the run moment must be timezone-aware")
    utc = moment.astimezone(timezone.utc)
    return Stamp(run_id=utc.strftime("%Y%m%dT%H%M%SZ"), observed_at=utc.strftime("%Y-%m-%dT%H:%M:%SZ"))


def _observation(facts: ListingFacts, site: Site, brand: str, stamp: Stamp, version: str, specs: Specs) -> Observation:
    """The one place a listing's facts meet the site's identity and statements, and its specs (read_specs)."""
    stated: dict[str, object] = {}
    provenance: dict[str, Provenance] = {}
    for name, unknown in _SITE_STATEMENT_FIELDS.items():
        own = getattr(facts, name) if name in _LISTING_STATEMENT_FIELDS else None
        general = getattr(site.statements, name)
        if own is not None:
            stated[name], provenance[name] = own, Provenance.listing
        elif general is not None:
            stated[name], provenance[name] = general, Provenance.site_statement
        else:
            stated[name], provenance[name] = unknown, Provenance.unknown
    for name in _LISTING_FIELDS:
        provenance[name] = Provenance.listing if getattr(facts, name) is not None else Provenance.unknown
    provenance.update(specs.provenance())
    return Observation(
        contract_version=config.CONTRACT_VERSION, source=Source.site, brand=brand, site_id=site.site_id,
        seller_name=site.seller_name, seller_type=site.seller_type, seller_country=site.seller_country,
        served_market=site.served_market, seller_is_our_supplier=site.supplier_key is not None,
        supplier_key=site.supplier_key,
        listing_key=facts.listing_key, source_url=facts.source_url, listing_text=facts.listing_text,
        stated_specs={label: value for label, value in facts.stated_specs.items() if label not in specs.used},
        **specs.columns(), grade_text=facts.grade_text, price_text=facts.price_text,
        price_amount=facts.price_amount, currency=facts.currency, price_status=facts.price_status,
        min_qty_text=facts.min_qty_text, availability_text=facts.availability_text, provenance=provenance,
        observed_at=stamp.observed_at, run_id=stamp.run_id, collector_version=version, **stated,
    )


def _vat_conflict(facts: ListingFacts, site: Site) -> bool:
    stated = site.statements.vat_basis
    return facts.vat_basis is not None and stated is not None and facts.vat_basis != stated


def invalid_reason(exc: ValidationError) -> str:
    """A row that does not validate, named by its first field and not its value (the value is the seller's)."""
    first = exc.errors()[0]
    return f"{'.'.join(str(part) for part in first['loc']) or 'row'} does not validate"


def counted_reasons(reasons: collections.Counter) -> str:
    """The most common reasons with their counts, the rest summed: '<reason> x<n>; ...; other reasons x<n>'."""
    ranked = sorted(reasons.items(), key=lambda item: (-item[1], item[0]))
    shown = [f"{reason} x{count}" for reason, count in ranked[:config.MALFORMED_REASONS_SHOWN]]
    rest = sum(count for _, count in ranked[config.MALFORMED_REASONS_SHOWN:])
    return "; ".join(shown + ([f"other reasons x{rest}"] if rest else []))


def collect_site(site: Site, brand: str, vocabulary: Vocabulary, countries: Countries, transport: Transport,
                 stamp: Stamp, pause: Callable[[float], None]) -> SiteResult:
    """One site, isolated. A listing is kept only when the run brand's vocabulary has it in scope; one out of scope
    is counted in `detail`, never dropped silently. Scope is decided first, on the collector's listing_text, so a
    listing out of scope is never read further and malformed counts in-scope listings only. An in-scope listing's
    specs are read through the site's spec_labels."""
    malformed = duplicates = vat_conflicts = out_of_scope = 0
    reasons: collections.Counter = collections.Counter()
    notes: collections.Counter = collections.Counter()

    def finish(status: SiteRunStatus, detail: str, kept: Iterable[Observation] = ()) -> SiteResult:
        ordered = tuple(sorted(kept, key=lambda row: row.listing_key))
        parts = [detail] if detail else []
        if out_of_scope:
            parts.append(f"listings out of scope x{out_of_scope}")
        if vat_conflicts:
            parts.append(f"{_VAT_CONFLICT} x{vat_conflicts}")
        parts.extend(f"{note} x{count}" for note, count in sorted(notes.items()))
        return SiteResult(ordered, SiteStatus(
            contract_version=config.CONTRACT_VERSION, source=Source.site, brand=brand, site_id=site.site_id,
            status=status, rows=len(ordered), malformed_listings=malformed, duplicate_listings=duplicates,
            detail="; ".join(parts),
            terms_checked_on=site.terms.checked_on.isoformat() if site.terms.checked_on else None,
            decision=site.decision.value, observed_at=stamp.observed_at, run_id=stamp.run_id))

    if not site.collectable:
        return finish(SiteRunStatus.disabled_terms, f"decision is {site.decision.value}")
    collector = COLLECTORS[site.platform]
    rows: dict[str, Observation] = {}
    try:
        fetcher = SiteFetcher(site.robots_url, transport, pause)
        for raw in collector.listings(site, fetcher):
            try:
                if not vocabulary.in_scope(collector.listing_text(raw, site), site.served_market):
                    out_of_scope += 1
                    continue
                facts = collector.facts(raw, site)
                specs = read_specs(facts.stated_specs, site.spec_labels, vocabulary, countries)
                row = _observation(facts, site, brand, stamp, collector.version, specs)
            except MalformedListing as exc:
                malformed += 1
                reasons[str(exc)] += 1
                continue
            except ValidationError as exc:
                malformed += 1
                reasons[invalid_reason(exc)] += 1
                continue
            if row.listing_key in rows:
                duplicates += 1     # a listing that moved between pages while they were read
                continue
            rows[row.listing_key] = row
            vat_conflicts += _vat_conflict(facts, site)
            notes.update(facts.notes)
            if specs.unstated:
                notes[UNSTATED_NOTE] += specs.unstated
    except SiteBlocked as exc:
        return finish(SiteRunStatus.unavailable_blocked, str(exc))
    except SiteDisallowed as exc:
        return finish(SiteRunStatus.disallowed_robots, str(exc))
    except (TransportError, UnexpectedResponse) as exc:
        skipped = f"; {malformed} malformed listings skipped: {counted_reasons(reasons)}" if malformed else ""
        if rows:
            return finish(SiteRunStatus.partial, f"stopped early: {exc}{skipped}", rows.values())
        return finish(SiteRunStatus.unavailable_error, f"{exc}{skipped}")
    # a site read whole with every listing out of scope is ok with no rows; nothing readable at all is an error
    if not rows and (malformed or not out_of_scope):
        why = f": {counted_reasons(reasons)}" if malformed else ""
        return finish(SiteRunStatus.unavailable_error, f"no readable listings ({malformed} malformed{why})")
    if malformed:
        return finish(SiteRunStatus.partial, f"{malformed} malformed listings skipped: {counted_reasons(reasons)}",
                      rows.values())
    return finish(SiteRunStatus.ok, "", rows.values())


def collect_run(register: Register, transport: Transport, moment: datetime,
                pause: Callable[[float], None] = time.sleep) -> RunResult:
    stamp = stamp_for(moment)
    vocabulary = load_vocabulary(register.brand)
    countries = load_countries()
    if uncovered := sorted(site.site_id for site in register.sites
                           if site.collectable and not vocabulary.covers(site.served_market)):
        raise VocabularyError(f"{register.brand}'s scope vocabulary declares no market for sites {uncovered}")
    results = [collect_site(site, register.brand, vocabulary, countries, transport, stamp, pause)
               for site in sorted(register.sites, key=lambda site: site.site_id)]
    return RunResult(brand=register.brand, run_id=stamp.run_id,
                     observations=tuple(row for result in results for row in result.observations),
                     statuses=tuple(result.status for result in results))


def _write_csv(path: Path, model: type[BaseModel], rows: Iterable[BaseModel]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(columns(model))
        writer.writerows(csv_values(row) for row in rows)


def write_run(result: RunResult, root: Path) -> Path:
    """The run's folder under `root`. Re-writing the same result gives byte-identical files. Every row must be the
    run's brand (brand_required), checked before anything is written."""
    require_run_brand(result.brand, result.observations, "observations")
    require_run_brand(result.brand, result.statuses, "site_status rows")
    run_dir = root / result.brand / result.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(run_dir / config.OBSERVATIONS_FILE, Observation, result.observations)
    _write_csv(run_dir / config.SITE_STATUS_FILE, SiteStatus, result.statuses)
    return run_dir
