"""Variant-image management CLI.

    python -m stone_pipeline.images

Drop the variant images you already have (old names) into
  images/inbox/slabs/   images/inbox/blocks/   images/inbox/tiles/
then run this. It renames each to images/renamed/{Key}.png and writes reports
(matched, unmatched, and what still needs generating) under images/reports/.

The pipeline owns image naming from here on: every variant's image is {Key}.png,
and new variants always come with their {Key}.png name to generate.
"""

from __future__ import annotations

import sys

from stone_pipeline.stages import image_intake


def main(argv: list[str] | None = None) -> int:
    res = image_intake.run()
    print("image intake complete")
    print(f"  renamed to {{Key}}.png : {res['renamed']}  -> {res['output_dir']}")
    print(f"  inputs seen          : {res['inputs']}")
    print(f"  unmatched (orphans)  : {res['unmatched']}  -> {res['reports_dir']}/unmatched_images.csv")
    print(f"  still to generate    : {res['to_generate']}  -> {res['reports_dir']}/images_to_generate.csv")
    print(f"  variants known       : {res['variants_total']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
