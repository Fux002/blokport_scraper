"""An operator's manual fetch of this environment's Medusa inputs from S3 into from_medusa/ (DEPLOY.md); the produce
runs the same flow itself. The flow is stone_pipeline.io.medusa_inputs.fetch_all."""

from __future__ import annotations

import sys

from stone_pipeline.io import medusa_inputs


def main() -> int:
    return medusa_inputs.fetch_all()


if __name__ == "__main__":
    sys.exit(main())
