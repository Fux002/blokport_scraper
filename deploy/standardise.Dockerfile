# The photo standardiser's GPU image for one commit (deploy/build_standardise_image.sh): the base (the Dockerfile's
# standardise-edit target: CUDA torch and the pinned, baked models) with this commit's tree on top. A full build of
# that target downloads about 25 GB of models; this adds only the code, so a release takes minutes and every image of
# one base runs the same models.
#
# The base is pinned by digest, never a tag: a moved tag can never change the models under a release. A new base
# (new models or packages) is built from the Dockerfile and pushed as standardise-base-<sha12>, kept by the ECR
# lifecycle (infra/main.tf); then its digest goes here.
ARG BASE_IMAGE=026090543098.dkr.ecr.eu-west-1.amazonaws.com/blokport-scraper@sha256:8908a42bd76e40308896ded9a6cfc189f1d918f62f6b600aa4e2a94464cc90da
FROM ${BASE_IMAGE}
# The base holds the tree of the commit it was built from: all of it but the baked models is removed, then this
# commit's tree goes in exactly as the base target lays it out, so no file of the base's commit survives.
RUN find /app -mindepth 1 -maxdepth 1 ! -name models -exec rm -rf {} +
COPY . /app
RUN for f in /app/from_medusa/*/variants_export_base.csv; do [ -e "$f" ] && cp "$f" "${f%.csv}.seed.csv"; done
RUN chmod +x /app/deploy/run_pipeline.sh
