"""The standardised-photo process (STANDARDISE_PROCESS.md): the application on top of the photo domain library
(stone_pipeline/standardise). process runs it (the runner's photos stage); select judges, cleans and reports; publish
makes the published folder match the choice records; decisions applies the owner's decisions; review serves the
hidden-listings page; batch runs the GPU rounds on AWS Batch, whose jobs are source, close and retouch_job (diag and
edit_test are dev jobs, mark_logo_step a one-off marker)."""
