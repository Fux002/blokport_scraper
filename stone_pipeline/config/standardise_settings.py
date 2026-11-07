"""The photo standardiser's tunables (stone_pipeline/standardise and stone_pipeline/photos), in their own module.

STANDARDISE is computed once at import from the environment (through settings' env helpers) and the env tier
(ENV_SEGMENT). It imports settings, never the reverse, so a settings import never builds it. Every reader imports it
from here: settings keeps no copy."""

from __future__ import annotations

from stone_pipeline.config.settings import ENV_SEGMENT, _env_bool, _env_float, _env_int


class STANDARDISE:
    """Every tunable of the photo standardiser (stone_pipeline/standardise). Flat on purpose: one namespace."""
    SAM_MODEL = "facebook/sam-vit-huge"   # Apache-2.0; the largest SAM: cleanest slab edges (T4: ~0.5 s encode, fp16)
    FOREIGN_MIN, FOREIGN_MAX = 0.0005, 0.03   # an excluded region this share of the slab = an object on it
    FOREIGN_THICK = 0.015          # ... and at least this thick (share of the slab's short side), not an edge sliver
    WHITE_PCT = 90                 # white balance + exposure read off the slab's brightest areas (this percentile)
    WHITE_TARGET_L = 92.0          # those areas are brought towards this L* (bright white, not clipped)
    DARK_STONE_L = 50.0            # brightest areas still darker than this L*: a dark stone (black, deep red), exposure kept
    EXPOSURE_MAX = 1.35            # exposure lift bound (about +0.43 stop); never lowered (highlights roll off instead)
    SHOULDER = 0.85                # linear values above this roll off softly towards white instead of clipping
    WB_STRENGTH = 0.8              # share of the measured cast removed
    WB_MAX = 0.08                  # never move a channel by more than this share (keeps a red stone red)
    WB_NEUTRAL_CHROMA = 12.0       # the cast is read only off pixels this close to grey (C*)
    WB_MIN_NEUTRAL = 0.05          # fewer neutral pixels than this share of the slab: a coloured stone, no cast fix
    CLARITY = 0.30                 # local-contrast boost of the veining (L* only), 0 = off
    CLARITY_SIGMA = 0.012          # its radius, share of the slab's long side
    VIBRANCE = 0.10                # chroma lift for dull colours, fading to none at VIBRANCE_FULL C*
    VIBRANCE_FULL = 40.0           # (a saturated red is not pushed further)
    WORK_LONG_EDGE = 2000          # process at this long edge
    MIN_SLAB_SHARE = 0.15          # the slab must cover at least this share of the photo, else not a full-slab shot
    TRIM_SHARE = 0.03              # attachments thinner than this share of the short side are not slab
    QUAD_EPS = (0.01, 0.02, 0.03)  # hull simplification ladder (share of the perimeter), finest first: a chipped
                                   # corner fits at the finest, a rough natural edge needs a coarser one
    EDGE_COVER_MIN = 0.85          # the four longest edges must be this share of the outline: a rectangle slab
    MAX_TILT_DEG = 35              # a corner fit implying more tilt than this is a misfit: clean-up only
    FLAT_SIGMA = 0.18              # illumination blur, share of the slab's long side
    FLAT_STRENGTH = 0.7            # 0 = no flat-field, 1 = full
    OCR_MIN_CONF = 30              # word confidence to join a mark (the card/logo box decides what is erased)
    OCR_ANCHOR_CONF = 80           # a mark is real text only with a word read this surely (labels/logos: 85-97;
    OCR_ANCHOR_LEN = 3             # ... of at least this many characters; stone texture reads as 2-letter noise <70)
    OCR_TIMEOUT = 180              # s: tesseract reading one photo
    OCR_SHORT_CONF = 88            # a 2-character word anchors from this confidence (a "DC+" grade label reads at ~90)
    CARD_FILL = 0.7                # a bright component this rectangular around a word is a printed card
    CARD_MAX_SHARE = 0.08          # and no larger than this share of the photo
    CARD_SEARCH = 4.0              # look for a card outline within this many word-heights around a text cluster
    CARD_MAX_TO_TEXT = 4.0         # a card outline counts only up to this many times the area of the text it holds
    SIDE_RATIO_MAX = 2.5           # opposite sides may differ by this factor (a slab shot at an angle): steeper leaves the far
                                   # end too soft to show (19ab0177 at 3.1; 2.4 still sharp, adeebb0f)
    SIDE_RATIO_MEASURED = 1.35     # up to this the sides measure the slab's proportions; steeper, its real size does
    ASPECT_TOLERANCE = 0.10        # the real proportions may be this much narrower than a foreshortened photo shows
    OCR_CLUSTER = 1.5              # words within this many word-heights of each other form one mark
    OCR_LINE_GAP = 3.0             # a word on a surely-read word's text line, within this many word-heights, joins it
    OCR_LINE_HEIGHT = (0.5, 1.6)   # ... when its height is within this ratio of that word's
    MARK_PAD = 0.9                 # a mark's box is widened by this many word-heights on every side (card, logo)
    MARK_PAD_MAX = 0.012           # ... at most this share of the photo's long side (a code painted huge on the floor)
    FLARE_MAX_S = 40               # a lamp's reflection: colourless (HSV saturation, 0..255) ...
    FLARE_CONTRAST = 18            # ... this much brighter than the stone around it (pale stone leaves little room) ...
    FLARE_BASE_SIZE, FLARE_BASE_BLUR = 512, 41   # (that stone: a median over this filter size, on the photo at this long edge) ...
    FLARE_AREA = (0.00002, 0.012)  # ... this share of the slab's area ...
    FLARE_MIN_THICK = 5            # ... at least this thick (px) ...
    FLARE_SOLIDITY = 0.68          # ... compact: this share of its convex hull (a vein is not) ...
    FLARE_MAX_ELONGATION = 8       # ... at most this many times longer than wide (a strip light's tube; a vein is a line) ...
    FLARE_CORE_PCT = 90            # ... and its core (this percentile of its grey) near white ...
    FLARE_PEAK_MIN = 230           # ... at least this grey (pale flecks on a black stone reach 224) ...
    FLARE_STONE_PCT, FLARE_ABOVE_STONE = 99, 35   # ... and this far above the stone's own brightest (this percentile of it).
                                   # Measured 2026-11-01 on 19 photos: reflections core 232-254 and +37..+125 above it; white
                                   # veins/clasts on pale stone at most +27, pale flecks on black stone at most 224
    FLARE_GROW = 9                 # the fill also covers its glow, this many px wide
    MARK_ON_MIN = 0.5              # a word counts as on the slab when this share of it lies on the slab
    FILL_DETAIL_SIGMA = 0.003      # an erased area keeps LaMa's texture finer than this share of the long side; coarser tone is the stone's around it
    # --- marks ---
    # --- models and the join (blend), logos, views, what a photo shows (kinds) ---
    CARD_PROMPT = ("a stack of color sample cards. a sticker. a paper label. a printed sheet. a price tag. handwritten numbers. "
                   "a metal rack. a metal stand.")
    CARD_MIN_SCORE = 0.35              # a weaker detection fires on most photos ...
    LABEL_MIN_SCORE, LABEL_MAX_BOX = 0.25, 0.01   # ... a small one (this share of the photo) is tried from this score:
                                   # stickers on the stone scored 0.27-0.45, clean stone up to 0.33 (2026-11-01), so a refused
                                   # fill of one stays as shot, like a strap
    CARD_ON_SLAB = 0.6                 # a mark counts when this share of it lies on the slab
    STRAP_ON_SLAB = 0.6                # so does a strap (one sticking out above the slab still has most of it on the slab)
    INK_CHROMA = 15.0                  # writing found in part is completed by its ink: a stroke this vivid (Lab a*b*) ...
    INK_MIN_SHARE = 0.1                # ... when this share of the found mark is that vivid ...
    INK_APART = 20.0                   # ... and its ink this unlike the stone (so a blue stone adds nothing) ...
    INK_SAME = 12.0                    # ... the same ink within this ...
    INK_REACH = 0.15                   # ... this near the found mark (share of the long side)
    INK_MAX_YELLOW = 5.0               # ink no yellower than this (b*): marker blue -17..-31 and logo pink -1.6; an orange clamp +22..+36
    INK_MIN_PIECE = 15e-5              # a stroke at least this share of the photo: written characters 17-270e-5, crystals <= 14e-5
    CARD_MAX_BOX = 0.12                # a detected card or label is at most this share of the photo (else it is the slab)
    RETOUCH_PROMPT = ("Remove the straps, metal racks, sample cards, stickers, labels and handwritten numbers in front of or on the stone, showing the natural stone "
                      "beneath. Keep the stone, its colour, veining and edges and everything else exactly as they are. Do not add anything.")
    RETOUCH_GROW = 0.006               # a found card is widened by this share of the photo's long side
    RETOUCH_GUARD = 0.02               # the stone is compared this far away from what was found and from the slab's edge (share of the long side)
    RETOUCH_BROAD = 0.01               # at this scale, so fine redrawing does not count, only moved colour
    RETOUCH_MAX_DRIFT = 9.5            # the model may shift the stone's colour evenly this much (Lab): on 69 real crops good fills were <= 8.0, repaints >= 10.7
    RETOUCH_MAX_REACH = 0.08           # a fill may reach this far from real stone (share of the long side): straps and labels <= 0.063, card fans >= 0.105
    RETOUCH_MARGIN = 0.05              # the model sees this much around a mark (share of the photo's long side)
    RETOUCH_MIN_CROP = 384             # px: at least this much of the photo, so it sees the stone and not only the mark
    RETOUCH_MAX_ASPECT = 2.5           # and no narrower than this
    RETOUCH_MAX_SCALE = 2.0            # a small crop is enlarged at most this much for the model
    RETOUCH_TONE_SIGMA = 0.02          # a fill's tone coarser than this (share of the long side) is the real stone's around it; finer is the model's
    RETOUCH_RING = 0.02                # the fill is compared with the stone this far around it (share of the long side) ...
    RETOUCH_RING_INSET = 3             # ... both kept this many px inside the slab
    RETOUCH_MAX_COLOUR = 14.0          # its colour may differ this much (Lab) from the real stone around it ...
    RETOUCH_MAX_BUSIER = 2.4           # ... it may be this many times busier (median texture: 110 good real fills <= 1.84, invented 4.4+) ...
    RETOUCH_MAX_SMOOTHER = 2.2         # ... and this many times smoother (good fills down to 1/0.51): more is a smear
    RETOUCH_FEATHER = 2.0              # px: a lifted shadow's edge is softened this much
    RETOUCH_BLEND = 0.006              # beyond a mark, the edit fades into the real stone over this share of the long side
    CROP_ASPECT = (1.3, 2.0)           # a close-up cropped clear of its marks is landscape, this wide over high ...
    CROP_MARGIN = 0.06                 # ... this far from them (share of the long side): a card beside the found ones was missed ...
    CROP_MIN_SHARE = 0.5               # ... and at least this share of the photo, else it is held
    CROP_WORK = 256                    # px: the search runs on the mask at this long side
    RETOUCH_SEEDS = (20261030, 20261031, 20261101)   # a refused removal is tried again with the next draw of the model
    SHADOW_REACH = 0.03                # a mark's shadow is lifted this far from it (share of the long side): a stand's bar cast 0.023
    SHADOW_MIN_LIFT = 3.0              # where the photo is this much darker (L*) than the edit, beyond the edit's own shift ...
    SHADOW_MAX_HUE = 3.0               # ... and its colour (a*, b*) moved less than this: a missed black card moved more
    STUDIO_FRAME = (510.0, 110.0, 900.0)   # the studio camera (base image px): centre x, highest top edge, bottom edge ...
    STUDIO_HEADROOM = 70.0             # ... raised to keep this much white above a tall slab
    STUDIO_EDGE_LIGHT = 0.6            # the sawn edge facing away from the light, this bright relative to the face
    STUDIO_EDGE_STRIP = 200            # the sawn edge is the slab's last (width // this) columns of stone ...
    STUDIO_EDGE_STRIP_MIN = 2          # ... and at least this many
    WHITE_MARGIN = 0.05                # on white, the slab's border (share of the canvas)
    WHITE_SEAM = 4                     # an open book's gap on white (px)
    WHITE_SHADOWS = ((26, 12, 0.30), (5, 3, 0.28))   # blur, drop (px), strength: a soft shadow and a tight contact one
    WHITE_TINT = (20, 41, 73)          # the shadow's colour, the site's navy
    EDIT_DIR = "/app/models/flux2-klein-4b"   # FLUX.2 Klein 4B (Apache-2.0), baked in the :standardise-edit image only
    EDIT_PIXELS = 1_000_000            # the model edits at about this many pixels
    EDIT_STEPS, EDIT_GUIDANCE = 4, 1.0
    EDIT_MAX_TOKENS = 128              # the instructions are short; the encoder's memory grows with this
    _KEEP = ("Keep the stone, its colour, veining, edges and everything else in the photo exactly as they are. "
             "Do not add anything.")
    EDIT_PROMPTS = {
        "strap": "Remove the two straps leaning across the stone slab, showing the slab's natural surface where they were. " + _KEEP,
        "logo": "Remove the company logo and its text from the surface of the stone, showing the natural stone beneath. " + _KEEP,
        "cards": "Remove the fan of sample cards lying on the stone, showing the natural stone beneath. " + _KEEP,
        # The showroom halls (assets/rooms) once: the user wants a matte floor, not a shiny or satin one (2026-11-01).
        "matte_floor": ("Make the shiny polished floor a matte grey concrete floor: no reflections of the lights, windows or "
                        "racks on it, no shine, an even matte surface with fine natural concrete texture. Keep the stone slabs "
                        "on their racks, the walls, windows, ceiling and lights exactly as they are. Do not add anything."),
    }
    TEST_PREFIX = "standardise-test"   # a test run writes under <env>/<this>/, never into improved/
    # The photo process (stone_pipeline.photos.process, STANDARDISE_PROCESS.md) continues in the run tags the published results
    # came from, so no stored verdict, cleaned copy or choice record is orphaned: the standardiser's run over the scraped
    # photos, and the run over the hosted edit's cleaned copies, which also keeps the choice records and the review list.
    # Fixed here, not per deployment: a mistyped tag would re-pay every photo and unpublish the folder. Moving to new tags
    # is a migration (copy the choice records), never a setting. Dev's live choice came from v2-clean, prod's from v2-clean2.
    RUN_TAG = "v2"
    CLEAN_TAGS = {"dev": "v2-clean", "prod": "v2-clean2"}
    CLEAN_TAG = CLEAN_TAGS[ENV_SEGMENT]
    # The slab outline's rule (layout.outline_with_erased): an erased mark is stone only within the stone's own hull, so
    # the wall in a rounded or broken corner never shows (2026-11-02; before it, the fitted corners counted). A whole slab
    # made under it has a note <run folder><this><sha>.json; one without the note was made under the older rule and goes
    # to the GPU once, which checks its outline; drawn differently (the wall in a corner, or the studio's sawn edge
    # beside a broken right corner), it is redrawn as a candidate beside it, judged, and promoted over it only on a pass.
    # A later rule change = a new folder name, and every whole slab is checked once again.
    OUTLINE_CHECKED = "outline-v2/"
    # On: the runner starts the photo process after each full produce (stage all, the owner's button), never after the
    # republish Blokport's Publish button runs (it must never find the run slot taken).
    AUTO = _env_bool("SCRAPER_STANDARDISE_AUTO", False)
    # The process works (GPU rounds, hosted calls) at most WORK_SECONDS, then reports, publishes and relinks in at most
    # FINISH_SECONDS; it refuses to start unless both fit under the runner's ceiling (RUN_TIMEOUT_SECONDS), which kills
    # a run without cleaning up.
    WORK_SECONDS = _env_int("SCRAPER_STANDARDISE_WORK_SECONDS", 7200)
    FINISH_SECONDS = 5400
    # The last JUDGE_WINDOW_SECONDS of the work window are the judge's: a GPU round is stopped this long before the window
    # ends, so what it finished is judged and published in the same run (a round that ran to the window's end left every
    # photo it finished unjudged, and the run published none of them).
    JUDGE_WINDOW_SECONDS = 2400
    LOCK_HEARTBEAT_SECONDS = 300       # the run lock is rewritten this often while the process runs (its sign of life)
    SPEND_DIR = "spend"                # <env>/<TEST_PREFIX>/<this>/<YYYY-MM>/<run id>.json: each run's spend of the month
    CLEAN_SPEND_DIR = "clean-spend"    # the same per run for the cleaning cap, with every paid call listed ("uses")
    # The listings the owner removed on the hidden-listings page (stone_pipeline.photos.decisions): <env>/<TEST_PREFIX>/<this>/
    # <SKU>.json each (percent-encoded), per environment and outside the run tags (a removal is about the listing, not a
    # run's results). The process never works on them and the image stage gives them no standardised photo, so they stay
    # hidden with the existing reason 'no_photo' (a new inventory reason needs Blokport's agreement); the record keeps why.
    REMOVED_DIR = "removed"
    REMOVED_REASON = "owner_removed"
    # The owner's photo choice per listing (stone_pipeline.photos.decisions): <env>/<TEST_PREFIX>/<CLEAN_TAG>/<this>/<SKU>.json
    # (percent-encoded) = {tag, sha, at, listing, decided_on}; it names a result of the process's tags, so it moves with
    # them (a tag migration copies these records with the choice records).
    OWNER_DIR = "owner"
    # The owner's click to clean one photo of a hidden listing: <env>/<TEST_PREFIX>/<CLEAN_TAG>/<this>/<SKU>.json
    # (percent-encoded) = {sku, listing, source, sha, at}. Only the intent: the next photos run cleans it within the cap.
    CLICKS_DIR = "clicks"
    # The review list of the hidden-listings page: <env>/<TEST_PREFIX>/<CLEAN_TAG>/<this> = {SKU: row} of the listings a
    # full photos run left without a good photo and the tiles publish could not render (stone_pipeline.photos.select.
    # review_row); it carries no URL. The config API serves it joined with the ledger (GET /config/v1/photos/hidden),
    # each photo's images as presigned GET URLs made afresh on every request, valid this long (Blokport's page fetches
    # the list again when an image no longer loads).
    REVIEW_FILE = "review.json"
    REVIEW_URL_TTL_SECONDS = 900
    # GET /config/v1/photos/hidden serves the list a page at a time: this many rows unless asked, never more than the max
    # (the per-row work, its reads and its signed images, is done for one page only).
    REVIEW_PAGE_DEFAULT = 50
    REVIEW_PAGE_MAX = 100
    REVIEW_PAGE_DIGITS = 9             # an offset or limit is at most this many ASCII digits (a longer one is a 400)
    # How far ahead of this server's clock an owner's decision may be timed (a browser clock running ahead): a decision
    # timed further ahead is refused, as it would be "later" than every decision made after it on its listing.
    DECISION_AHEAD_SECONDS = 300
    # A photo listings of several varieties or finishes show is never chosen by itself (index.shared_photos): the reason
    # the review list gives for it.
    CONFLICT_REASON = "photo_conflict"
    JOB_PREFIX = "photos-"             # the process's Batch jobs: <this><run id>-..., so a later run finds what one left
    # The GPU's share of the month's budget: a job's running time at this price per vCPU-hour (g4dn.xlarge on demand in
    # eu-west-1, about 0.59 USD/h for its 4 vCPUs). An instance's boot before its first job is not counted.
    GPU_VCPU_HOUR_USD = _env_float("SCRAPER_STANDARDISE_GPU_VCPU_HOUR_USD", 0.147)
    # ... plus this much per job, for what its running time leaves out: the instance's boot, the image pull and the
    # pool's idle tail before it scales in (about 12 min on g4dn with the standardise image).
    GPU_JOB_ALLOWANCE_SECONDS = 720
    GPU_MIN_WINDOW_SECONDS = 1800      # no GPU round starts with less of the work window left (a boot alone takes minutes)
    # A stopped round's jobs are asked every STOP_POLL_SECONDS, at most STOP_WAIT_SECONDS, until they have stopped, so
    # their real stop times are counted; inside the 100 s stop grace on a SIGTERM.
    STOP_WAIT_SECONDS, STOP_POLL_SECONDS = 60, 5
    CUTOUT_WORKERS = 4                 # 3D cut-outs made at once by publish (full-size slabs in memory, on the 6 GB service)
    STORE_WORKERS = 32                 # S3 reads, copies and deletes made at once (records, published files, job stops)
    JPEG_QUALITY = 92                  # every JPEG the process writes, and the photo it sends to an edit model
    AWS_MAX_ATTEMPTS = 10              # a throttled AWS call is retried this many times (botocore standard mode), never fatal
    DIAG_DIR = "diag"                  # the outline diagnostic (stone_pipeline.photos.diag) writes under <env>/<TEST_PREFIX>/<this>/ only
    DIAG_JOINTS = 6                    # it lists this many of the strongest joints in each half of a profile ...
    DIAG_JOINT_MIN = 2.0               # ... scoring at least this (the trim's own bar is SEAM_MIN_END)
    DIAG_LONG_EDGE = 1600              # px: its overlay's long side
    DIAG_LINE_PER, DIAG_LINE_MIN = 500, 2   # its lines: one px per this many px of the photo's long side, at least this
    DIAG_WIDTHS = {"box": 3, "frame": 4, "outline": 2, "trim": 2, "slab": 2}   # in lines
    DIAG_TINT = 0.3                    # the mask's tint
    DIAG_FONT = 0.5                    # text scale per line px
    DIAG_TEXT_AT = (10, 20)            # the reason's place, in lines
    DIAG_COLOURS = {"mask": (0, 255, 0), "detection": (60, 120, 255), "box": (0, 200, 255), "frame": (255, 0, 0),
                    "outline": (255, 230, 0), "joint": (255, 140, 0), "trim": (255, 0, 255), "slab": (0, 255, 0),
                    "text": (255, 255, 0)}
    SHEET_KEY = "scraper/to_upload/3_products_all.csv"   # the product sheet, under <env>/
    # Each listing's own photos in its order ({Product Handle: [[source, sha], ...]}), written by the produce beside the
    # sheet: the sheet's image columns carry only the chosen photo's views once it is published, so the photo process
    # and its GPU job read the listing's photos from here.
    PHOTOS_FILE = "standardise_photo_list.json"      # outside 3_products_*: no importer glob or source name meets it
    PHOTOS_KEY = "scraper/to_upload/" + PHOTOS_FILE
    # The 3D slab (the "Slab 3D on Blokport" contract, version 1): the clean slab a run saved for a whole slab (<sha>.slab.png:
    # a single slab, or one half of an open book, each half a whole slab) is published as a WebP cut-out and a JSON outline;
    # Blokport's viewer extrudes the outline to the product's thickness with the photo on the face. A slab product gets one
    # from its photo, a tile product one rendered from its slab stone (stone_pipeline.photos.publish.publish_tiles); a block
    # or a close-up gets none.
    # The 3D tile (Blokport's viewer, 2026-11-02): the cut-out's horizontal axis is the listing's stored Length, its aspect
    # Length:Width, at least CUTOUT_LONG_SIDE px on its long side (the viewer zooms 4x): the real face resampled
    # (Lanczos), never generated; its meta also records px_per_mm: the stone's real source pixels per millimetre before
    # that resample (0.65 to 1.2 on prod), i.e. how much real detail the texture holds, not the cut-out's own scale.
    CUTOUT_VERSION = 1
    CUTOUT_FORMAT = "slab"             # the product format (format_value, any case) that gets a 3D slab from its photo,
                                       # and whose published 3D slabs are the stone tiles are cut from
    CUTOUT_LONG_SIDE = 2048            # px, the contract's maximum
    CUTOUT_WEBP_QUALITY = 86           # the quality the approved review viewer used
    CUTOUT_ALPHA_CUT = 127             # alpha above this is stone
    CUTOUT_MAX_POINTS = 64             # the contract's outline limit
    # The outline is simplified to this share of its length (the approved viewer), coarser by CUTOUT_EPSILON_STEP until
    # it has at most CUTOUT_MAX_POINTS points: a ragged natural edge keeps its shape, only finer.
    CUTOUT_EPSILON, CUTOUT_EPSILON_STEP = 0.0015, 1.5
    CUTOUT_EDGE_WHITE = 0.25           # the sawn edge: the stone's mean colour mixed with this much white (paler, duller)
    # The last check before a finished photo is published (user 2026-11-01: GPU first, a strict check, the hosted model for
    # the rest): a vision model sees the original and the catalog image and passes or fails it with reasons. Opus matched the
    # user's own labels best (23 of 25 defects, 6 of 6 flagged; Haiku 16, Sonnet 19). One good photo per listing is enough:
    # its photos are judged in order and judging stops at the first pass, at most JUDGE_MAX_PER_LISTING of them.
    JUDGE_URL = "https://fal.run/openrouter/router/vision"
    JUDGE_MODEL = "anthropic/claude-opus-5.5"
    # The bar the prompt sets. 2 (user 2026-11-02, Namib Bianco: "if the final result looks like the FAL image I would
    # accept that, I rather keep the product"): a tiny remnant at the very edge no longer fails. A stored verdict carries
    # the version it was made under; a FAIL made under a lower one is judged again (a pass stays a pass: the bar only
    # moved towards passing, so nothing that passed before can fail now). 3 (owner 2026-11-02, hidden listings whose
    # results looked fine: black-queen 7029-27, bianco-river 7099, amasya 980): the slab's proportions are corrected to its
    # real dimensions on purpose (never wrong_shape), and only what a buyer would see at catalog size fails.
    JUDGE_VERSION = 3
    # An answer of the judge that does not parse is asked again, once a run, until it has been asked this many times;
    # then it is a final fail ("the judge's answer could not be read") and its listing goes to the review list.
    JUDGE_UNPARSED_ASKS = 2
    JUDGE_REASONING = True             # the endpoint refuses this model without it
    JUDGE_MAX_TOKENS = 6000            # reasoning included
    JUDGE_LONG_SIDE, JUDGE_JPEG_QUALITY = 1024, 88   # each image is sent at this long side
    JUDGE_TIMEOUT = 300                # s per call
    # A run over the listings still without a good photo goes further down each listing (prod 2026-11-02: 365 such listings
    # had 2,025 photos past the third, never tried): a photo costs a GPU pass and one judge call, far less than an edit.
    JUDGE_MAX_PER_LISTING = _env_int("SCRAPER_STANDARDISE_JUDGE_MAX", 3)
    JUDGE_KINDS = ("light_reflection", "scene_reflection", "bad_fill", "leftover_mark", "not_one_slab", "wrong_shape", "other")
    JUDGE_PROMPT = """You are the last quality check before a natural-stone product photo is published in a marketplace catalog.
Image 1 is the supplier's original photo. Image 2 is our processed catalog image of it.
- For a SLAB (or two bookmatched slabs side by side): the slab is cut out of image 1, squared, cleaned and placed
  upright in a showroom. Its chrome feet / supports at the bottom belong to the showroom stand. The whole slab is also
  stretched or squeezed to its real dimensions, so its width-to-height ratio may differ from image 1: that alone is
  never wrong_shape. But every edge and corner the slab has in image 1 must still be in image 2.
- For a BLOCK or TILE: image 2 is the product cleaned in its own surroundings; there is no showroom. A quarry
  block's painted numbers and markings, its lifting cables and chains, and the quarry or yard around it are normal
  and never a problem.
Judge ONLY the stone product in image 2 (its surface, its outline and what is on it), using image 1 to tell what is
real stone and what is not. The showroom room, floor, stand and feet are fixed and never a problem.

Look at image 2 as a buyer sees it at catalog size (a card about 400 pixels wide). FAIL it only if that buyer would
clearly notice any of these on the stone:
- light_reflection: lamp spots, rows of ceiling lights, light tubes or bright glare mirrored on the polished surface
- scene_reflection: windows, the hall, people or objects clearly mirrored in the stone
- bad_fill: a patch, rectangle or smear whose tone, brightness or texture does not match the stone around it
  (where a label, logo, card or strap was removed)
- leftover_mark: a label, sticker, card, logo, writing, tape, protective film or strap still on the stone (a tiny
  remnant at the very edge of the slab, hardly visible at catalog size, is not a problem)
- not_one_slab: two or more slabs shown as one (stacked with an offset, a second slab's edge visible), or another
  stone taken in
- wrong_shape: the product cut off, part of it missing, warped, or something in front of it cut into its outline
  (for a SLAB, the whole slab at a different width-to-height ratio is the scaling and never wrong_shape; an edge,
  corner or strip of the slab in image 1 that is missing from image 2 is wrong_shape)
- other: any other clear defect of the image (not of the stone itself)

PASS when the stone looks clean and natural at catalog size. Natural features of the stone (veins, cracks, holes,
colour changes, breccia, resin, sheen, rough or chipped edges) are not problems, nor is a faint reflection, soft sheen
or small faded spot a buyer would not notice at catalog size.
Your whole answer is one JSON object and nothing else:
{"verdict": "pass" | "fail", "problems": [{"kind": "<one of the kinds>", "where": "<short>"}]}"""
    # A listing with no photo that passes: the hosted edit models clean ONE of its photos (the first that can be fixed by
    # cleaning) in turn until a copy passes the clean check; that copy then goes through the standardiser like any photo and
    # the judge again. Order by cost per passed copy (prod run 2026-11-02, 1,013 photos): Nano Banana 2 ($0.12) passed 55%
    # even on the photos Kontext had failed, Kontext ($0.15) 31% first, FLUX 3 ($0.15) 37% last; the cheapest and most
    # successful goes first (the three in turn reach the same photos either way). The queue, not the sync gateway: a slow
    # model outlasts it (504) and the paid call is lost.
    CLEAN_QUEUE_URL = "https://queue.fal.run"
    # Straps too (sample 2026-11-01: 27 of 42 failures were the GPU's own strap fills, light bands and clamp remnants, on
    # copies the hosted edit had otherwise cleaned): the hosted edit takes off everything foreign, so the retouch finds
    # nothing left to fill.
    CLEAN_INSTRUCTION = ("Remove only these from the polished stone slab: lifting straps, belts and their corner clamps or "
                         "protectors; reflections of lamps, light tubes, windows and the room mirrored on its surface; and any "
                         "label, sticker, sample card, tape, protective plastic film, writing or logo on it. Where they were, "
                         "show the slab's own stone, "
                         "continuing its pattern. Keep the stone's colour, veins, pattern and texture exactly as they are, and keep "
                         "the slab's outline and the rest of the photo unchanged.")
    # The instruction's version (2: protective film too). A stored attempt that failed under a lower one is made again with
    # the current instruction; a passed one stands.
    CLEAN_VERSION = 2
    CLEAN_MODELS = (                   # (name, endpoint, extra input); the photo goes in as image_url or image_urls
        ("nano2", "fal-ai/nano-banana-2/edit", {"resolution": "2K", "output_format": "jpeg", "seed": 1}),
        ("kontext", "fal-ai/flux-pro/kontext", {"output_format": "jpeg", "seed": 1}),
        ("flux3", "blackforestlabs/flux-3/edit-image", {"resolution": "1.5k", "output_format": "jpeg"}),
    )
    CLEAN_SINGLE_IMAGE = ("kontext",)  # these take one image_url; the others a list, image_urls
    CLEAN_SEND_LONG = 2048             # the photo is sent at this long side
    CLEAN_TIMEOUT, CLEAN_POLL = 600, 3 # s: give up on a queued edit after this long, asking every this many s
    CLEAN_COLLECT_TIMEOUT = 60         # s: one status, result or image request of a queued edit
    # Problems a cleaned copy can fix; a photo held or failed for anything else (an outline the standardiser could not
    # find, stacked slabs) is not worth an edit.
    CLEAN_FIXES_KINDS = ("light_reflection", "scene_reflection", "bad_fill", "leftover_mark")
    # Held reasons (their start) a cleaned copy removes the cause of. "logo:" is no longer written (the logo step is
    # retired: a logo is the judge's leftover_mark now), but holds the retired step stored stay on S3 per photo (prod
    # v2: 100 varsha photos, 2026-11-02) and a cleaned copy fixes them, so they stay cleanable.
    CLEAN_FIXES_HELD = ("retouch:", "logo:")
    # What is cleaned without asking (owner 2026-11-05): every mark on the stone, the judge's leftover_mark (labels,
    # stickers, cards, writing, logos, tape, straps, film) and the holds the retouch or the retired logo step left for a
    # mark. Any other fixable photo (a reflection, a bad fill) is cleaned only when the owner clicks it (CLICKS_DIR).
    CLEAN_AUTO_KINDS = ("leftover_mark",)
    CLEAN_AUTO_HELD = ("retouch:", "logo:")
    CLEAN_FAILED_CODES = (400, 422)    # the model refused the photo for good (at submit or collect): the next model's turn
    # The statuses a hosted call (an edit or the judge) is known to be answered with BEFORE any work is done, so nothing
    # is billed: a malformed or unprocessable request, auth, balance, forbidden, unknown endpoint, too large, rate limit.
    # Any other failure (408 among them: OpenRouter's request timeout can follow upstream work) counts at its price.
    HOSTED_REFUSED_CODES = (400, 401, 402, 403, 404, 413, 422, 429)
    CLEAN_CHECK_PROMPT = """Image 1 is a supplier's photo of a natural-stone slab. Image 2 is an edited copy meant to remove lifting
straps with their clamps, reflections (lamps, light tubes, windows, the room mirrored on the polished stone) and
foreign marks (labels, stickers, sample cards, tape, protective film, writing, logos) from the stone, and nothing else. Judge image 2
on two questions:
1. clean: are all such straps, clamps, reflections and marks gone from the stone in image 2? (true/false)
2. faithful: is the stone itself the same stone as in image 1 (same colour, veins, pattern and texture, nothing
   invented, nothing smeared or blurred, slab outline unchanged)? (true/false)
Your whole answer is one JSON object and nothing else:
{"clean": true|false, "faithful": true|false, "problems": "<short, empty if none>"}"""
    # The month's (UTC) budget of an environment's photo process, all runs together: the judge (as billed) and GPU time
    # (GPU_VCPU_HOUR_USD). A call or a GPU round that would take the month's total past it is not made. No default: unset,
    # the process does not start.
    BUDGET_USD = _env_float("SCRAPER_STANDARDISE_BUDGET_USD", None)
    # The month's cap of the hosted clean-up edits and their clean checks, all runs together, kept apart from BUDGET_USD
    # (owner 2026-11-05: the cleaning has its own cap). An edit is reserved with its check, so a copy paid for is always
    # checked in the run that paid for it. No default: unset, no photo is sent to an edit (the run says so once).
    CLEAN_BUDGET_USD = _env_float("SCRAPER_STANDARDISE_CLEAN_BUDGET_USD", None)
    # A judge call's cost reserved before it is made: its worst case (JUDGE_MAX_TOKENS out at $20/M plus two images in at
    # $4/M, Opus 5.5), so parallel calls can never together pass the budget; Opus bills ~0.016 on average.
    JUDGE_PRICE_USD = 0.14
    CLEAN_PHOTOS_PER_LISTING = 1       # one good photo is enough: only the first fixable photo of a listing is cleaned
    # Off: no photo is cleaned without asking (the automatic marks, CLEAN_AUTO_*); the owner's click still is, within the
    # cleaning cap (owner 2026-11-05). An edit already paid for is always collected and checked.
    CLEAN_ENABLED = _env_bool("SCRAPER_STANDARDISE_CLEAN", True)
    # The process (stone_pipeline.photos.process) sends the GPU work to AWS Batch in slices of RUN_SLICE photos, each a retouch job
    # then a standardise job. The GPU pool's ceiling is raised to RUN_GPU_VCPUS while it works and always set back to
    # IDLE_GPU_VCPUS after (the pool scales to zero when idle; the ceiling caps what a burst can cost).
    # The size of a run's GPU burst is set per run (a full environment overnight wants more GPUs and smaller slices than
    # a test): the ceiling is the account's G-instance quota at most, and is always set back to IDLE_GPU_VCPUS.
    # A slice's photos run one after another on one GPU, about 100 s each for retouch and standardise together, so a slice
    # of 30 ends inside the GPU rounds' part of the work window (WORK_SECONDS - JUDGE_WINDOW_SECONDS) with its boot, and a
    # backlog spreads over the pool's GPUs instead of queueing on a few.
    RUN_SLICE = _env_int("SCRAPER_STANDARDISE_RUN_SLICE", 30)
    RUN_GPU_VCPUS = _env_int("SCRAPER_STANDARDISE_GPU_VCPUS", 128)
    # The idle ceiling is Terraform's gpu_idle_vcpus, the value it also creates the pool with: no default here, so the two
    # can never disagree. None off AWS; setting the pool's ceiling without it fails loud (stone_pipeline.photos.batch).
    IDLE_GPU_VCPUS = _env_int("SCRAPER_STANDARDISE_IDLE_GPU_VCPUS", None)
    RUN_POLL_SECONDS = 60              # how often a run asks Batch whether its jobs are done ...
    RUN_MAX_WAIT_SECONDS = 12 * 3600   # ... and how long at most per wave of jobs (the slices that run at once) before it
    #                                    stops them and fails (jobs stuck, no GPU quota); above a healthy slice's longest
    RETOUCH_TIMEOUT, STANDARDISE_TIMEOUT = 14400, 21600   # s a Batch job may take before Batch stops it
    GPU_JOB_VCPUS = 4                  # each GPU job's vCPUs (the standardise job definition: 4 vCPU, 1 GPU, no retries):
    #                                    RUN_GPU_VCPUS // this jobs run at once
    SLICE_MAX_STORAGE_FAILURES = 3     # storage errors in a row before a GPU slice stops (the next run retries)
    SELECT_WORKERS = _env_int("SCRAPER_STANDARDISE_WORKERS", 12)   # listings worked on at once (hosted calls only)
    # Per edit at the sizes above, an upper bound (fal.ai/pricing: Kontext per megapixel rounded up, Nano Banana 2 per image
    # with 2K above base, FLUX 3 per megapixel), so the cap stops early rather than late.
    CLEAN_PRICE_USD = {"kontext": 0.15, "nano2": 0.12, "flux3": 0.15}
    # The revision each model is baked at in the image (Dockerfile): the jobs run offline, so "latest" cannot resolve.
    SAM_REVISION = "87aecf0df4ce6b30cd7de76e87673c49644bdf67"
    DETECTOR_REVISION = "12bdfa3120f3e7ec7b434d90674b3396eccf88eb"
    CLIP_REVISION = "3d74acf9a28c67741b2f4f2ea7635f0aaf6f0268"
    BLEND_REVISION = "115134f363124c53c7d878647567d04daf26e41e"
    BLEND_VAE_REVISION = "207b116dae70ace3637169f1ddd2434b91b3a8cd"
    CLIP_MODEL = "openai/clip-vit-base-patch32"
    BLEND_MODEL = "diffusers/stable-diffusion-xl-1.0-inpainting-0.1"   # Open RAIL++-M
    BLEND_VAE = "madebyollin/sdxl-vae-fp16-fix"   # the stock SDXL VAE overflows in fp16 (black images)
    BLEND_PROMPT = ("photograph of a large polished natural stone slab standing on a black steel stand in a stone showroom, sharp "
                    "natural slab edge, realistic contact shadow on the polished concrete floor")
    BLEND_NEGATIVE = "halo, glow, blur on the slab, text, logo, people, cgi, 3d render, illustration"
    BLEND_SIZE = (1216, 832)           # an SDXL training bucket, the canvas's shape
    BLEND_STEPS, BLEND_GUIDANCE, BLEND_STRENGTH = 30, 5.0, 0.6
    BLEND_SEED = 20261030              # fixed: same input, same output
    BLEND_FEATHER = 2.0
    BAND_OUT, BAND_IN = 44, 4          # the repainted band: this far outside the slab and this far inside it (px)
    BAND_FADE = 0.7                    # it fades out over this share of its width, so no rectangle shows on the floor
    BAND_CHIP_SOFT = 2.0               # a real chip is never repainted (edge softness, px)
    OPAQUE_MAX_CHANGE = 12             # a pixel changing less than this with the room behind it is slab or stand
    STAND_MIN_CHANGE = 6               # a pixel changing more than this without the stand is the stand or its shadow
    PHOTO_KINDS = {
        "slab": "a photo of one large polished stone slab standing upright, the whole slab in view",
        "close-up": "a close-up photo of the surface texture of polished stone",
        "open book": "a photo of two bookmatched stone slabs standing side by side",
        "pile": "a photo of many stone slabs stacked in piles or racks in a warehouse",
        "hangar": "a wide photo of a warehouse or factory hall",
        "interior": "a photo of a finished kitchen, bathroom or room with stone surfaces",
        "other": "a document, logo, price list or chart",
    }
    SKIP_KINDS = ("pile", "hangar", "interior", "other")
    SKIP_MIN_CONFIDENCE = 0.6          # only a sure one is skipped here; the outline's checks catch what is left
    CLOSEUP_SHARE = 0.45               # a close-up shows this share of the slab's width when the photo has the pixels for it ...
    CLOSEUP_MAX_ENLARGE = 1.0          # ... else it widens: at most 1:1 of the photo's own pixels, never enlarged (the owner,
                                       # 2026-11-02, only real detail; any enlargement, ours or a viewer's, shows the
                                       # interpolation, not the stone) ...
    CLOSEUP_MIN_WIDTH = 1400           # ... until it holds this many of them across: stored at its own size (at most the 2048 px
                                       # canvas, made smaller with an area filter, never larger) in the canvas's 3:2 shape.
                                       # Blokport draws every image contain-fit, scaled to the frame, never at 1:1; its 3:2
                                       # frames are drawn up to 1328 device px wide (product page, DPR2 desktop), 1068 (iPhone
                                       # DPR3), 1148 (quick view), 1350-2700 (full-screen viewer): narrower than 1400 x 933
                                       # (Blokport's minimum, 2026-11-02) the frame would enlarge it, so it has none ...
    CLOSEUP_MAX_SHARE = 0.9            # ... up to this share of the slab on either axis; needing more: no close-up
    CLOSEUP_DENSITY_SAMPLES = 5        # the squaring's local scale is read on this many points per axis (corner to corner)
    CLOSEUP_ALIAS_RATIO = 1.25         # a close-up whose near side has more than this many times its far side's photo pixels is
                                       # squared at the near side's scale, then made smaller (else its finer stone folds into
                                       # false bands: 9.1 at 2x); below it, straight at the far side's, as a square-on shot is: a
                                       # reduce of a few % acts as a 2 px blur and takes a fifth of the finest detail (12 px
                                       # skew, sen-1547ed34: 18.6 -> 14.5), while what folds at 1.25x lands near the finest
                                       # scale, never in broad bands
    # A result made before its chosen slab was recorded (<sha>.slab.json) is decided against the <sha>.slab.png it saved:
    CLOSEUP_SLAB_SIZE_TOL = 0.03       # a slab found now is its size as drawn within this share on either axis (a fresh outline
                                       # moves a few px; a single slab and an open book's half differ about twice) ...
    CLOSEUP_SLAB_MATCH_WIDTH = 64      # ... and shows its stone: both compared this many px across (coarse: a fresh outline's
                                       # drift of 1% then moves it under a pixel) ...
    CLOSEUP_SLAB_MATCH_SMOOTH = 4.0    # ... their light and broad shading taken off (a blur of this many px subtracted) ...
    CLOSEUP_SLAB_MATCH_MIN = 0.5       # ... correlating at least this much ...
    CLOSEUP_SLAB_MATCH_MARGIN = 0.2    # ... and by this much more than an open book's other half (its mirror image); else none.
                                       # 12 real stones bookmatched, the outline drifted 1%: the same half 0.63 to 0.93, the
                                       # mirrored one at most 0.34, the least gap 0.32
    CLOSEUP_EDIT_MAX = 0.01            # a close-up more than this share not as shot (a logo, erased label, retouch fill, a hosted
                                       # model's cleaned copy) is none: a made-up patch shows at 1:1
    CLOSEUP_RETOUCH_DIFF = 8.0         # a retouched copy's pixel this much off the photo it was made from (8-bit luma, ...
    CLOSEUP_RETOUCH_SMOOTH = 2.0       # ... smoothed over this many px) was filled: luma only, as a q95 4:2:0 re-encode moves
                                       # colour, not luma (measured 2026-11-02 on 55 real photos: 0 px flagged; colour too
                                       # flagged up to 0.39%), a fill of other stone found at a median of 100%
    CLOSEUP_DETAIL_BANDS = ((0.18, 0.25), (0.28, 0.40))   # cycles/px: the band just under half a photo's resolution and the
                                       # one above it, where a soft photo has little of its own ...
    CLOSEUP_DETAIL_SMOOTH = 8.0        # ... their local energy smoothed over this many px ...
    CLOSEUP_DETAIL_STEP = 8            # ... and compared every this many px
    JPEG_BLOCK = 8                     # px: a JPEG photo's block grid, whose frequencies the detail score leaves out ...
    CLOSEUP_JPEG_GRID_BINS = 1.5       # ... this many FFT bins either side
    CLOSEUP_DETAIL_MIN = 0.40          # the upper band's energy rises at least this much with the lower's across the crop, on the
                                       # photo's own pixels, else too soft to show at 1:1: no close-up. Calibrated 2026-11-02 on
                                       # 55 real photos (gate/good, even ones; checked on the odd ones) against each enlarged 2x
                                       # by bilinear and bicubic, plain and unsharp-masked, JPEG q60 and q85 (clearly soft at
                                       # 1:1): those pass 1.8% / 4.2% pooled, real photos 79% / 78%. Accepted residual risk: a
                                       # vendor's own enlargement shown at 1:1, with no enlargement of ours, the same pixels the
                                       # main view shows; per kind (calibration / held-out) bilinear 2x passes 7% / 15% (about
                                       # 20% in the review's crop harness), bicubic and bilinear 1.25x-1.5x up to 39% / 30%,
                                       # nearest-neighbour at any factor 68-100% (blocky but real pixels). No detector tells
                                       # these from a downscale or a crop without losing the sharpest photos
    CLOSEUP_DETAIL_SPREAD_MIN = 0.15   # the lower band's energy varies at least this much across the crop (std / mean), else
                                       # detail cannot be told from noise (real calm stone: 0.32 and up)
    CLOSEUP_DETAIL_ENERGY_MIN = 0.1    # the lower band's mean energy (8-bit grey levels squared) at least this, else nothing to
                                       # judge: a stone smoothed to its 8-bit steps (0.01; real calm stone 2.8 and up)
    CLOSEUP_VERSION = 2                # raised when the close-up's rule changes: the close-only run makes every close-up again
    CLOSEUP_RETRY_MAX = 3              # a photo a passing storage error left to the next run this many runs in a row has none
    CLOSEUP_HOLD_STREAK_MAX = 25       # this many photos in a row with no close-up for one kind of reason stop the slice
                                       # unwritten: a cause outside the photos (inputs gone from storage, a broken model),
                                       # not the photos; photos off the product sheet are an outcome en masse and never count
    SEAM_BAND = (0.46, 0.54)       # an open book is two equal slabs from one block: its seam is central
    SEAM_MIN = 6.0                 # robust z-score of a joint column (edge change + dark gap)
    JOINT_EDGE = 0.01              # joints are looked for inside this margin of the outline's ends
    JOINT_NMS = 0.03               # two joints closer than this share of the width are one
    SLIVER_MAX = 0.12              # at a frame-touching end, a joint within this share cuts off a neighbour's edge
    SEAM_MIN_END = 4.0             # ... at this lower score (a slab meeting a pale post is a soft joint)
    MIRROR_MIN = 0.08              # vein mirror correlation across the seam (single slabs: ~0.01)
    JOINT_BAND = 0.25              # mirror test on this share of each slab's width at the joint
    JOINT_HEIGHT = 384             # both slabs resampled to this height for the joint test
    JOINT_SIGMA = (0.007, 0.04)    # band-pass of the veining compared at the joint, share of the height
    JOINT_MIRROR_MIN = 0.15        # mirrored joint correlation of a bookmatch (Sentas pair: 0.27)
    JOINT_MARGIN = 0.10            # and at least this much above the unmirrored control
    PAIR_ASPECT = 2.4              # an outline this wide is two slabs: a strong seam alone splits it
    SAME_IOU = 0.5                 # a probed mask overlapping ours this much is our own slab, not a partner
    NEIGHBOUR_GAP = 0.04           # the partner's near edge within this share of the slab width of ours
    BESIDE_GAP = 0.15              # a separate slab beside ours (a rack's next slab, a pole between: 39212b82 ~0.10) within this:
                                   # it explains a detector box reaching past our slab
    NEIGHBOUR_SIZE = (0.88, 1.12)  # and its width and height within this ratio of ours (equal slabs)
    DETECTOR = "IDEA-Research/grounding-dino-base"   # Apache-2.0 open-vocabulary detector, our GPU
    DETECT_PROMPT = "a stone slab."
    DETECT_MIN = 0.3               # detector box confidence
    EXTEND_MAX = 0.10              # the detector's box may reach past our outline by at most this share of its size,
                                   # else the outline is partial (straps in front, a rough natural edge)
    STRAP_PROMPT = "a wooden plank. a strap."
    STRAP_MIN_SCORE = 0.20         # detector confidence for a strap (lower than for slabs: the shape tests below guard)
    STRAP_PAD = 0.012              # strap mask widened by this share of the photo's long side (edges, shadow)
    STRAP_MIN_HEIGHT = 0.6         # a strap runs along at least this share of the slab's height ...
    STRAP_MAX_WIDTH = 0.25         # ... is at most this share of its width (else it is the slab itself) ...
    STRAP_ELONGATION = 6.0         # ... and its mask is at least this many times longer than wide
    STRAP_PIECE_MIN = 0.02         # slab pieces left beside the straps count when this share of the slab
    FRAME_EDGE = 0.01              # a slab corner this close (share of the photo) to a frame edge its stone runs into: cut off
    FRAME_TOUCH = 0.02             # the stone runs into a frame edge when it covers this share of that edge's outermost line
                                   # (measured 2026-11-01: cut off 3.8-74%, framed tightly 0%)
    # --- outline ---
    ROOM_COUNT = 3                     # rooms/<1..n>.jpg, and far_<1..n>.jpg (the same hall from twice as far, for an open book)
    FAR_ROOM_BLUR = 3.0                # px: the far hall softened like a background behind slabs in focus
    OUT_W, OUT_H = 2048, 1365           # every image: the storefront's 3:2 frame (card, product page, viewer) at the 2048 px it stores
    MARGIN = 0.035                     # showroom border left, right and above the slab(s), share of OUT_W
    FLOOR_SHARE = 0.13                 # the one floor line: floor below every slab, share of the canvas height
    SHADOW = 0.35                      # contact shadow strength under each slab
    TWO_STONES_GAP = 150.0             # a cut-out whose light and dark halves differ by more than this (8-bit Lab) ...
    TWO_STONES_PIECE = 0.05            # ... with the odd half's largest piece this share of it ...
    TWO_STONES_BORDER = 0.3            # ... running along this share of its border took in another slab (178/0.72 vs <=119/0.35 on 72)
    TWO_STONES_WORK = 400              # px: judged at this long side ...
    TWO_STONES_BLUR = 3.0              # ... on the broad colour, not the grain
    CONTACT = 0.6                      # under the slab the floor is this much darker (no light gets there) ...
    CONTACT_DEPTH = 0.04               # ... over this share of the canvas height ...
    CAST = 0.55                        # ... and a soft shadow spills past the slab's ends: this strong ...
    CAST_REACH = 0.07                  # ... reaching this share of the canvas width
    TRIM_REACH = 0.03                  # an edge is tightened by at most this share of the slab
    TRIM_BAND = 0.03                   # the stone it is compared with: this wide, just inside that reach ...
    TRIM_SMOOTH = 0.05                 # ... averaged along the edge over this share of it
    TRIM_UNLIKE = 11.0                 # unlike the stone from this colour difference (Lab) ...
    TRIM_L_WEIGHT = 0.35               # ... brightness counting this much (an edge's light and shade are still the stone)
    TRIM_OWN, TRIM_OWN_PCT = 1.2, 98   # ... and from this many times what the stone itself varies by (this percentile), so a busy stone is left alone
    TRIM_SOFT = 0.006                  # the cut is smoothed along the edge over this share of it
    TRIM_LINE, TRIM_LINE_PCT = 1.6, 99.5   # a physical edge is this many times this percentile of the stone's own change further in
    TRIM_MIN_LENGTH = 0.2              # a cut must run along at least this share of the edge, else it would leave a nick
    TRIM_RUN = 0.03                    # a cut must run along this share of the edge (a vein crossing the edge does not)
    NOTCH_FILL = 0.08                  # bites in an edge narrower than this share of the slab's height are filled
    OUTLINE_CLOSE = 0.012              # notches thinner than this share of the photo's long side are filled, wider ones are real
    OUTLINE_HOLLOW_MAX = 0.02          # a cut-out missing more than this share of its convex hull has a stand in front of it
                                       # (measured 2026-11-01 on 320 slabs: normal <= 0.0064, a stand cut out 0.108-0.153)
    EDGE_INSET = 2                     # the outline is taken this many px inside the photo's mixed edge pixels
    EDGE_SOFT = 0.6                    # the slab's edge softness (px): crisp
    EDGE = 0.45                        # the slab's bottom edge face (its thickness): this bright, relative to the stone ...
    EDGE_HEIGHT = 0.004                # ... this high, share of the canvas height
    FOOT_FADE = (0.10, 0.03)           # the stone's lowest part, near the floor, gets less light: by this much, over this share of the height
    AMBIENT_FALL = 0.035               # the slab's soft shade on the floor in front of it, share of the canvas height
    JOINT = 3                          # an open book's seam: this many dark pixels between the two slabs
    JOINT_COLOUR = (28, 28, 30)
    JOINT_TRIM = 0.006                 # each slab's edge face at the seam (catches the light) trimmed, share of its width
    DETAIL_SCALE = 0.8                 # ESRGAN contributes detail finer than this many source pixels
    ESRGAN_TILE = 512                  # ESRGAN input tile (bounds GPU memory on large slabs)
    # --- layout ---
    DEFAULT_SLAB = (3.04, 1.87)        # median prod slab L x W (m): proportions for test photos with no product
    CLOSEUP_BORDER = 0.70              # stone along at least this share of EACH photo border: a close-up ...
    CLOSEUP_INSET = 0.02               # ... read this share in from the edge (SAM's masks stop a few px short of it)
    CLOSEUP_FLOOR_PROMPT = "a floor."  # a close-up shows no floor: the detector boxes its stone as "floor" from the top ...
    CLOSEUP_FLOOR_MIN = 0.35           # ... (real close-ups 0.41-0.91, box from the top), but under a slab the real floor
    CLOSEUP_FLOOR_TOP = 0.30           # ... starts lower (best box from 0.46-0.76 H on 56 Sentas warehouse photos)
    SHARP_MIN = 15.0                   # variance of the Laplacian at 1024 px below this: blurred, skipped (calm sharp marble ~32, blurred ~4)
    # --- closeup ---
    BLOCK_PROMPT = "a stone block."
    CABLE_PROMPT = "a rope. a cable."
    BLOCK_MIN_SHARE = 0.10             # a detected block must cover this share of the photo to measure light on it
    CABLE_ELONGATION = 6.0             # a cable mask is at least this many times longer than it is wide
    CABLE_MAX_SHARE = 0.04             # ... and no larger than this share of the photo (not the block itself)
    CROP_NOTE_MIN = 0.01               # the log says so when the canvas crop cuts more than this share of a block
    # --- scene ---
    # --- tiles (stone_pipeline.photos.publish.publish_tiles: a tile listing's views and 3D tile, from its slab stone) ---
    # The owner (2026-11-02): a tile listing is shown from the same variety's judge-passed, published slab stone (its 3D
    # cut-out), never from the vendor's tile photos (warehouse racks), never generated; CPU only, no judge per tile.
    TILE_FORMAT = "tile"               # the format whose listings are rendered here, never sent through the photo process
    VARIANTS_KEY = "scraper/from_medusa/variants_export.csv"   # under <env>/: the Medusa variations (Id, Name) ...
    ATTRIBUTES_KEY = "scraper/from_medusa/attributes.csv"      # ... and attributes (category, value, sourceid)
    TILE_RENDER_VERSION = 3            # part of every tile's made-from stamp: a new version re-renders every tile
                                       # (2: the owner's view set, the laid tiles from above as the main view)
                                       # (3: every tile leads with the laid tiles, however few its faces)
    # A tile is rendered at its listing's own size only when that size is a tile's: each side and the thickness within
    # these bounds (metres). Outside them the size is a data error, never drawn: no images, the reason in the summary.
    # A size the produce filled from the pack's defaults (dimension_defaulted) is never shown either (the image stage).
    TILE_SIDE_RANGE_M = (0.1, 1.6)
    TILE_THICKNESS_RANGE_M = (0.008, 0.05)
    TILE_FAILED_LISTED = 20            # the run record names at most this many tile listings whose step failed
    TILE_JPEG_QUALITY = 92
    # The stone: the slab's outer rim (sawn edge, mixed edge pixels) and its lamp reflections are never cut. Its scale is
    # the listed size over the cut-out's px, so the cut-out must be at the listed proportions within this share (a tile
    # then shows within about half of it of its real size); else the scale would be a guess and the slab is not used.
    # A cut-out keeps the proportions its photo measured (SIDE_RATIO_MEASURED): prod 2026-11-02, 544 published slabs,
    # 62 off by more than 5%, 24 by more than 10%.
    TILE_STONE_INSET_M = 0.03
    TILE_ASPECT_TOLERANCE = 0.05
    # The cut-out check: a regular crosshatch (a mesh, a screen) over the face prints onto every tile, with no judge to
    # catch it. Straight lines along BOTH of the cut-out's axes put the face's fine detail on both axes of its spectrum;
    # stone is never like that (veins and grooves run one way). Measured 2026-11-02 on 510 prod cut-outs: Tiger Black
    # 805cf5ac (mesh over the whole face) 17.6; every other cut-out at most 1.97, linear and grooved stone included.
    TILE_GRID_WINDOW, TILE_GRID_STEP = 128, 64   # px: the spectrum is averaged over windows this big, this far apart ...
    TILE_GRID_INSET_PX = 20            # ... lying wholly this far inside the stone ...
    TILE_GRID_DETAIL_SIGMA = 3.0       # ... of the detail finer than this (px) ...
    TILE_GRID_BAND = (4, 0.45)         # ... over the frequencies from this many cycles per window to this share of the window
    TILE_GRID_OFF_AXIS = (3, 6)        # an axis is compared with the frequencies this many bins beside it
    TILE_GRID_MAX = 4.0                # both axes this many times their neighbours: a crosshatch, the slab is not used
    # Laying: 3 mm joints (the norm for natural stone floors), straight for a square, half-bond for a rectangle.
    TILE_JOINT_M = 0.003
    TILE_JOINT_GREY = (205.0, 70.0)    # grout grey for a light / a dark stone, mixed with the stone's own mean ...
    TILE_JOINT_STONE_SHARE = 0.35      # ... this much
    TILE_JOINT_DARK_STONE_L = 45.0     # a stone darker than this L* gets the dark grout
    TILE_BEVEL_M, TILE_BEVEL_DARK = 0.0015, 0.18   # each tile's eased edge darkens this much over this distance
    TILE_PATTERN_ORIGIN_M = (0.37, 0.11)            # the pattern's offset, so no joint runs through the image's centre
    TILE_CUT_STEP, TILE_ROW_SEED = 7, 20261102      # neighbours along a row step by this many faces; rows by a hashed offset
    TILE_LEVELS = 6                    # texture pyramid levels for minified (far) tiles
    TILE_SUPERSAMPLE = 2               # the studio view is drawn this many times larger, then reduced
    # The single tile on white (white view) and its 3/4 studio view.
    TILE_WHITE_MARGIN = 0.12
    TILE_MAX_UPSCALE = 2.5             # the single-tile views never enlarge the real pixels more than this
    TILE_SIDE_ANGLES = (32.0, 24.0)    # azimuth, elevation (degrees) of the 3/4 view of one tile lying flat
    TILE_SIDE_DISTANCE = 6.0           # camera distance in tile lengths: a long lens, little perspective
    TILE_SIDE_MARGIN = 0.14
    TILE_SIDE_EDGE_LIGHT = (0.62, 0.78)   # the two visible sawn edges, this bright relative to the face
    TILE_SIDE_EDGE_PX = 64             # px across the thickness of the stretched edge strip
    TILE_SIDE_SHADOW = ((40, 18, 0.30), (6, 4, 0.30))   # blur, drop (px), strength: soft + contact shadow
    TILE_CLOSE_MAX_UPSCALE = 2.0       # the close view never enlarges the real pixels more than this ...
    TILE_CLOSE_MIN_TILES = 1.6         # ... and shows at least this many tile lengths (a joint for scale)
