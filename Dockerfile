# syntax=docker/dockerfile:1
#
# Two build targets:
#   core       — scrape + pipeline + faithful image enhancement/upscale (CPU,
#                no torch). This is what the scheduled Fargate task runs.
#   imageproc  — core + the Real-ESRGAN enhance + CLIP classify stack on CPU torch
#                (fp32). Same local models as `gpu` but far slower; for local/CPU use.
#   gpu        — same stack on CUDA torch (fp16). What the on-demand AWS Batch GPU runs.
#                (De-watermarking on both is FAL FLUX Fill -- hosted, no local model.)
#
#   docker build --target core      -t blokport-scraper:core .
#   docker build --target imageproc -t blokport-scraper:imageproc .
#   docker build --target gpu       -t blokport-scraper:gpu .

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


# --- imageproc variant (optional, CPU torch) --------------------------------
# Same enhancement + classify stack as the GPU target, but CPU torch (fp32). Far slower
# than the GPU batch — kept for local runs / CPU-only environments. For a full catalogue
# use the `gpu` target. (De-watermarking is FAL FLUX Fill, hosted -- no torch, no bake.)
FROM core AS imageproc
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

# Bake the model weights, PINNED (ESRGAN by SHA-256; CLIP by immutable revision), so
# nothing is fetched/torch.load-ed from an unverified source on a live task.
RUN mkdir -p /app/models \
    && curl -fsSL -o /app/models/RealESRGAN_x4plus.pth \
       https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth \
    && echo "4fa0d38905f75ac06eb49a7951b426670021be3018265fd191d2125df9d682f1  /app/models/RealESRGAN_x4plus.pth" | sha256sum -c -
RUN python -c "from huggingface_hub import snapshot_download as d; \
    d('openai/clip-vit-base-patch32', revision='3d74acf9a28c67741b2f4f2ea7635f0aaf6f0268', ignore_patterns=['*.msgpack','*.h5','*.md'])"
ENV BLOKPORT_ESRGAN_WEIGHTS=/app/models/RealESRGAN_x4plus.pth


# --- GPU enhancement variant (CUDA torch + Real-ESRGAN + CLIP) ----------------
# For the on-demand GPU batch (AWS Batch). Real-ESRGAN (enhancement) + CLIP (classify) run on
# the GPU here; de-watermarking is FAL FLUX Fill (hosted). The CPU 'core' image keeps
# scrape+catalog. Pinned CUDA base + pinned weights so dev and prod produce identical output.
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
# first so nothing backtracks to an uncompilable old pillow), never re-touch torch.
COPY stone_pipeline/requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt \
    && pip install "pillow>=10" "spandrel==0.4.2" \
       "transformers==4.44.2" "safetensors==0.4.4" "fal-client>=0.4" "requests>=2.31" \
       "git+https://github.com/PramaLLC/BEN2.git@2c99a5da477b5523585bfa5c893888a6e818a8f6"
# ben2: BEN2 background-removal for VARIANT TEXTURES (rb_images.py), so RUN_MODE=generate-textures runs on
# THIS same GPU image. Additive -- the de-watermark/enhance paths are unchanged. Its model is fetched from
# HF at first use (rb_images.BEN_Base.from_pretrained); baking it pinned (like ESRGAN/CLIP below) is a follow-up.
# Bake the model weights at build, PINNED (ESRGAN by SHA-256; CLIP by immutable revision) so no
# unverified fetch happens on a live task and dev/prod are byte-identical:
#   - Real-ESRGAN x4plus       : the enhancement engine (local GPU)
#   - clip-vit-base-patch32    : the non-stone image classifier (local GPU)
# De-watermarking is FAL FLUX Fill (hosted) -- no local model to bake.
RUN mkdir -p /app/models \
    && curl -fsSL -o /app/models/RealESRGAN_x4plus.pth \
       https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth \
    && echo "4fa0d38905f75ac06eb49a7951b426670021be3018265fd191d2125df9d682f1  /app/models/RealESRGAN_x4plus.pth" | sha256sum -c -
RUN python -c "from huggingface_hub import snapshot_download as d; \
    d('openai/clip-vit-base-patch32', revision='3d74acf9a28c67741b2f4f2ea7635f0aaf6f0268', ignore_patterns=['*.msgpack','*.h5','*.md'])"
# Sensible enhancement defaults baked in (Batch job overrides SRC / BLOKPORT_ENV / bucket).
ENV BLOKPORT_ESRGAN_WEIGHTS=/app/models/RealESRGAN_x4plus.pth \
    BLOKPORT_IMAGE_PROCESSING=true
COPY . /app
RUN chmod +x /app/deploy/run_pipeline.sh
ENTRYPOINT ["/app/deploy/run_pipeline.sh"]
