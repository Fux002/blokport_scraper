"""The seed's source errors are corrected AT THE SOURCE (Mas's ruling 13 Nov, under the owner's rule: fix bad data
where it is produced, never a one-off rule or a downstream patch): both committed bases and the backbone hold the real
terms, and no correction table or reader remains. The errors are the 48 traced on 13 Nov: framing fragments with no
stone name ("In China Ston Emarket", "In Russian"), concatenations ("Tropical Siena Bordeaux Granite White River
Granite"), mojibake residue ("K R Ehir Nero Picasso"; "C Te D" undeterminable, dropped for the owner) and a former type
name ("Dolomite Marble"). Values compare by alias_key. No em dashes."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from stone_pipeline.core.text import alias_key

REPO = Path(__file__).resolve().parents[2]
BASES = [REPO / "from_medusa" / env / "variants_export_base.csv" for env in ("development", "production")]
BACKBONE = REPO / "catalog_source" / "backbone_varieties.json"

# (Key, the source error): none may remain in any seed input
SOURCE_ERRORS = [
    ('basalt__menggu_black_917cdd2a-c0b2-5bb7-9645-bace40583fdb', 'In China Ston Emarket'),
    ('granite__barbara_white_4a649260-074b-50ab-a8d8-1ab679692edd', 'In China Local Market Called'),
    ('granite__barbara_white_4a649260-074b-50ab-a8d8-1ab679692edd', 'In China Local Stone Market Called'),
    ('granite__brown_bear_66f06860-df95-5d17-a42b-6ab57a2b4f32', 'In Russian'),
    ('granite__gabbro_drugoreckoe_be251c1b-ca07-56f3-b3aa-002b87b00fa2', 'In Russian'),
    ('granite__grain_beige_0d312302-a58d-52d1-92ed-cb567a5d3937', 'In China Ston Emarket'),
    ('granite__green_ray_6ddfccf1-d665-5c60-9f33-e00892c30c83', 'In Russia'),
    ('granite__jacaranda_9b43f98d-2670-5451-8059-9fd229bb1ec4', 'In China Market Called'),
    ('granite__jacaranda_9b43f98d-2670-5451-8059-9fd229bb1ec4', 'In China Stone Market Called'),
    ('granite__karelia_black_147f1209-51e3-53d8-93b2-0d5271d59296', 'In Russian'),
    ('granite__kashina_gora_58ceec0a-4a30-58bc-98aa-1cf8ad61159c', 'In Russian'),
    ('granite__kurtinskiy_af3164e8-6f12-51f8-b1de-7b03f9a10c01', 'In Russian'),
    ('granite__little_green_star_407bb3b4-221f-5484-8c83-7da3f35e6c1b', 'In China Ston Emarket'),
    ('granite__mansurovsky_af6db601-2fe8-5644-a1ef-bb59734e4fc6', 'In Russian'),
    ('granite__nordic_sunset_560227aa-3ae6-5c4a-8a85-3a893a804453', 'In Russian Nigrozero'),
    ('granite__rich_red_0f07497d-a56c-5613-91ff-6a592236ac06', 'In China Market Calling'),
    ('granite__rich_red_0f07497d-a56c-5613-91ff-6a592236ac06', 'In China Stone Market Calling'),
    ('granite__siena_river_7dd64323-8da5-5c94-849d-4a2406b0f55f', 'Tropical Siena Bordeaux Granite White River Granite'),
    ('granite__siena_river_7dd64323-8da5-5c94-849d-4a2406b0f55f', 'Tropical Siena Bordeaux White River'),
    ('granite__siena_white_24f3553a-eab9-57b0-b07c-3f179226ca96', 'Cotton White Graniteo White Siena Granite'),
    ('granite__siena_white_24f3553a-eab9-57b0-b07c-3f179226ca96', 'Cotton White O White Siena'),
    ('granite__tiger_skin_yellow_057e7300-77fd-53d0-8ed2-e96e96b144fc', 'In China Market'),
    ('granite__yuzhno_sultayevskiy_698091c5-12b5-5d6a-ad7c-a10ff5e64789', 'In Russian'),
    ('limestone__cypress_9784225e-1425-5c64-9977-8b62c678abe4', 'In China Marlet'),
    ('limestone__cypress_9784225e-1425-5c64-9977-8b62c678abe4', 'In China Stone Marlet'),
    ('marble__black_night_9e80cff6-52e9-5327-ad9b-4aeaba11e760', 'In China Market Called'),
    ('marble__black_night_9e80cff6-52e9-5327-ad9b-4aeaba11e760', 'In China Stone Market Called'),
    ('marble__blue_coast_92272076-a92a-5721-80a6-7daca3f0665d', 'C Te D'),
    ('marble__cipollino_renoir_43ea7fda-5f90-55cd-9318-4007d87e490a', 'China Cipollino In China Market'),
    ('marble__cipollino_renoir_43ea7fda-5f90-55cd-9318-4007d87e490a', 'China Cipollino In China Stone Market'),
    ('marble__cipollino_renoir_43ea7fda-5f90-55cd-9318-4007d87e490a', 'China Cipollino Marble In China Stone Market'),
    ('marble__cream_ivory_20deee04-d96f-5778-a285-ef9f1cc63fd0', 'In China Ston Emarket'),
    ('marble__crema_dolomit_2b4a0981-bc6b-5faa-b562-c75ca7123858', 'Dolomite Marble'),
    ('marble__espresso_macchiato_ad5031f7-054b-51e0-9a6b-13477dc75633', 'In China Market'),
    ('marble__fine_lines_snow_white_a4a68a4b-1176-52cc-943f-ef2cfd95dda7', 'In China Stonemarket'),
    ('marble__grigio_fiorito_9c9394bf-ebb0-5f54-bcf8-222f1f8339f3', 'Name In China Stone Market'),
    ('marble__nero_picasso_67b0c554-9dac-58a1-afd7-e795c5bbbe66', 'K R Ehir Nero Picasso'),
    ('marble__nero_picasso_67b0c554-9dac-58a1-afd7-e795c5bbbe66', 'K R Ehir Nero Picasso Marble'),
    ('marble__new_elegant_beige_45f5974e-c1c9-5d2b-9668-2df5ce76154f', 'In China Stonemarket'),
    ('marble__pena_dark_blue_bc13fdb8-178c-5244-8a0c-e9c89bb5407f', 'Name In China Stone Market'),
    ('marble__red_cherry_be904da5-2c83-533d-a42b-b22477b059dc', 'In China'),
    ('marble__romantic_grey_c674d2fd-3583-59a0-b94e-3356e489b1f3', 'In China Ston Emarket'),
    ('marble__spanish_gold_87b4213c-cb83-58d8-9db8-7da8bc2fc92c', 'In China Market Called'),
    ('marble__spanish_gold_87b4213c-cb83-58d8-9db8-7da8bc2fc92c', 'In China Stone Market Called'),
    ('marble__ultraman_beige_afbb6a5d-2aed-55a3-a3a9-c3b6530dabc2', 'In Chinese Market'),
    ('marble__ultraman_beige_afbb6a5d-2aed-55a3-a3a9-c3b6530dabc2', 'In Chinese Stone Market'),
    ('marble__vienna_golden_2dec6a92-8229-53a0-9ff0-023c5e836727', 'In China Ston Emarket'),
    ('marble__ya_beige_ef9021c3-5570-5100-9de6-5ac561fb4002', 'In China Market'),
]
# (Key, a real term the error stood for): each is in every seed input
REAL_TERMS = [
    ('granite__nordic_sunset_560227aa-3ae6-5c4a-8a85-3a893a804453', 'Nigrozero'),
    ('granite__siena_river_7dd64323-8da5-5c94-849d-4a2406b0f55f', 'Tropical Siena Bordeaux Granite'),
    ('granite__siena_river_7dd64323-8da5-5c94-849d-4a2406b0f55f', 'White River Granite'),
    ('granite__siena_river_7dd64323-8da5-5c94-849d-4a2406b0f55f', 'Tropical Siena Bordeaux'),
    ('granite__siena_river_7dd64323-8da5-5c94-849d-4a2406b0f55f', 'White River'),
    ('granite__siena_white_24f3553a-eab9-57b0-b07c-3f179226ca96', 'Cotton White Granite'),
    ('granite__siena_white_24f3553a-eab9-57b0-b07c-3f179226ca96', 'White Siena Granite'),
    ('granite__siena_white_24f3553a-eab9-57b0-b07c-3f179226ca96', 'Cotton White'),
    ('granite__siena_white_24f3553a-eab9-57b0-b07c-3f179226ca96', 'White Siena'),
    ('marble__cipollino_renoir_43ea7fda-5f90-55cd-9318-4007d87e490a', 'China Cipollino'),
    ('marble__cipollino_renoir_43ea7fda-5f90-55cd-9318-4007d87e490a', 'China Cipollino Marble'),
    ('marble__nero_picasso_67b0c554-9dac-58a1-afd7-e795c5bbbe66', 'Kırşehir Nero Picasso'),
    ('marble__nero_picasso_67b0c554-9dac-58a1-afd7-e795c5bbbe66', 'Kırşehir Nero Picasso Marble'),
]


def _inputs() -> dict[str, dict[str, set[str]]]:
    """Each seed input: Key -> the alias_keys of its values."""
    out: dict[str, dict[str, set[str]]] = {}
    for base in BASES:
        with base.open(encoding="utf-8-sig", newline="") as handle:
            out[base.parent.name] = {r["Key"]: {alias_key(a) for a in (r["Aliases"] or "").split("|") if a}
                                     for r in csv.DictReader(handle)}
    posts = json.loads(BACKBONE.read_text(encoding="utf-8"))["posts"]
    out["backbone"] = {p["key"]: {alias_key(v) for v in [*p.get("aliases", []), *p.get("codes", [])]}
                       for p in posts if p.get("key")}
    return out


def test_no_seed_input_holds_a_source_error():
    held = [(name, key, value) for name, keys in _inputs().items() for key, value in SOURCE_ERRORS
            if alias_key(value) in keys.get(key, set())]
    assert held == []


def test_every_real_term_is_in_every_seed_input():
    missing = [(name, key, term) for name, keys in _inputs().items() for key, term in REAL_TERMS
               if alias_key(term) not in keys.get(key, set())]
    assert missing == []


def test_no_correction_table_or_reader_remains():
    from stone_pipeline.aliases import seed_rewrite
    assert not (REPO / "catalog_source" / "seed_alias_fixes.csv").exists()
    assert not hasattr(seed_rewrite, "FIXES")
