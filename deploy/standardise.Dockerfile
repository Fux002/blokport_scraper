# The photo standardiser's GPU image for one commit (deploy/build_standardise_image.sh): the base (the Dockerfile's
# standardise-edit target: CUDA torch and the pinned, baked models) with this commit's code on top. A full build of
# that target downloads about 25 GB of models; this adds only the code, so a release takes minutes and every image of
# one base runs the same models.
#
# The base is pinned by digest, never a tag: a moved tag can never change the models under a release. A new base
# (new models or packages) is built from the Dockerfile and pushed as standardise-base-<sha12>, kept by the ECR
# lifecycle (infra/main.tf); then its digest goes here.
ARG BASE_IMAGE=026090543098.dkr.ecr.eu-west-1.amazonaws.com/blokport-scraper@sha256:8908a42bd76e40308896ded9a6cfc189f1d918f62f6b600aa4e2a94464cc90da
FROM ${BASE_IMAGE}
# The base carries the code of the commit it was built from: removed first, so the image runs exactly this commit's.
RUN rm -rf /app/deploy /app/stone_pipeline
COPY deploy /app/deploy
COPY stone_pipeline /app/stone_pipeline
RUN chmod +x /app/deploy/run_pipeline.sh
