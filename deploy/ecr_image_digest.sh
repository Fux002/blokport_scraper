#!/usr/bin/env bash
# The digest the blokport-scraper ECR repo holds for one tag, or nothing when the tag does not exist: the Deploy
# workflow's one tag lookup (.github/workflows/deploy.yml), so an existing :<sha> or :gpu-<sha> is never rebuilt.
#
# Uses BatchGetImage (the deploy role has no DescribeImages). It answers a missing tag with one failure entry whose
# failureCode is ImageNotFound, and every other problem (a manifest type it was not asked to accept, a malformed tag,
# a denied call) differently. Only that one answer reads as "absent": anything else exits non-zero, because a lookup
# that cannot tell would let an existing immutable tag be rebuilt and re-pushed.
#
#   deploy/ecr_image_digest.sh <tag>
set -euo pipefail

TAG="${1:?usage: deploy/ecr_image_digest.sh <tag>}"
REPLY=$(aws ecr batch-get-image --repository-name blokport-scraper --image-ids imageTag="$TAG" \
  --accepted-media-types application/vnd.docker.distribution.manifest.v2+json \
    application/vnd.oci.image.manifest.v1+json application/vnd.docker.distribution.manifest.list.v2+json \
    application/vnd.oci.image.index.v1+json --output json)
jq -j --arg tag "$TAG" '
  if (.images | length) == 1 and (.failures | length) == 0 then .images[0].imageId.imageDigest
  elif (.images | length) == 0 and (.failures | length) == 1 and .failures[0].failureCode == "ImageNotFound" then ""
  else error("unexpected BatchGetImage reply for tag \($tag): \(.failures | map(.failureCode + ": " + (.failureReason // "")) | join("; "))")
  end' <<< "$REPLY"
