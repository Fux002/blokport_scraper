"""Valid-combinations CLI: build the relational valid-combination rows and write the
single upload artifact to to_upload/2_valid_combinations.csv.

    python -m stone_pipeline.tree            # writes to_upload/2_valid_combinations.csv

Run this after the variant files are loaded into Medusa and the variant export is
refreshed (from_medusa/variants_export.csv), so every new variant resolves to a sourceId.
"""

from __future__ import annotations

from stone_pipeline.stages import tree_build


def main(argv: list[str] | None = None) -> int:
    path = tree_build.run()
    print(f"valid combinations ready to upload: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
