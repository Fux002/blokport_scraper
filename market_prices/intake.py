"""The owner's competitor price sheets, absorbed into delivered runs: `python -m market_prices.intake`
(RUN_MODE=market-prices-intake on the ad-hoc task). No arguments: the brand is the required run input
SCRAPER_MARKET_PRICES_BRAND (market_prices/run_input.py), never the deployment's own brand; the environment, bucket and
S3 dry run come from settings, every tunable from market_prices/config.py, the layouts from
market_prices/sheet_layouts/<brand>.yaml.

Each file in `<env>/market_prices/<brand>/intake/` is read once, by its sha256:
- a file whose sha is in the brand's `intake_absorbed.csv` is skipped, under any name;
- a file no layout reads (or over a cap, or naming an unknown competitor) is refused, named, and left there, so it
  is read once a layout for it is committed;
- any other file becomes one delivered run, published like a crawl run (publish_run), with `source=file` rows. The
  run id is when S3 received the file (its LastModified): a fact, so a re-run of the same file gives the same run.
The record is written after the run is published, so a run that dies in between redoes that file into the same run
and the same bytes. The entry exits non-zero when any file was refused. Under the S3 dry run the run folders are
written locally and nothing is published or recorded.
"""

from __future__ import annotations

import collections
import csv
import hashlib
import io
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Optional
from urllib.parse import quote

from botocore.exceptions import ClientError
from pydantic import ValidationError

from market_prices import config
from market_prices.brand import output_key, require_brand_key
from market_prices.collect import RunResult, Stamp, counted_reasons, invalid_reason, stamp_for, write_run
from market_prices.layouts import Competitor, Layout, Layouts, cell_position, check_ids_apart, load_layouts, name_key
from market_prices.publish import publish_run
from market_prices.register import load_register
from market_prices.run_input import run_brand
from market_prices.schema import (
    DeliveryIncluded, MalformedListing, Observation, Provenance, SiteRunStatus, SiteStatus, Source, VatBasis,
)
from market_prices.sheet_files import FileRefused, Table, read_tables
from market_prices.sheet_values import cell_text, read_currency, read_price
from market_prices.specs import UNSTATED_NOTE, Countries, Specs, load_countries, read_specs
from market_prices.vocabulary import Vocabulary, load_vocabulary
from stone_pipeline.config.settings import ENV_NAME, S3_BUCKET, SETTINGS
from stone_pipeline.io.deliverables import MANIFEST_NAME, file_entry, published_manifest
from stone_pipeline.io.storage import BOTOCORE_DEFAULTS, aws_client

ABSORBED_COLUMNS = ("sha256", "key", "version_id", "layout_id", "run_id")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class IntakeError(Exception):
    """The intake's own state cannot be read: nothing is absorbed against an unknown record."""


@dataclass(frozen=True)
class IntakeFile:
    key: str
    version_id: str
    last_modified: datetime
    data: bytes


@dataclass(frozen=True)
class IntakeOutcome:
    absorbed: tuple[tuple[str, str], ...]               # (key, run_id)
    skipped: tuple[str, ...]                            # already absorbed, by sha
    refused: tuple[tuple[str, str], ...]                # (key, why)


@dataclass(frozen=True)
class _Matched:
    layout: Layout
    table: Table


# --- one file -> one run ---------------------------------------------------------------------------------------------
def absorb_file(file: IntakeFile, layouts: Layouts, vocabulary: Vocabulary, countries: Countries, bucket: str,
                brand: str) -> RunResult:
    """The file's rows and one status per competitor in it. Raises FileRefused when the whole file cannot be read."""
    return _absorb(file, layouts, vocabulary, countries, bucket, brand)[1]


def _absorb(file: IntakeFile, layouts: Layouts, vocabulary: Vocabulary, countries: Countries, bucket: str,
            brand: str) -> tuple[Layout, RunResult]:
    matched = _match_layout(file.key, read_tables(PurePosixPath(file.key).name, file.data), layouts)
    layout, rows = matched.layout, matched.table.rows
    stamp = stamp_for(file.last_modified)
    header = list(layout.signature)                     # the matched header row, as compared
    named = _file_competitor(file.key, layout, rows, layouts)
    kept: dict[str, dict[str, Observation]] = {}
    malformed: dict[str, int] = {}
    reasons: dict[str, collections.Counter] = {}
    unstated: dict[str, int] = {}
    duplicates: dict[str, int] = {}
    data_rows = 0
    for index in range(layout.header_row, len(rows)):
        cells = _trimmed(rows[index])
        if not any(cell_text(cell) for cell in cells):
            continue
        data_rows += 1
        competitor = named or _row_competitor(layout, header, cells, index, layouts)
        site_id = competitor.competitor_id
        kept.setdefault(site_id, {})
        try:
            row, specs = _observation(file, bucket, brand, layout, header, cells, index, matched.table, competitor,
                                      stamp, vocabulary, countries)
        except (MalformedListing, ValidationError) as exc:
            malformed[site_id] = malformed.get(site_id, 0) + 1
            why = str(exc) if isinstance(exc, MalformedListing) else invalid_reason(exc)
            reasons.setdefault(site_id, collections.Counter())[why] += 1
            continue
        unstated[site_id] = unstated.get(site_id, 0) + specs.unstated
        if row.listing_key in kept[site_id]:
            duplicates[site_id] = duplicates.get(site_id, 0) + 1
            continue
        kept[site_id][row.listing_key] = row
    if not data_rows:
        raise FileRefused(f"{file.key}: no data rows under the header (row {layout.header_row})")
    observations = tuple(row for site_id in sorted(kept) for _key, row in sorted(kept[site_id].items()))
    statuses = tuple(_status(site_id, brand, len(kept[site_id]), malformed.get(site_id, 0),
                             duplicates.get(site_id, 0), reasons.get(site_id, collections.Counter()),
                             unstated.get(site_id, 0), stamp) for site_id in sorted(kept))
    return layout, RunResult(brand=brand, run_id=stamp.run_id, observations=observations, statuses=statuses)


def _match_layout(key: str, tables: list[Table], layouts: Layouts) -> _Matched:
    """The one layout whose header row the file has exactly, at the declared row of the declared sheet."""
    found = [_Matched(layout, table) for layout in layouts.layouts for table in tables
             if _sheet_fits(layout, table) and len(table.rows) >= layout.header_row
             and tuple(name_key(cell_text(c) or "") for c in _trimmed(table.rows[layout.header_row - 1]))
             == layout.signature]
    if len(found) == 1:
        return found[0]
    if found:
        raise FileRefused(f"{key}: matches more than one layout: {sorted(m.layout.layout_id for m in found)}")
    seen = "; ".join(f"{table.name or 'csv'}: {_first_rows(table)}" for table in tables)
    raise FileRefused(f"{key}: no layout matches its header row (first rows: {seen})")


def _sheet_fits(layout: Layout, table: Table) -> bool:
    if layout.sheet is None or table.name is None:
        return layout.sheet is None and table.name is None
    return name_key(layout.sheet) == name_key(table.name)


def _first_rows(table: Table) -> list[list[str]]:
    """The table's first non-blank rows, as text: what a new layout for this file would be written from."""
    rows = ([cell_text(cell) or "" for cell in _trimmed(row)] for row in table.rows)
    return [row for row in rows if row][:config.INTAKE_REFUSAL_ROWS_SHOWN]


def _trimmed(row: list[object]) -> list[object]:
    """The row without its trailing empty cells, so a sheet's unused columns change nothing."""
    end = len(row)
    while end and cell_text(row[end - 1]) is None:
        end -= 1
    return row[:end]


def _file_competitor(key: str, layout: Layout, rows: list[list[object]], layouts: Layouts) -> Optional[Competitor]:
    """The competitor a cell or the file name names for the whole file; None when each row names its own."""
    source = layout.competitor
    if source.column is not None:
        return None
    if source.cell is not None:
        row, column = cell_position(source.cell)
        cells = rows[row] if row < len(rows) else []
        name = cell_text(cells[column]) if column < len(cells) else None
        where = f"cell {source.cell}"
    else:
        match = re.search(source.filename, PurePosixPath(key).name)
        name = match.group("name") if match else None
        where = "the file name"
    return _resolve(name, layouts, f"{key}: {where}")


def _row_competitor(layout: Layout, header: list[str], cells: list[object], index: int,
                    layouts: Layouts) -> Competitor:
    return _resolve(_cell(cells, header, layout.competitor.column), layouts, f"row {index + 1}")


def _resolve(name: Optional[str], layouts: Layouts, where: str) -> Competitor:
    competitor = layouts.competitor_named(name) if name else None
    if competitor is None:
        raise FileRefused(f"{where} names {name!r}, not a competitor the layouts declare")
    return competitor


def _cell(cells: list[object], header: list[str], column: Optional[str]) -> Optional[str]:
    return cell_text(_raw(cells, header, column))


def _raw(cells: list[object], header: list[str], column: Optional[str]) -> object:
    if column is None:
        return None
    position = header.index(name_key(column))
    return cells[position] if position < len(cells) else None


def _observation(file: IntakeFile, bucket: str, brand: str, layout: Layout, header: list[str], cells: list[object],
                 index: int, table: Table, competitor: Competitor, stamp: Stamp, vocabulary: Vocabulary,
                 countries: Countries) -> tuple[Observation, Specs]:
    """The one place a sheet row becomes an observation, with the specs read through the layout's `specs` (their
    unstated count is the caller's to report)."""
    if len(cells) > len(header):
        raise MalformedListing(f"row {index + 1} has cells past the last header")
    gaps = sorted(column for row, column in table.formula_gaps if row == index)
    if gaps:
        raise MalformedListing(f"row {index + 1} has a formula saved without a value")
    key_parts = [_cell(cells, header, column) for column in layout.listing_key]
    if not all(key_parts):
        raise MalformedListing(f"row {index + 1} has no listing key")
    text = " ".join(part for column in layout.listing_text if (part := _cell(cells, header, column)))
    price_column = layout.price_column(competitor.competitor_id)
    if price_column is None:                            # the whole file is refused, never half-read
        raise FileRefused(f"{file.key}: layout {layout.layout_id} gives competitor {competitor.competitor_id} "
                          f"no price column")
    price = read_price(_raw(cells, header, price_column), layout)
    column_currency = read_currency(_raw(cells, header, layout.currency), layout)
    if column_currency and price.currency and column_currency != price.currency:
        raise MalformedListing(f"row {index + 1}: currency {column_currency} but the price says {price.currency}")
    listed_currency = column_currency or price.currency
    listed_unit = _cell(cells, header, layout.unit)
    said = layout.statements
    stated = {layout.headers[position]: value for position, cell in enumerate(cells) if (value := cell_text(cell))}
    specs = read_specs(stated, layout.specs, vocabulary, countries)
    fields = {name_key(column) for column in (*layout.field_columns(competitor.competitor_id), *specs.used)}
    kept = {header_text: value for header_text, value in stated.items() if name_key(header_text) not in fields}
    grade, min_qty = _cell(cells, header, layout.grade), _cell(cells, header, layout.min_qty)
    row = Observation(
        contract_version=config.CONTRACT_VERSION, source=Source.file, brand=brand, site_id=competitor.competitor_id,
        seller_name=competitor.names[0], seller_type=competitor.seller_type, seller_country=competitor.seller_country,
        served_market=competitor.served_market, seller_is_our_supplier=competitor.supplier_key is not None,
        supplier_key=competitor.supplier_key, listing_key="|".join(key_parts),
        source_url=_source_url(bucket, file, table.name, index), listing_text=text, stated_specs=kept,
        **specs.columns(),
        grade_text=grade, price_text=price.text, price_amount=price.amount,
        currency=listed_currency or said.currency, price_status=price.status,
        vat_basis=said.vat_basis or VatBasis.unknown, unit_text=listed_unit or said.unit_text,
        delivery_text=said.delivery_text, delivery_included=said.delivery_included or DeliveryIncluded.unknown,
        min_qty_text=min_qty, availability_text=_cell(cells, header, layout.availability),
        provenance={
            "vat_basis": _said(said.vat_basis), "unit_text": _either(listed_unit, said.unit_text),
            "delivery_text": _said(said.delivery_text), "delivery_included": _said(said.delivery_included),
            "grade_text": _listed(grade), "price_amount": _listed(price.amount),
            "currency": _either(listed_currency, said.currency), "min_qty_text": _listed(min_qty),
            **specs.provenance(),
        },
        observed_at=stamp.observed_at, run_id=stamp.run_id,
        collector_version=f"{config.SHEET_COLLECTOR_VERSION}:{layout.layout_id}",
    )
    return row, specs


def _listed(value: object) -> Provenance:
    return Provenance.listing if value is not None else Provenance.unknown


def _said(value: object) -> Provenance:
    return Provenance.site_statement if value is not None else Provenance.unknown


def _either(listed: object, said: object) -> Provenance:
    return Provenance.listing if listed is not None else _said(said)


def _source_url(bucket: str, file: IntakeFile, sheet: Optional[str], index: int) -> str:
    """The exact object version and row the values came from."""
    place = f"{quote(sheet, safe='')}!R{index + 1}" if sheet is not None else f"R{index + 1}"
    return f"s3://{bucket}/{quote(file.key, safe='/')}?versionId={quote(file.version_id, safe='')}#{place}"


def _status(site_id: str, brand: str, rows: int, malformed: int, duplicates: int, reasons: collections.Counter,
            unstated: int, stamp: Stamp) -> SiteStatus:
    """One competitor's status: the refused rows named by their reasons, and the unstated words counted, as a site's."""
    why = f": {counted_reasons(reasons)}" if malformed else ""
    if not rows:
        status, detail = SiteRunStatus.unavailable_error, f"no readable rows ({malformed} malformed{why})"
    elif malformed:
        status, detail = SiteRunStatus.partial, f"{malformed} malformed rows skipped{why}"
    else:
        status, detail = SiteRunStatus.ok, ""
    detail = "; ".join(part for part in (detail, f"{UNSTATED_NOTE} x{unstated}" if unstated else "") if part)
    return SiteStatus(contract_version=config.CONTRACT_VERSION, source=Source.file, brand=brand, site_id=site_id,
                      status=status, rows=rows, malformed_listings=malformed, duplicate_listings=duplicates,
                      detail=detail, terms_checked_on=None, decision=config.FILE_DECISION,
                      observed_at=stamp.observed_at, run_id=stamp.run_id)


# --- the intake folder, the record and the publish --------------------------------------------------------------------
def run_intake(client, bucket: str, brand: str, layouts: Layouts, root: Path, dry_run: bool) -> IntakeOutcome:
    """Every intake file of the brand, in key order, each isolated: a refused file never stops the others."""
    vocabulary, countries = load_vocabulary(brand), load_countries()
    record_key = output_key(brand, config.ABSORBED_FILE)
    record = read_absorbed(client, bucket, record_key)
    seen = set(record)
    absorbed: list[tuple[str, str]] = []
    skipped: list[str] = []
    refused: list[tuple[str, str]] = []
    for key, size in _listed_files(client, bucket, output_key(brand, config.INTAKE_DIR_NAME) + "/"):
        require_brand_key(brand, key)       # brand_scoped_output: a listed key is read only inside the brand's prefix
        if size > config.MAX_INTAKE_FILE_BYTES:
            refused.append((key, f"{size} bytes is over the cap of {config.MAX_INTAKE_FILE_BYTES} bytes"))
            continue
        file = _fetch(client, bucket, key)
        sha = hashlib.sha256(file.data).hexdigest()
        if sha in seen:
            skipped.append(key)
            continue
        seen.add(sha)
        try:
            layout, result = _absorb(file, layouts, vocabulary, countries, bucket, brand)
        except FileRefused as exc:
            refused.append((key, str(exc)))
            continue
        run_dir = write_run(result, root)
        if not dry_run:
            if (held := _held_by_another_run(client, bucket, brand, result.run_id, run_dir)) is not None:
                refused.append((key, held))
                continue
            publish_run(client, bucket, brand, run_dir)
            record[sha] = {"sha256": sha, "key": key, "version_id": file.version_id, "layout_id": layout.layout_id,
                           "run_id": result.run_id}
            write_absorbed(client, bucket, record_key, record)
        absorbed.append((key, result.run_id))
    return IntakeOutcome(tuple(absorbed), tuple(skipped), tuple(refused))


def _listed_files(client, bucket: str, intake_prefix: str) -> list[tuple[str, int]]:
    """(key, size) of every file under the intake prefix, sorted by key; folder markers are not files."""
    files: list[tuple[str, int]] = []
    token: Optional[str] = None
    while True:
        page = client.list_objects_v2(Bucket=bucket, Prefix=intake_prefix,
                                      **({"ContinuationToken": token} if token else {}))
        files.extend((item["Key"], int(item["Size"])) for item in page.get("Contents", [])
                     if not item["Key"].endswith("/"))
        if not page.get("IsTruncated"):
            return sorted(files)
        token = page["NextContinuationToken"]


def _fetch(client, bucket: str, key: str) -> IntakeFile:
    answer = client.get_object(Bucket=bucket, Key=key)
    version = answer.get("VersionId")
    if not version or version == "null":
        raise IntakeError(f"s3://{bucket}/{key} has no version id: the record names each file's version, so the "
                          "bucket must have versioning enabled")
    return IntakeFile(key=key, version_id=version, last_modified=answer["LastModified"].astimezone(timezone.utc),
                      data=answer["Body"].read())


def _held_by_another_run(client, bucket: str, brand: str, run_id: str, run_dir: Path) -> Optional[str]:
    """Why this file's run folder cannot be published, or None. A folder already holding exactly these observations
    is this file's own run, published before its record was written."""
    held = published_manifest(client, bucket, output_key(brand, run_id, MANIFEST_NAME))
    if not held:
        return None
    theirs = held.get("files", {}).get(config.OBSERVATIONS_FILE, {}).get("sha256")
    if theirs == file_entry(run_dir / config.OBSERVATIONS_FILE)["sha256"]:
        return None
    return f"run folder {run_id} already holds another run; the file is left for the next run"


def read_absorbed(client, bucket: str, key: str) -> dict[str, dict[str, str]]:
    """The record by sha256; empty before the first file. A record that does not read stops the intake."""
    try:
        body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return {}
        raise
    try:
        reader = csv.DictReader(io.StringIO(body.decode("utf-8")))
        rows = list(reader)
    except (UnicodeDecodeError, csv.Error) as exc:
        raise IntakeError(f"{config.ABSORBED_FILE} does not read: {exc}") from exc
    if tuple(reader.fieldnames or ()) != ABSORBED_COLUMNS:
        raise IntakeError(f"{config.ABSORBED_FILE} has columns {reader.fieldnames}, not {list(ABSORBED_COLUMNS)}")
    record: dict[str, dict[str, str]] = {}
    for row in rows:
        if not _SHA256.fullmatch(row["sha256"] or "") or row["sha256"] in record:
            raise IntakeError(f"{config.ABSORBED_FILE}: a bad or repeated sha256 {row['sha256']!r}")
        record[row["sha256"]] = row
    return record


def write_absorbed(client, bucket: str, key: str, record: dict[str, dict[str, str]]) -> None:
    """The whole record, sorted by sha256: the same files give the same bytes."""
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=ABSORBED_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(record[sha] for sha in sorted(record))
    client.put_object(Bucket=bucket, Key=key, Body=out.getvalue().encode("utf-8"), ContentType="text/csv")


# --- the entry -------------------------------------------------------------------------------------------------------
def summary(outcome: IntakeOutcome, dry_run: bool) -> str:
    lines = [f"absorbed: {key} -> run {run_id}" for key, run_id in outcome.absorbed]
    lines += [f"skipped (already absorbed): {key}" for key in outcome.skipped]
    lines += [f"refused: {key}: {why}" for key, why in outcome.refused]
    lines.append("published: no (S3 dry run)" if dry_run else "published: yes")
    return "\n".join(lines)


def _dry_run() -> bool:
    return SETTINGS.s3.dry_run


def _s3_client():
    return aws_client("s3", config=BOTOCORE_DEFAULTS)


def _runs_root() -> Path:
    return config.RUNS_DIR / ENV_NAME / config.INTAKE_RUNS_DIR_NAME


def main() -> None:
    brand = run_brand()
    layouts = load_layouts(brand)
    check_ids_apart(layouts, load_register(brand))
    dry_run = _dry_run()
    outcome = run_intake(_s3_client(), S3_BUCKET, brand, layouts, _runs_root(), dry_run)
    print(summary(outcome, dry_run))
    if outcome.refused:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
