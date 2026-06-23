# syntax=docker/dockerfile:1
#
# Two build targets:
#   core       — scrape + pipeline + faithful image enhancement/upscale (CPU,
#                no torch). This is what the scheduled Fargate task runs.
#   imageproc  — core + the de-watermark stack (Florence-2 + LaMa). Only needed
#                when BLOKPORT_IMAGE_PROCESSING=true AND a source is watermarked.
#                Heavier image; build/push it separately when you enable that.
#
#   docker build --target core      -t blokport-scraper:core .
#   docker build --target imageproc -t blokport-scraper:imageproc .

FROM python:3.12-slim AS core

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Runtime libs: ca-certificates for TLS; libglib2.0-0 for opencv-python-headless.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Install deps first (layer cache) — both requirement files live in stone_pipeline/.
COPY stone_pipeline/requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

COPY . /app
RUN chmod +x /app/deploy/run_pipeline.sh

# Default run: scrape -> pipeline -> catalog -> push artifacts to S3 (staging).
ENTRYPOINT ["/app/deploy/run_pipeline.sh"]


# --- de-watermark variant (optional, CPU torch) -----------------------------
FROM core AS imageproc
COPY stone_pipeline/requirements-imageproc.txt /tmp/requirements-imageproc.txt
# --extra-index-url (NOT --index-url): keep PyPI as the primary index so deps like
# pillow/transformers resolve to their normal wheels, while torch/torchvision pull
# the CPU build from the pytorch index (far smaller than the default CUDA wheels).
RUN pip install --extra-index-url https://download.pytorch.org/whl/cpu \
    -r /tmp/requirements-imageproc.txt
