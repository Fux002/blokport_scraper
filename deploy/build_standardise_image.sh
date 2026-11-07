#!/bin/bash
# Build and push the photo standardiser's GPU image for the commit HEAD names (deploy/standardise.Dockerfile) as
# standardise-pin-<sha12>, the tag the brand tfvars pin (dev_/prod_standardise_image_tag) and the ECR lifecycle keeps.
# Terraform registers the job definition on it; this script never does. The build context is the commit's own tree
# (git archive), never the working files, so the tag names exactly what the image holds. Refuses a tag that already
# exists (a release is immutable) and a commit whose package requirements the base does not meet (a new base is due).
# Needs docker and AWS credentials for the registry.
set -euo pipefail
REGISTRY=026090543098.dkr.ecr.eu-west-1.amazonaws.com
REPO="$REGISTRY/blokport-scraper"
REGION=eu-west-1
REQUIREMENTS=(stone_pipeline/requirements.txt stone_pipeline/requirements-standardise.txt)

cd "$(git rev-parse --show-toplevel)"
TAG="standardise-pin-$(git rev-parse --short=12 HEAD)"
# Only ImageNotFound means the tag is free: any other failure (access, throttling, network) must not read as "absent",
# or a push would replace a pinned image under the same tag (the repository's tags are mutable).
if found=$(aws ecr describe-images --region "$REGION" --repository-name blokport-scraper --image-ids imageTag="$TAG" 2>&1); then
  echo "ERROR: $REPO:$TAG already exists; a pinned image is never rebuilt" >&2
  exit 1
elif ! grep -q ImageNotFoundException <<<"$found"; then
  echo "ERROR: could not check whether $REPO:$TAG exists: $found" >&2
  exit 1
fi

CONTEXT=$(mktemp -d)
trap 'rm -rf "$CONTEXT"' EXIT
git archive HEAD | tar -x -C "$CONTEXT"
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$REGISTRY" >/dev/null
docker build --platform linux/amd64 -f "$CONTEXT/deploy/standardise.Dockerfile" -t "$REPO:$TAG" "$CONTEXT"

# The base's installed packages must meet this commit's requirements: pip resolves them offline, so an unmet one fails.
REQ_ARGS=()
for f in "${REQUIREMENTS[@]}"; do REQ_ARGS+=(-r "/app/$f"); done
docker run --rm --platform linux/amd64 --entrypoint pip "$REPO:$TAG" install --dry-run --no-index --quiet "${REQ_ARGS[@]}" \
  || { echo "ERROR: the base does not meet this commit's package requirements; build a new base first" >&2; exit 1; }
# Every standardise run mode the entrypoint dispatches (deploy/run_pipeline.sh) must import in the image.
IMPORTS=$(grep -o 'python -m stone_pipeline\.photos\.[a-z_]*' "$CONTEXT/deploy/run_pipeline.sh" | sed 's/python -m //' | sort -u | paste -sd, -) \
  || { echo "ERROR: no standardise run modes found in deploy/run_pipeline.sh" >&2; exit 1; }
docker run --rm --platform linux/amd64 --entrypoint python "$REPO:$TAG" -c "import $IMPORTS"

docker push "$REPO:$TAG" | tail -1
DIGEST=$(aws ecr describe-images --region "$REGION" --repository-name blokport-scraper --image-ids imageTag="$TAG" \
  --query 'imageDetails[0].imageDigest' --output text)
echo "==> pushed $REPO:$TAG ($DIGEST); pin it in infra/brands/<brand>.tfvars (dev_/prod_standardise_image_tag)"
