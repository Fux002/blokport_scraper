"""An operator's manual upload of this environment's deliverables (to_upload/, the manifest last) and review files to
S3 (DEPLOY.md); the produce runs the same flow itself. The flow is stone_pipeline.io.deliverables.upload_all."""

from __future__ import annotations

import sys

from stone_pipeline.io import deliverables


def main() -> int:
    return deliverables.upload_all(None)


if __name__ == "__main__":
    sys.exit(main())
