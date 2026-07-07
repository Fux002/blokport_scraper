# Image enhancement → Real-ESRGAN on on-demand GPU (dev + prod) — PLAN

Replace the classical enhance chain (which distorts colour + texture) with Real-ESRGAN,
run on an **on-demand GPU that scales to zero**, built **env-parametric** so prod is a
`terraform apply`, not a rebuild. No constant GPU. Nothing is built until this is approved.

## 0. What we already proved (risks retired)
- **GPU quota:** On-Demand G/VT = **384 vCPU** (works now, no support ticket). Spot = 0 (optional request later for ~40% off). g4dn.xlarge available in all eu-west-1 AZs; on-demand ~$0.53/hr.
- **Recipe:** validated on real slabs — brighter, crisp, natural, colour preserved. Locked below.
- **Cost:** full catalogue (~2,765 imgs, ~45 min on a T4) ≈ **$0.40 on-demand**; incremental (new imgs per scrape) ≈ cents. Idle = **$0**.

## 1. The locked recipe (per image)
```
[varsha only] de-watermark (existing LaMa ink-mask, on the original)   # de-wm is a SEPARATE track
  → Real-ESRGAN x4plus  (learned clean + sharpen + 4x, colour untouched)
  → cap long edge 2048  (INTER_AREA downscale of the 4x output)
  → levels / exposure lift  (stretch to full tonal range — brightens dull hangar shots)
  → vibrance 0.20  (un-mute colour bad light lost; boosts low-sat more, invents nothing)
  → JPEG q90
DROP: gray-world WB, CLAHE, NLM denoise, unsharp  (these caused the distortion)
```
Model: `RealESRGAN_x4plus`. Alternatives to trial for sharper stone: `4x-UltraSharp`, `4x-NMKD-Siax`.

## 2. Architecture (three env-parametric pieces)
1. **Enhancement code** — new recipe in `stone_pipeline/io/image_processing.py`, config-driven
   (`ImageProcessingConfig`: engine=esrgan, target 2048, vibrance/levels knobs). Same code dev/prod.
2. **GPU container** — a new ECR image target (`:gpu`) = CUDA base + torch(CUDA) + spandrel + the
   ESRGAN weights **baked and checksum-pinned** (like `big-lama.pt`). One image, both envs pull it.
3. **On-demand GPU compute** — **AWS Batch** managed compute env, **min vCPU = 0** (idle = $0),
   On-Demand `g4dn.xlarge`, GPU-optimised AMI, `resourceRequirements GPU=1`. A job reads
   `<env>/products/scraped/ → improved/` (the existing `reprocess_source` pattern), then the
   instance terminates. All defined as a **Terraform module** in `infra/`, instantiated per env.

Parity: enhanced photos are **content-keyed** (`<sha-of-original>.jpg`), same suppliers → same
originals → same keys; the **pinned model** → byte-identical output in both envs, only the bucket
differs. Promotion = `terraform apply` with prod vars + the promoted image tag.

## 3. Phased rollout — each phase VALIDATED before the next (so no surprises)
- **A. Recipe code** — implement the ESRGAN recipe behind config; unit-test on CPU with a tiny model
  stub; run locally (MPS) on ~20 slabs → eyeball. *Gate: quality signed off.*
- **B. GPU container** — build the CUDA image; **run it on ONE manually-launched g4dn instance** to
  prove torch-CUDA + spandrel + pinned weights all work on the real GPU (this is where CUDA/torch
  mismatches surface — caught here, not in Batch). *Gate: one image enhanced end-to-end on GPU.*
- **C. AWS Batch (Terraform)** — the compute env + queue + job def + IAM; submit **one** job.
  *Gate: a single Batch job enhances a slice scraped→improved and the instance scales back to 0.*
- **D. Clean-slate fresh run** — **decided: forget the existing images.** When A–C are green:
  (1) **delete all legacy** `improved/` + `scraped/` (+ their manifest entries) for every source,
  (2) run the pipeline **from scratch** — scrape → GPU-enhance → stage — so every image is produced
  by the new recipe. No migration of old images. Verify counts, sizes, montage. *Gate: dev looks right.*
- **E. Prod** — instantiate the same TF module with prod vars + promoted image tag; clean-slate run there too.

## 4. Decisions (resolved)
1. **GPU compute → AWS Batch on-demand** (managed, scales to 0, Terraform module, dev/prod-clean). ✓
2. **No re-process of existing images → clean slate.** Build the setup, then run fresh from the
   scraper and delete all legacy processed images. ✓
3. **Output cap:** 2048 long edge (default; tunable).
4. **Spot quota:** stay On-Demand for now (quota is there); optionally request Spot later for ~40% off.
5. **De-watermark:** keep the existing LaMa ink-mask for now (separate track); estimate-and-subtract is later.

## 5. Risks & mitigations
- **CUDA/torch build churn** (the LaMa-style trap): use a pinned, known-good `pytorch/pytorch:*-cuda*`
  base + pinned torch/spandrel; validate on a real GPU in Phase B before any Batch wiring.
- **Big container** (~6–8 GB): torch-CUDA image; acceptable, pull cached on the Batch AMI.
- **Spot interruptions:** we're On-Demand initially (Spot quota=0), so no interruption risk.
- **Cost creep:** Batch min=0 guarantees $0 idle; jobs are minutes.
- **Compute placement:** enhancement moves OFF the CPU Fargate pipeline task onto the GPU batch;
  the Fargate task keeps scrape+catalog (fast, cheap). Clean separation.
