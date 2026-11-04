#!/bin/bash
# Build and push the photo standardiser's GPU image for this checkout's commit (deploy/standardise.Dockerfile) as
# standardise-pin-<sha12>, the tag the brand tfvars pin (dev_/prod_standardise_image_tag) and the ECR lifecycle keeps.
# Terraform registers the job definition on it; this script never does. Refuses a dirty tree (the tag must be the
# commit) and a tag that already exists (a release is immutable). Needs docker and AWS credentials for the registry.
set -euo pipefail
REGISTRY=026090543098.dkr.ecr.eu-west-1.amazonaws.com
REPO="$REGISTRY/blokport-scraper"
REGION=eu-west-1

cd "$(git rev-parse --show-toplevel)"
if [ -n "$(git status --porcelain)" ]; then
  echo "ERROR: the working tree is not clean; commit first (the tag names the commit)" >&2
  exit 1
fi
TAG="standardise-pin-$(git rev-parse --short=12 HEAD)"
if aws ecr describe-images --region "$REGION" --repository-name blokport-scraper --image-ids imageTag="$TAG" >/dev/null 2>&1; then
  echo "ERROR: $REPO:$TAG already exists; a pinned image is never rebuilt" >&2
  exit 1
fi
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$REGISTRY" >/dev/null
docker build --platform linux/amd64 -f deploy/standardise.Dockerfile -t "$REPO:$TAG" .
docker run --rm --platform linux/amd64 --entrypoint python "$REPO:$TAG" -c \
  "import deploy.standardise_retouch, deploy.standardise_source, deploy.standardise_close"   # the GPU run modes (run_pipeline.sh)
docker push "$REPO:$TAG" | tail -1
DIGEST=$(aws ecr describe-images --region "$REGION" --repository-name blokport-scraper --image-ids imageTag="$TAG" \
  --query 'imageDetails[0].imageDigest' --output text)
echo "==> pushed $REPO:$TAG ($DIGEST); pin it in infra/brands/<brand>.tfvars (dev_/prod_standardise_image_tag)"
