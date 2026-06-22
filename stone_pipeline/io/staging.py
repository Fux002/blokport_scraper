"""Canonical parquet read and write (section 12 io/staging.py, section 13A.1).

The canonical table is the checkpoint between stages. Scalar fields land as
native polars columns so the deterministic stages can vectorize over them; the
nested list and object fields (flags, gaps, image key lists) are JSON-encoded
into string columns so the round-trip is lossless. CanonicalRow is the in-memory
shape; parquet is the on-disk shape.
"""

from __future__ import annotations

import json
from pathlib import Path

import polars as pl

from stone_pipeline.core.schema import CanonicalRow

# Fields whose values are lists or nested models. Stored as JSON strings on disk.
JSON_FIELDS: frozenset[str] = frozenset(
    {
        "raw_image_urls",
        "port_ids",
        "image_keys",
        "oriented_image_keys",
        "product_image_keys",
        "review_flags",
        "reject_reasons",
        "tree_gaps",
    }
)


def rows_to_records(rows: list[CanonicalRow]) -> list[dict]:
    records: list[dict] = []
    for row in rows:
        data = row.model_dump(mode="json")
        flat: dict = {}
        for key, value in data.items():
            if key in JSON_FIELDS:
                flat[key] = json.dumps(value, ensure_ascii=False)
            else:
                flat[key] = value
        records.append(flat)
    return records


def rows_to_frame(rows: list[CanonicalRow]) -> pl.DataFrame:
    if not rows:
        # Build an empty frame with the right columns from the model fields.
        columns = list(CanonicalRow.model_fields.keys())
        return pl.DataFrame({c: [] for c in columns})
    # infer_schema_length=None scans every row, so a column that is null for the
    # first many rows (e.g. variation_id on a run of gaps) then a string later
    # still gets the right dtype instead of failing schema inference.
    return pl.DataFrame(rows_to_records(rows), strict=False, infer_schema_length=None)


def write_canonical(rows: list[CanonicalRow], path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = rows_to_frame(rows)
    frame.write_parquet(path)
    return path


def frame_to_rows(frame: pl.DataFrame) -> list[CanonicalRow]:
    rows: list[CanonicalRow] = []
    for record in frame.iter_rows(named=True):
        data = dict(record)
        for key in JSON_FIELDS:
            if key in data and isinstance(data[key], str):
                data[key] = json.loads(data[key])
        # Drop None values that are not part of the model defaults cleanly.
        rows.append(CanonicalRow.model_validate(data))
    return rows


def read_canonical(path: Path) -> list[CanonicalRow]:
    frame = pl.read_parquet(Path(path))
    return frame_to_rows(frame)
