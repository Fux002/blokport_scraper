# The standardised-photo process

One process gives every listing of an environment one good standardised photo, or puts it on the review list with the
reason. It runs by itself after the owner's produce button; nobody picks listings, sets tags, sets a per-run cap or
publishes by hand any more. This replaces the operator rounds (laptop `standardise_run.py` with LISTINGS_KEY, RUN_TAG,
CLEAN_TAG, SPEND_CAP_USD, a separate `standardise_publish.py`, and a wrapper that copied prod settings from the Batch job
definition).

## Flow

```
owner presses Run (or Republish)            POST /config/v1/run {"stage": "all"}
  produce: scrape, build, publish the product sheet
  succeeded and SCRAPER_STANDARDISE_AUTO=true
    -> the runner starts the next run: stage "photos"   (its own run record, same run slot)
       python -m deploy.standardise_run:
       1. lock        <env>/standardise-test/run.lock (one process per environment)
       2. pick        every listing of the sheet just published WITHOUT a valid choice record
                      (record missing, or its photo no longer the listing's, or on the exclude list)
       3. GPU         the listings' first JUDGE_MAX photos the standardiser has not run (RUN_TAG)
       4. judge       each listing's results in its own order until one passes
       5. clean       only for a listing whose every judged photo failed for a cleanable reason:
                      one photo, hosted edit models cheapest first (nano2, kontext, flux3) until a copy passes
       6. GPU         the cleaned copies (CLEAN_TAG)
       7. judge       the cleaned copies' results
       8. report      choice records (<clean root>/chosen/<listing>.json), the review list
                      (<clean root>/tracked.csv) and the run summary (<clean root>/summary.json)
       9. publish     the chosen photos' views and 3D slabs into <env>/products/standard/, copying only what
                      changed and removing what is no longer chosen
      10. unlock
      11. relink      when step 9 changed anything: produce --stage republish, in the same run, so the new
                      photos are linked and the listings go live in the same button press
```

Every step reads back what earlier runs stored (verdicts, cleaned copies, paid edit requests, GPU results), so a run
that stops is simply run again, and nothing is paid twice. A listing on the review list is picked again by the next
run at no hosted cost (its answers are stored), so the list is always complete, and a change that can help it (a
higher JUDGE_MAX, a new clean instruction version) reaches it by itself.

## Trigger

The process is a stage of the config runner (`photos`), chained after every produce stage that publishes a product
sheet (`catalog`, `all`, `republish`) when `SCRAPER_STANDARDISE_AUTO` is on. It can also be started on its own:
`POST /config/v1/run {"stage": "photos"}`. Nothing starts it otherwise (no schedule); a later schedule is an
EventBridge target on the same API, like the produce.

Why the runner, not a Batch or ECS task the produce submits:
- It needs the product sheet the produce just published and must not overlap a produce: the runner's single run slot
  gives exactly that (and refuses destructive ops while it runs), with no second lock to keep in step.
- The relink is a produce, which writes the ledger; only the svc config container holds the ledger.
- Its log streams to the svc's CloudWatch and the run record keeps the tail, like every produce.
- No new compute: it only orchestrates (S3, Batch API, hosted HTTP calls; the 3D cut-outs are light CPU work).
  The GPU work stays on the existing Batch GPU queue.

Trade-offs: the run slot is busy while the process runs, so the next produce waits (409). The process bounds its own
work to `SCRAPER_STANDARDISE_WORK_SECONDS` (default 2.5 h), leaving the rest of the runner's 4 h ceiling for publish
and the relink. A big backlog is worked off over several presses. The runner kills a run past its ceiling without
cleanup; the work window makes that a backstop only.

Why relink in the same run: with `require_standard_photo` on, a new listing stays hidden until its photo is published
AND a produce has linked it. Waiting for the next button press would leave freshly scraped listings hidden for days.
The relink runs only when the published folder changed, so a run with nothing new costs no extra produce.

## Budget

One hosted budget per calendar month (UTC) per environment, `SCRAPER_STANDARDISE_BUDGET_USD`. No default: unset, the
process refuses to start (not configured). The month's spend is kept at
`<env>/standardise-test/spend/<YYYY-MM>.json` as `{"runs": {"<run id>": usd}}`; the month's total is the sum. A run
reads it at start and rewrites its own entry after every hosted call is reserved or settled (reserved counts as spent
until the bill is known), so a run that dies leaves an upper bound, never too little. A call that would take the
month's total past the budget is not made: the run stops its hosted steps loud (exit 3), still reports, publishes what
is chosen and relinks. The rest waits for the next month or a higher budget. GPU cost is not in this budget; it is
bounded by the work window and the GPU pool's ceiling (RUN_GPU_VCPUS, set back to 8 after every round).

## Layout

The process continues in the tags the published results came from, so no verdict, cleaned copy or choice record is
orphaned:

| environment | RUN_TAG (scraped photos) | CLEAN_TAG (cleaned copies, choice records, review list) |
|---|---|---|
| dev  | `v2` | `v2-clean` |
| prod | `v2` | `v2-clean2` |

They are fixed in `settings.STANDARDISE` (not env, so they cannot be mistyped per deployment). Moving to new tags is a
deliberate code change and a migration (copy the choice records), never a setting. Under `<env>/standardise-test/`:

```
run.lock                      the running process (taken over when older than LOCK_STALE_SECONDS: its run was killed)
spend/<YYYY-MM>.json          the month's hosted spend per run
<RUN_TAG>/<src>/...           GPU results, held/, judged/ (unchanged)
<CLEAN_TAG>/<src>/...         cleaned/, attempts/, GPU results of cleaned copies, judged/ (unchanged)
<CLEAN_TAG>/chosen/<listing>.json   one record per listing with a good photo (what publish reads)
<CLEAN_TAG>/tracked.csv       the review list: every listing no step could fix, what happened to each photo
<CLEAN_TAG>/summary.json      the last run: counts, spend, budget, pending listings, problems, published changes
```

## Settings

All in `stone_pipeline/config/settings.py` (`STANDARDISE`), set per environment by Terraform on the svc config
container:

| setting | env | meaning |
|---|---|---|
| AUTO | `SCRAPER_STANDARDISE_AUTO` | run the process after each produce (default off) |
| BUDGET_USD | `SCRAPER_STANDARDISE_BUDGET_USD` | the month's hosted budget (required, no default) |
| WORK_SECONDS | `SCRAPER_STANDARDISE_WORK_SECONDS` | the run's work window (default 9000) |
| JUDGE_MAX_PER_LISTING, RUN_SLICE, RUN_GPU_VCPUS, SELECT_WORKERS, CLEAN_ENABLED | as before | sizes of a run |
| (job target) | `SCRAPER_GPU_QUEUE`, `SCRAPER_STANDARDISE_JOBDEF` | the GPU queue and the standardise job definition |
| (hosted key) | `FAL_KEY` | already injected on the config container in both environments |

The standardise job definition moves into Terraform (`gpu_enhance` module, image pinned by `*_standardise_image_tag`
in the brand tfvars), so the laptop registration script and the settings wrapper are no longer needed. The svc task
role gains the Batch calls the process makes (submit to the standardise job definition, describe, terminate, set the
GPU pool's ceiling).

## Exit codes of the process

0 done; 1 some listings or photos failed or the work window ended (the next run goes on); 2 not configured;
3 the month's budget was reached; 4 another process is running. The relink's own failure is the run's failure.

## Failure modes

- Budget reached: hosted calls stop; what is chosen is published and linked; exit 3; review the summary.
- Work window ended: GPU jobs of the round are stopped, unfinished listings are `pending` in the summary; next press.
- GPU job failed or a photo crashed the GPU: listed as problems; its photos are retried next press, a photo that
  crashes the GPU twice is held (as before).
- Hosted call failed (timeout, 5xx, 429): that listing is retried once in the run, then next press. Auth or balance
  errors are loud.
- svc rolled or killed mid-run: the GPU jobs run to their own timeout; the lock is taken over once it is older than
  LOCK_STALE_SECONDS; everything stored is reused. Rolls during a run stay forbidden (as for the produce).
- A photo added to the reviewed exclude list (`stone_pipeline/config/standardise_exclude.txt`): the next run removes
  it from the published folder and relinks; the listing is hidden until another of its photos passes (the press after,
  once its other photos are back in the sheet).
- A listing that left the sheet keeps its choice record and published files (harmless: products link only their own
  photos); housekeeping is an open item.

## What an operator still does

1. Press Run (or Republish) in the admin. Everything else follows.
2. Read the review list (`tracked.csv`) and the summary; add a photo the judge let through to the exclude list, if any.
3. Raise the month's budget when the summary says it was reached and more is wanted.

One-time, per environment: set the Terraform values (AUTO, BUDGET_USD, the standardise image tag) and apply.
