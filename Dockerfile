# syntax=docker/dockerfile:1
#
# Two build targets:
#   core       — scrape + pipeline + faithful image enhancement/upscale (CPU,
#                no torch). This is what the scheduled Fargate task runs.
#   imageproc  — core + the de-watermark stack (colour-locate + LaMa inpaint, CPU
#                torch). Only needed when BLOKPORT_IMAGE_PROCESSING=true AND a source
#                is watermarked. Heavier image; build/push it separately to enable.
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

# Patch build tooling first: setuptools < 78.1.1 is vulnerable to PYSEC-2025-49.
RUN pip install --upgrade "pip" "setuptools>=78.1.1"

# Install deps first (layer cache) — both requirement files live in stone_pipeline/.
COPY stone_pipeline/requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

COPY . /app
RUN chmod +x /app/deploy/run_pipeline.sh

# Default run: scrape -> pipeline -> catalog -> push artifacts to S3 (staging).
ENTRYPOINT ["/app/deploy/run_pipeline.sh"]


# --- de-watermark variant (optional, CPU torch) -----------------------------
FROM core AS imageproc
# Build tooling for any sdist-only dep, plus libgl1/libglib2.0-0: the LaMa stack
# (simple-lama-inpainting) pulls in the FULL opencv-python (not headless), which
# links libGL.so.1 — absent in the slim base, so cv2 import fails without it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       build-essential libjpeg-dev zlib1g-dev libgl1 libglib2.0-0 curl \
    && rm -rf /var/lib/apt/lists/*
COPY stone_pipeline/requirements-imageproc.txt /tmp/requirements-imageproc.txt
# --extra-index-url (NOT --index-url): keep PyPI as the primary index so pillow etc.
# resolve normally, while torch/torchvision pull the CPU build from the pytorch index
# (far smaller than the default CUDA wheels).
RUN pip install --extra-index-url https://download.pytorch.org/whl/cpu \
    -r /tmp/requirements-imageproc.txt

# Bake the LaMa weights at build time with a pinned SHA-256, into the torch-hub cache
# where simple-lama looks. This closes the runtime supply-chain hole: the weights are
# never fetched-then-torch.load-ed (pickle) from an unverified source on a live task.
RUN mkdir -p /root/.cache/torch/hub/checkpoints \
    && curl -fsSL -o /root/.cache/torch/hub/checkpoints/big-lama.pt \
       https://github.com/enesmsahin/simple-lama-inpainting/releases/download/v0.1.0/big-lama.pt \
    && echo "7ba7aa7ac37a4d41fdbbeba3a2af7ead18058552997e3a3cd1a3b2210c9e6b4c  /root/.cache/torch/hub/checkpoints/big-lama.pt" \
       | sha256sum -c -


# --- GPU enhancement variant (CUDA torch + Real-ESRGAN + LaMa) ----------------
# For the on-demand GPU batch (AWS Batch). Real-ESRGAN (the enhancement engine) + LaMa
# run on the GPU here; the CPU 'core' image keeps scrape+catalog. Pinned CUDA base + pinned
# model weights so dev and prod produce identical output. Built/pushed separately.
#   docker build --target gpu -t blokport-scraper:gpu .
FROM pytorch/pytorch:2.4.1-cuda12.1-cudnn9-runtime AS gpu
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates libgl1 libglib2.0-0 curl build-essential libjpeg-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --upgrade "pip" "setuptools>=78.1.1"
# torch/torchvision are ALREADY in the CUDA base — install ONLY the app deps (pillow>=10
# first so simple-lama doesn't backtrack to an uncompilable old pillow), never re-touch torch.
COPY stone_pipeline/requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt \
    && pip install "pillow>=10" "spandrel==0.4.2" simple-lama-inpainting
# Bake both model weights with pinned SHA-256 (no unverified fetch/torch.load on a live task).
RUN mkdir -p /app/models /root/.cache/torch/hub/checkpoints \
    && curl -fsSL -o /app/models/RealESRGAN_x4plus.pth \
       https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth \
    && echo "4fa0d38905f75ac06eb49a7951b426670021be3018265fd191d2125df9d682f1  /app/models/RealESRGAN_x4plus.pth" | sha256sum -c - \
    && curl -fsSL -o /root/.cache/torch/hub/checkpoints/big-lama.pt \
       https://github.com/enesmsahin/simple-lama-inpainting/releases/download/v0.1.0/big-lama.pt \
    && echo "7ba7aa7ac37a4d41fdbbeba3a2af7ead18058552997e3a3cd1a3b2210c9e6b4c  /root/.cache/torch/hub/checkpoints/big-lama.pt" | sha256sum -c -
# Sensible enhancement defaults baked in (Batch job overrides SRC / BLOKPORT_ENV / bucket).
ENV BLOKPORT_ESRGAN_WEIGHTS=/app/models/RealESRGAN_x4plus.pth \
    BLOKPORT_IMAGE_ENGINE=esrgan \
    BLOKPORT_IMAGE_PROCESSING=true
COPY . /app
RUN chmod +x /app/deploy/run_pipeline.sh
ENTRYPOINT ["/app/deploy/run_pipeline.sh"]
