"""catalog_source/ports.csv is the file the operator imports into the backend (Settings > Ports, PHASE-0 runbook 4b.2).
The backend's importer refuses a row without a kind and never guesses one (BE Migration20261018200000.ts), so the
master states every row's kind, in the column order of the backend's own export (BE api/admin/ports/export/route.ts)."""

import csv
from pathlib import Path

MASTER = Path(__file__).resolve().parents[2] / "catalog_source" / "ports.csv"
# The backend's closed NodeKind set, held by its port_kind_check (BE Migration20261016120200.ts)
NODE_KINDS = {"sea", "rail", "air", "inland"}
# The backend export's header, so the master re-imports and round-trips without a column map
EXPORT_HEADER = ["id", "name", "un_locode", "country_iso", "latitude", "longitude", "region_name", "port_size", "kind"]


def _rows() -> tuple[list[str], list[dict]]:
    with MASTER.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


def test_master_header_is_the_backend_export_header():
    assert _rows()[0] == EXPORT_HEADER


def test_every_master_row_states_a_backend_node_kind():
    header, rows = _rows()
    assert rows, "the port master is empty"
    bad = [(r["id"], r.get("kind")) for r in rows if r.get("kind") not in NODE_KINDS]
    assert bad[:10] == [] and len(bad) == 0


def test_kind_and_un_locode_identify_one_master_row():
    # the backend's identity is (kind, un_locode) (BE IDX_port_kind_un_locode_unique); a duplicate refuses the import
    _, rows = _rows()
    keys = [(r["kind"], r["un_locode"]) for r in rows]
    assert len(keys) == len(set(keys))
