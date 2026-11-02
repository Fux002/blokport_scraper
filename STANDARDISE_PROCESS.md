# The standardised-photo process

One process gives every listing of an environment one good standardised photo, or puts it on the review list with the
reason. It runs by itself after the owner's full produce; nobody picks listings, sets tags, sets a per-run cap or
publishes by hand any more. This replaces the operator rounds (laptop `standardise_run.py` with LISTINGS_KEY, RUN_TAG,
CLEAN_TAG, SPEND_CAP_USD, a separate `standardise_publish.py`, and a wrapper that copied prod settings from the Batch job
definition).

## Flow

```
owner presses Run                            POST /config/v1/run {"stage": "all"}
  produce: scrape, build, publish the product sheet
  succeeded and SCRAPER_STANDARDISE_AUTO=true
    -> the runner starts the next run: stage "photos"   (its own run record, same run slot)
       python -m deploy.standardise_run:
       1. lock        <env>/standardise-test/run.lock, rewritten every 5 min while the process runs
       2. clear       GPU jobs a killed earlier run left (photos-<run id>-...) are stopped, the GPU pool's ceiling
                      set back to idle
       3. pick        every listing of the sheet just published (its photos from the photo list beside it)
                      WITHOUT a valid choice record
                      (record missing, its photo no longer the listing's or on the exclude list, not the owner's
                      choice, or a conflict photo the owner did not assign), and every slab listing whose chosen
                      photo is no whole slab (a close-up), its choice kept; never one the owner removed, never a
                      tile listing (rendered at step 10, see Tile listings)
       4. GPU         the listings' first JUDGE_MAX photos the standardiser has not run, or whose result is
                      outdated (RUN_TAG); none of a listing the owner chose a photo for
       5. judge       the owner's choice for the listing, else its results in its own order until one passes (a
                      conflict photo is never chosen by itself); a slab listing goes on past a passing close-up to
                      its whole slabs only, and takes the first of those that passes
       6. clean       only for a listing whose every judged photo failed for a cleanable reason:
                      one photo, hosted edit models cheapest first (nano2, kontext, flux3) until a copy passes
       7. GPU         the cleaned copies (CLEAN_TAG)
       8. judge       the cleaned copies' results
       9. report      choice records (<clean root>/chosen/<listing>.json), the review list
                      (<clean root>/tracked.csv) and the run summary (<clean root>/summary.json)
      10. publish     the chosen photos' views and 3D slabs into <env>/products/standard/, copying only what
                      changed; then every tile listing's views and 3D tile rendered from its variety's slab
                      stone (CPU, only what changed); then what is no longer chosen or rendered is removed
      11. unlock      only if the lock is still this run's
      12. relink      when step 10 changed anything: produce --stage republish, inside the same process (never a
                      child, so a run the runner stops takes it along), so the new photos go live in the same press
```

Every step reads back what earlier runs stored (verdicts, cleaned copies, paid edit requests, GPU results), so a run
that stops is simply run again, and nothing is paid twice. A listing on the review list is picked again by the next
run at no hosted cost (its answers are stored), so the list is always complete, and a change that can help it (a
higher JUDGE_MAX, a new clean instruction version) reaches it by itself.

## A whole slab before a close-up (the 3D slab)

A result is a whole slab when its run saved `<sha>.slab.png`: a single slab, or an open book, whose slab is one of its
two halves (each half is a whole real slab: the one with more stone in its outline as the layout draws it, the left on
a tie; its close view is made from that half too). Which slab it is, is recorded beside it (`<sha>.slab.json`, written
just before the slab), so a close-only run makes the close-up of that same half, never choosing again; a result made
before that record existed gets the same rule again, on the outlines as drawn. Publish makes the 3D slab from it. A close-up shows only part of a slab: no slab.png, no
close view, no 3D.

A slab listing prefers a whole slab, within the same JUDGE_MAX candidates and their order. Once a close-up has passed
(in this run, or as the listing's choice from an earlier one, picked again as "kept"), only the listing's whole results
are judged after it, and the first that passes becomes the choice; if none does, the close-up stays. What was already
tried is read from the stored results and verdicts (no marker): a kept listing whose candidates all have a result and
a verdict costs nothing again, and its record is not rewritten. It never goes to the hosted clean step (it has a
passing photo). The GPU and the judge stay inside the month's budget and the round sizing like every listing. When the
choice moves, publish removes the close-up's views and adds the whole slab's views and its 3D slab. Blocks take
their first pass as before.

A listing's photos come from the photo list, never from the sheet's image columns: once a listing is published its
sheet row carries only its chosen photo's views. The produce writes the list beside the sheet,
`<env>/scraper/to_upload/standardise_photo_list.json` (`{Product Handle: [[source, sha], ...]}`, each product's own hosted
photos in its order, kept by the image stage before the standardised views replace them; sorted, the same bytes on a
re-run, listed in the deliverables manifest like every file there). The pick step and the GPU job both read it. A list
that is missing or malformed, or a sheet listing it does not have, stops the run loud; there is no fallback to the
sheet. A source whose latest run has no list (a run made before the list existed, restored after a roll) never blocks
the catalog: its source is left out of the list and named in a warning, and the photo process stops on its listings
until a produce runs that source again. A reset removes the list with the product sheets.

An open book made before its halves had a slab (a side view, no slab.png) is outdated: the next run sends it to the
GPU once, which completes it from a re-run (its close view, then its slab, last). What was judged and published (main,
white, studio) stays as it is, so its verdict and choice stand and no judge call is made; publish then adds its close
view and 3D slab. A re-run that is no open book this time, or that fails for the photo itself, adds nothing and is
noted in `noslab/<sha>.json`, so it is never run again; a judged result is never held. A completion cut short takes its
close view off again while its slab is missing, and a passing storage error leaves it for the next run. Nothing is
removed by hand.

## Tile listings

The owner (2026-11-02): a tile listing is shown from the same variety's judge-passed, published slab stone, from any
vendor's slab of that variety, never from its own photos (warehouse racks) and never generated. Publish renders them
(`stone_pipeline/standardise/tile_plan.py`, `tile_stone.py`, `tile_views.py`), on the CPU, with no judge per tile.

- **Stone, never a guess**: a slab listing of the same variety (stone type and name, through the variations export;
  the tile's source from the photo list)
  AND the same finish whose chosen photo has a published 3D cut-out. None: the listing gets no files and stays hidden
  by the no-photo rule, with its reason in `summary.json` (`tiles_hidden`): no slab of the variety, none in this
  finish (Tiger Black Flamed), none with a published 3D cut-out, or every one refused.
- **Refused cut-outs**: a crosshatch over the face (straight lines along both axes: `TILE_GRID_*`; Tiger Black
  805cf5ac scores 17.6, every other of 510 prod cut-outs at most 1.97), or proportions more than
  `TILE_ASPECT_TOLERANCE` from the slab's listed size (its scale would be a guess). Lamp reflections
  (`marks.flare_mask`) and a 3 cm rim are never cut. The slab job's fills (labels, straps, flares it painted over) are
  not saved, so they cannot be avoided yet: the job would have to save a fill mask beside `<sha>.slab.png`
  (`<sha>.fill.png`, 8-bit, the squared slab's frame, 255 where it painted or a hosted edit changed the stone) and
  publish carry it beside the cut-out; the tile step would then add it to the stone it never cuts.
- **One slab, never a pool**: colour and texture statistics cannot tell a different stone from the same stone shot
  differently (Pietra Grey 902c4b, a brown quartzite, is within 1 L* of its pool; 8160c6, the same grey stone shot
  darker, 17 L* off), and pooled tiles mix lots. A variety's slabs are tried in one order: the tile's own
  vendor's first, then the most usable stone, then the cut-out key; a listing takes the first on which a tile of its
  size fits. One (variety, finish) at a time and one decoded slab at a time (the checks decode each slab in turn and
  keep only its usable area; the slab cut from is decoded again).
- **Size**: a tile is drawn only at its listing's own L x W x H, and only when that is a tile's size (each side in
  `TILE_SIDE_RANGE_M`, the thickness in `TILE_THICKNESS_RANGE_M`); otherwise no images, the reason in `tiles_hidden`.
  A size the produce filled from the pack's defaults (flagged `dimension_defaulted`) is never shown: the image stage
  links no rendered set to that row (the sheet carries only the numbers, and the tile default 60 x 60 x 2 is also a
  common real size, so publish cannot tell them apart).
- **Views** (the owner's set, 2026-11-02; the slab view folders, the contract unchanged): main (the catalog card) = the
  laid tiles from straight above at true scale, a few tiles and their 3 mm joints, real pixels enlarged at most 2x;
  white = one tile face-on; studio = one tile at an angle, its three dimensions with the true thickness; the close
  slot is not used (the main view is the close-up). With fewer than `TILE_MIN_FACES` distinct faces (non-overlapping
  cuts of the slab) the laid tiles would repeat a face: main is then the single tile face-on and there is no separate
  white view. No perspective floor or room is drawn.
- **3D tile**: the same cut-out format as a slab's, the face at the listing's stored length along x, 2048 px on its
  long side (Lanczos). `px_per_mm` is the stone's real resolution in source pixels per millimetre, measured before the
  resample to 2048 px (0.65 to 1.2 on prod); the cut-out's own scale is `width_px` / (`length` in mm), which is
  higher. Model_3d flows as for a slab.
- **Names**: a tile's files are named after the listing (`imagestore.listing_sha(handle)`), not a photo (one photo can
  show six sizes), under `standard/<view>|cutout/<src>/`, plus `standard/tile/<src>/<name>.json`: what they were made
  from (the slab's cut-out and ETag, the size, the finish, `TILE_RENDER_VERSION`), written last. The image stage links
  a tile product only to the set named after it (never to one of its photos); any other product ignores such a set.
- **Rendered again** exactly when that record's stamp changes; the output is byte-identical for the same inputs per
  build (the same OpenCV and NumPy).
- **One tile never stops the run**: a read, decode or encode error in one tile listing's step (S3, OpenCV, a value
  or file error) puts it on the run's problem list (`summary.json`) and names it in the run record
  (`published.tiles.failed_listings`, at most `TILE_FAILED_LISTED`); it keeps the files it had while its record's
  stone is still one of the slabs it may be cut from, or while S3 fails to return its record (unknown is not gone);
  when the record is missing, corrupt or names another stone they are removed and it hides until it renders again. A
  record that cannot be read (our own file, written last) only means out of date: the tile renders again, noted on the
  problem list. Any other error fails the run, and a failed bucket listing stops publish, before anything is removed.
- **Variations export**: a sheet product whose variation is missing from it is matched to no stone; the log says how
  many (the export is stale or empty) and the run record counts them (`tiles.unknown_variations`).

## Trigger

The process is a stage of the config runner (`photos`), chained only after a successful full produce (`all`, the
owner's Run) when `SCRAPER_STANDARDISE_AUTO` is on, never after `republish` or `catalog`. It can also be started on its
own: `POST /config/v1/run {"stage": "photos"}`. Nothing starts it otherwise (no schedule).

When the close-up rule changes (`STANDARDISE.CLOSEUP_VERSION`), the chosen photos' close-ups are made again by a
close-only photos run, an operator's action (no UI button): `POST /config/v1/run {"stage": "photos", "close_only": true}`.
It is a photos run like any other (the run slot, giving way to the owner's Publish, a run record with `close_only: true`);
the runner alone tells the process it is close-only (`SCRAPER_STANDARDISE_CLOSE_ONLY` in that run's environment only). It
holds the same lock, budget, work window and run id, makes one close-only GPU job per slice of the chosen photos whose
close-up is not made yet (`deploy.standardise_close`) instead of steps 1-7, then publishes and relinks as any run; its
summary is `close-summary.json`, so the photos run's `summary.json` stays.

The owner's Publish is never blocked: any other stage asked for while a photos run holds the run slot (Blokport's
Publish button runs a republish) stops the photos run cleanly (the SIGTERM path below) and starts at once; the request
gets its 202 straight away. The photos run ends `stopped`; everything it stored is kept and the next photos run goes on
from there. A photos run asked for while another run holds the slot is refused (409), as before.

Why the runner, not a Batch or ECS task the produce submits: it needs the product sheet the produce just published and
must not overlap a produce, which the runner's single run slot gives; the relink writes the ledger, which only the svc
holds; its log streams to the svc's CloudWatch with the run record keeping the tail. No new compute: it only
orchestrates (S3, Batch API, hosted calls; 3D cut-outs four at a time). Trade-off: the run slot is busy while it runs.

Why relink in the same run: with `require_standard_photo` on, a new listing stays hidden until its photo is published
AND a produce has linked it. The relink runs only when the published folder changed.

## Run record

A photos run ends `succeeded` whenever it went through, also with pending listings, problems or the budget reached;
`stopped` when it gave way to another stage; `failed` only on a real error (not configured, another process holding the
lock, a crash, a failed relink). Its record
(`GET /config/v1/run`, stage `photos`) carries the summary as `counts`: listings, chosen, review, pending, problems,
published changes, run and month spend, budget, budget_reached, relinked. The same summary, with the pending listings
and the problems, is kept in `<clean root>/summary.json`.

## Budget

One budget per calendar month (UTC) per environment, `SCRAPER_STANDARDISE_BUDGET_USD`, for everything the process pays:
hosted calls (judge as billed, edits at list price) and GPU time (each job's running time from Batch's start and stop
times, read once the jobs have actually stopped, plus `GPU_JOB_ALLOWANCE_SECONDS` (12 min) per job that started, for
the boot, the image pull and the pool's idle tail, at `SCRAPER_STANDARDISE_GPU_VCPU_HOUR_USD` per vCPU-hour, default
0.147: g4dn.xlarge on demand). No default budget: unset, the process refuses to start.

- Each run keeps its own spend in `<env>/standardise-test/spend/<YYYY-MM>/<run id>.json`, so no run can overwrite
  another's. The month's other runs are summed at start and again at every settle.
- A price is reserved before a call or GPU round and settled to what it cost. A GPU round reserves its worst case from
  the slice plan (its running time: one GPU per slice at a time, never past the work window, at most the pool's
  ceiling at once; plus the allowance of every job that can start, two per slice), so it never settles above what it
  reserved. A round is sized to the budget left: as many whole slices as fit; if not even one fits, the
  GPU work waits and the hosted steps still judge the photos already finished, within their own reservations.
- Storage is never asked while the spend's lock is held, and a stop halts paid work without taking that lock.
- A call that fails after it may have been sent, an answer without its cost, and a GPU round whose job times cannot be
  read all count at the reserved price, never as free. A reserve counts as spent in the file until it is settled.
- A call or round that would take the month past the budget is not made: paid work stops, the run still reports,
  publishes and relinks, and its summary says `budget_reached`.

## Timing and stopping

- Work window `SCRAPER_STANDARDISE_WORK_SECONDS` (default 2 h): after it no hosted call and no GPU round starts and a
  running round's jobs are stopped. No GPU round starts with less than `GPU_MIN_WINDOW_SECONDS` (30 min) of it left. Report, publish and relink get `FINISH_SECONDS` (1.5 h). The process refuses to
  start unless both fit under the runner's ceiling (`SCRAPER_RUN_TIMEOUT_SECONDS`, 4 h on the svc).
- A stop of the svc (a roll): no run starts any more, and the config server passes SIGTERM to the running run. One
  deadline, `SCRAPER_RUN_STOP_GRACE_SECONDS` (80 s), bounds the whole stop and leaves 40 s of the container's
  `stopTimeout` (120 s) for the last config save. The photo process stops its GPU jobs (waiting up to 60 s for them to
  stop, so their real times are counted), sets the pool's ceiling back, settles its spend and releases its lock; the
  work left in its pools is cancelled; its GPU jobs are stopped all at once. A run still going at the deadline is
  killed; what a killed photos run left is cleared by the next photos run (clear_leftovers, the stale lock), never
  during the stop, so the last save keeps its time. Hosted calls already in flight end with the process; their
  reserves stay counted. The run record is finished before the server's last save.
- Giving way (the owner's Publish during a photos run) uses the same deadline. A photos run marked to give way before
  its subprocess started never starts; one that ended on its own before the signal keeps its real status. Two runs are
  never under way at once: the mark, the stop and the launch decision are taken under one lock. A photos run killed
  while giving way has what it left cleared within the deadline, by its run id only: the runner's run id names its
  lock and its GPU jobs, so its jobs are stopped and the lock and the pool's ceiling are reset only while the lock is
  still its own (a rolling deploy's other task or a process started by hand is never touched).
- A heartbeat that finds the lock gone or replaced stops the run's paid work: it no longer holds the lock.
- The lock is taken over only when it has not been rewritten for the runner's ceiling (its process was killed). A lock
  taken over while a run works stops that run's paid calls; a run removes the lock only while it is still its own.

## Layout

The process continues in the tags the published results came from, so no verdict, cleaned copy or choice record is
orphaned:

| environment | RUN_TAG (scraped photos) | CLEAN_TAG (cleaned copies, choice records, review list) |
|---|---|---|
| dev  | `v2` | `v2-clean` |
| prod | `v2` | `v2-clean2` |

They are fixed in `settings.STANDARDISE` (not env, so they cannot be mistyped per deployment). Moving to new tags is a
deliberate code change and a migration, never a setting: copy the choice records and the owner's choice records
(`<CLEAN_TAG>/owner/`) to the new clean tag, and keep (or copy) the results they name, since each names a tag and a
photo; an owner record naming a tag the process no longer runs is not read (the listing is worked again). The removal
records are outside the tags and need nothing. Under `<env>/standardise-test/`:

```
run.lock                            the running process (rewritten every 5 min)
spend/<YYYY-MM>/<run id>.json       each run's spend of the month
removed/<SKU>.json                  a listing the owner removed (SKU percent-encoded)
<RUN_TAG>/<src>/...                 GPU results, held/, judged/, noslab/ (an outdated open book tried once)
<CLEAN_TAG>/<src>/...               cleaned/, attempts/, GPU results of cleaned copies, judged/ (unchanged)
<CLEAN_TAG>/owner/<SKU>.json        the owner's photo choice of a listing (SKU percent-encoded): {tag, sha, at, listing,
                                    decided_on}
<CLEAN_TAG>/chosen/<listing>.json   one record per listing with a good photo (what publish reads)
<CLEAN_TAG>/tracked.csv             the review list: every listing no step could fix, what happened to each photo
<CLEAN_TAG>/summary.json            the last run's summary, its pending listings and problems
```

## Settings

All in `stone_pipeline/config/settings.py` (`STANDARDISE`, plus the runner's `RUN_TIMEOUT_SECONDS` and
`RUN_STOP_GRACE_SECONDS`), set per environment by Terraform on the svc config container:

| setting | env | dev / prod |
|---|---|---|
| AUTO | `SCRAPER_STANDARDISE_AUTO` | off / off (owner's call) |
| BUDGET_USD | `SCRAPER_STANDARDISE_BUDGET_USD` | 20 / 100 (owner, 2026-11-02; hosted + GPU, required) |
| RUN_GPU_VCPUS | `SCRAPER_STANDARDISE_GPU_VCPUS` | 32 / 128 |
| GPU_VCPU_HOUR_USD | `SCRAPER_STANDARDISE_GPU_VCPU_HOUR_USD` | 0.147 (default) |
| WORK_SECONDS | `SCRAPER_STANDARDISE_WORK_SECONDS` | 7200 (default) |
| (job target) | `SCRAPER_GPU_QUEUE`, `SCRAPER_STANDARDISE_JOBDEF` | from the gpu_enhance module |
| (hosted key) | `FAL_KEY` | already injected in both (not needed by a close-only run) |
| CLOSE_ONLY | `SCRAPER_STANDARDISE_CLOSE_ONLY` | never set on the svc: the runner sets it per photos run (`close_only`) |

## Failure modes

- Budget reached: paid work stops; what is chosen is published and linked; the run succeeds with `budget_reached`.
- Work window ended: the round's jobs are stopped, unfinished listings are `pending`; the next press goes on.
- GPU job failed or a photo crashed the GPU: listed as problems; retried next press (a photo that crashes the GPU
  twice is held, as before).
- Hosted call failed (timeout, 5xx, 429): that listing is retried once in the run, then next press. Auth or balance
  errors are loud.
- svc stopped mid-run: clean stop as above; if it is killed regardless, the next run stops the jobs it left, resets the
  ceiling and takes the lock over once it is stale. Rolls during a run stay forbidden (as for the produce).
- A photo added to the reviewed exclude list (`stone_pipeline/config/standardise_exclude.txt`): the next run removes
  it from the published folder and relinks; the listing is hidden until another of its photos passes (its other
  photos are in the photo list, so the same run can try them).
- The photo list missing or malformed, or a sheet listing missing from it: the run fails loud before any paid step; a
  produce that runs the sources named in the catalog's warning writes it again.
- A listing that left the sheet keeps its choice record and published files (harmless: products link only their own
  photos); housekeeping is an open item.
- A tile listing without slab stone of its variety and finish: no files; listed in `summary.json` (`tiles_hidden`).

## Rollout (once, per environment)

1. Pin the GPU image: add the tag `standardise-pin-cbc576cf9ebd` to the image `standardise-edit-cbc576cf9ebd-x` in ECR
   (same digest; `aws ecr put-image` with its manifest). The lifecycle rule `standardise-pin-` keeps pinned images; the
   build tags (`standardise-edit-...`) stay subject to the keep-3 churn rule. Later releases: tag the new image
   `standardise-pin-<sha>`, then change `*_standardise_image_tag`.
2. The job definition moves into Terraform. Before applying, compare the live latest revision of
   `blokport-standardise-<env>` (`aws batch describe-job-definitions`) with the planned one: image (by digest), vCPU 4,
   GPU 1, memory 15000, job and execution roles, environment. Apply (Terraform registers a new revision of the same
   name), then deregister the hand-registered revisions. The hand scripts (`register_prod_jobdef.py`,
   `with_prod_env.sh`, `prod_run.sh`, the laptop `prod_review.py`) are retired.
3. The budgets are in the brand tfvars (dev 20 USD, prod 100 USD a month). Switch AUTO on in dev, verify a dev run, then
   prod after the owner's review of it.

TerminateJob scope: the svc task role may stop jobs `arn:aws:batch:<region>:<account>:job/*`. Batch has no condition on
a job's name; scoping to the process's own jobs would need tags on submit (`batch:TagResource`) and an
`aws:ResourceTag` condition on TerminateJob, which is not applied here: the process only stops jobs it named
(`photos-<run id>-...`).

## Shared photos

A photo belongs to a stone and finish, not to one listing. There is one conflict set, made once and read by every side.
The produce makes it after validation (`stages.images.link_standard`): over the products it emits, nothing validation
rejects, each with every photo it has, it classifies each photo several products of a source show with
`stone_pipeline.standardise.index.shared_photos` (by variety and finish ids), applies it to its own linking, and stores
it per source as `standard/conflicts/<src>.json` (`{sha: [handles]}`, whole, an empty set too, written only when it
changed). Only a produce that writes the bucket stores it (s3 image mode without the s3 dry run, as the service runs);
a laptop run never overwrites an environment's set. The photo process, the owner's decisions and the review page read
that stored set (`standardise_publish.read_conflicts`); none classifies conflicts again, and a source on the sheet with
no stored set is refused ("no conflict set yet: run a produce first", the photos run exits 2), never read as no
conflicts. (The sheet could not serve as the input: it carries only a live product's lead photo.)

1. Shown by one listing: nothing special.
2. Shown by listings of one variety and finish (slabs of one block or bundle, tile sizes): the vendor's own practice. A
   pass is good for them all, and the owner's choice on one reaches the others (below; the groups come from the sheet,
   `index.groups`, never for a photo in the conflict set).
3. Shown by products of different varieties or finishes (prod: Luminatta Leathered and Polished at zucchi, Royal Black
   and Toros Black at marenostone): a conflict. The photo is never chosen by itself for any of them (the review list
   says `photo_conflict` and names the others), and a choice made before it became one no longer counts. Only the owner
   assigns it, to one listing, by a decision on that listing itself (a choice that reached it from a sibling of its
   stone never does); the applier refuses a second listing and names the first, and a photo the owner's choices name
   for two listings anyway is linked to neither and reported. Publish writes `standard/assigned/<src>/<sha>.json`
   (whose it is) before the photo's views and removes a stale one only after them, so a publish stopped half way never
   leaves a conflict photo's view without its file; a conflict photo nobody is assigned is not published. The produce
   links a conflict photo only to the product its file names, from the produce that makes it a conflict on. Two names
   for one stone (a variety alias) is a question for the variety review, not for this page.

Until the first produce after the release stores the sets, the photos run, the applier and the review page refuse.
That produce already holds a live listing whose photo is a conflict (prod: Royal Black and Toros Black) until the owner
assigns it on the review page built after it.

## The owner's decisions

The owner reviews the hidden listings (no chosen photo, not removed) on a decision page: per listing "Use this photo"
(one of its standardised results) or "Remove listing"; a shared photo says which listings of the same stone it also
applies to, and the conflicts page names, per conflict photo, every listing that shows it, live ones included (one
live now is hidden by the next run unless the owner assigns the photo to it). The page keeps one document per
listing; its export (a JSON array of `{id: <listing handle>, action, source, sha, tag, at}`) is applied with
`python -m deploy.standardise_decisions` (`SCRAPER_ENV` set explicitly, `DECISIONS_FILE=<export>`, `DRY_RUN=true|false`).
It prints the environment and bucket first. It checks every decision before writing anything: the listing is on the
sheet; the action is known with exactly the page's fields; `at` is an ISO UTC timestamp; the source is the listing's;
the photo is one of the listing's and not excluded; the tag's result exists; and the decision is not older than the one
already stored for the listing, so an old export applied again never undoes a later decision. One bad decision refuses
the whole file (exit 2). It reads and writes inside the run lock (exit 4 while a photos run holds it), writes only what
differs (applying again writes nothing), never touches the judge's verdicts, and the next photos run acts on it. Every
record is keyed by the listing's SKU: a variety decided later re-binds the product and moves its handle, never its SKU.

- **use**: the owner's choice record `<CLEAN_TAG>/owner/<SKU>.json` (`{tag, sha, at, listing, decided_on}`); a later
  decision overwrites it. Every run checks it (the photo is still one of the listing's, not excluded, its result there)
  and then chooses it before anything else: nothing of the listing is judged, cleaned or sent to the GPU. A record that
  no longer applies is not read, the same as a choice record. The choice reaches each listing of the same stone that shows
  the photo, each with its own record (`decided_on` names the listing decided on; the dry run lists them), except one with
  its own decision (in the same file or stored) and one live with its own photo (never re-chosen). A conflict photo is
  assigned to that one listing only. A "use" lifts the listing's removal.
- **remove**: a record `removed/<SKU>.json` (`{sku, listing, source, reason: "owner_removed", at}`); the listing's owner
  choice and choice record go. The process never works on the listing, and the image stage links nothing standardised
  to the product (no standardised thumbnail, gallery, 3D or version, whatever `require_standard_photo` says), even a
  photo published for a listing of its stone: it keeps its own images and has no standardised photo, so it stays hidden
  with reason `no_photo`. Nothing is deleted from the catalog; Blokport deletes the product.

Why removals are a per-environment store and not a reviewed file like the exclude list: the exclude list is about a
photo (its sha, the same in every environment) and is an engineering review; a removal is the owner's decision about one
environment's listing, made on that environment's page, and takes effect on the next run without a release, beside the
owner's choices it is applied with.

Why `no_photo` and not a new reason: the inventory reason is a closed set on Blokport's side (MEDUSA_INTEGRATION_GUIDE.md
section 5) and an unknown one is read as a plain quantity; `owner_removed` is kept in the removal record until Blokport
agrees to a new reason.

Order of review: variety minting and aliasing come first. That holds by construction: a product whose variety is still
pending review (a variety card in the config.db review queue) is not bound, fails validation (tree gap) and is not on
the product sheet, so it is never a listing on the decision page (prod 2026-11-02: no product of the 14 pending
variety cards is on the sheet). Once its variety is decided it is bound, appears on the sheet and is hidden until a photo
is chosen. A variety decided later moves the handle but not the photos or the SKU, so no decision is lost.

## What an operator still does

1. Press Run in the admin. Everything else follows.
2. Read the run record's counts (or `summary.json`) and the review list (`tracked.csv`); add a photo the judge let
   through to the exclude list, if any.
3. Raise the month's budget when the summary says it was reached and more is wanted.
