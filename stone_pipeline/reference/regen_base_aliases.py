"""One-off (P2-10 C4): both committed bases' Aliases written as C4's own emit projects them, so the seed stays a fixed
point (seed.verify: the base equals emit's hermetic projection of it). Under C4 emit derives a variety's aliases from
the alias stores (seed_only: the backbone posts), never from the base's Aliases column; after the seed rewrite (SC #649)
moved every base alias onto its Key's post, this writes each base's Aliases from those posts.

Run once from the repo root, after SC #649 is merged in, committed together with its output:

    python -m stone_pipeline.reference.regen_base_aliases

Each base is rewritten in its own byte form (line ending, final newline), so only Aliases cells differ. It refuses, and
writes NOTHING, when the projection would change anything but Aliases (a row added, removed or moved, or a Key, Name
or Type changed: not this tool's change to make) or when a second pass would change anything. A second run changes 0
bytes."""
from __future__ import annotations

import csv
import io
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASES = ["from_medusa/development/variants_export_base.csv", "from_medusa/production/variants_export_base.csv"]
KEPT = ("Key", "Name", "Type")


class Refused(RuntimeError):
    """The projection would change more than Aliases, or is not a fixed point: nothing is written."""


def _read_csv(text: str) -> tuple[list[str], list[dict]]:
    reader = csv.DictReader(io.StringIO(text, newline=""))
    return list(reader.fieldnames or []), list(reader)


def _write_csv(header: list[str], rows: list[dict], like: str) -> str:
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, fieldnames=header, lineterminator="\r\n" if "\r\n" in like else "\n")
    writer.writeheader()
    writer.writerows(rows)
    out = buf.getvalue()
    return out if like.endswith("\n") else out.rstrip("\r\n")


def project(text: str) -> list[dict]:
    """emit's hermetic projection (seed_only: the base and the backbone, nothing else) of a base's text."""
    from stone_pipeline.stages import emit_catalog
    with tempfile.TemporaryDirectory() as td:
        base, out = Path(td) / "variants_export_base.csv", Path(td) / "1_variants_full.csv"
        base.write_bytes(text.encode("utf-8"))
        emit_catalog.build(base, seed_only=True, out_path=out)
        return _read_csv(out.read_bytes().decode("utf-8"))[1]


def regenerate(text: str, full: list[dict]) -> str:
    """The base's text with each row's Aliases taken from `full`, its projection; Refused when the projection differs
    in anything but Aliases."""
    header, rows = _read_csv(text)
    if len(full) != len(rows):
        raise Refused(f"the projection has {len(full)} rows, the base {len(rows)}")
    for line, (row, new) in enumerate(zip(rows, full), 2):
        moved = [(c, row[c], new[c]) for c in KEPT if row[c] != new[c]]
        if moved:
            raise Refused(f"line {line}: the projection changes {moved}")
        row["Aliases"] = new["Aliases"]
    return _write_csv(header, rows, text)


def plan() -> dict[str, str]:
    """{path: new text} for every base whose text changes; raises Refused, writing nothing."""
    # bytes decoded as they are: read_text would translate CRLF to LF and every CRLF file would change
    texts = {p: (ROOT / p).read_bytes().decode("utf-8") for p in BASES}
    out = {p: regenerate(t, project(t)) for p, t in texts.items()}
    unstable = [p for p, t in out.items() if regenerate(t, project(t)) != t]
    if unstable:
        raise Refused(f"not a fixed point after one pass: {unstable}")
    return {p: t for p, t in out.items() if t != texts[p]}


def main() -> int:
    changes = plan()
    for path, text in changes.items():
        (ROOT / path).write_bytes(text.encode("utf-8"))
    print(f"regenerated the Aliases of {len(changes)} base(s): {sorted(changes)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
