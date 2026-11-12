"""A variety keeps its OWN other spellings (Scraper M2 on #637): the same-type alias cleanup must never drop a name
that another row or post of the SAME variety (one Key, or one identity core across the category copies) spells it
by, e.g. 'G562 Maple Red' and its posts 'G562'. These pairs were dropped by the first cleanup and are restored. A
keyless block post is named by file, type and variant. No em dashes."""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
BASES = [REPO / "from_medusa" / env / "variants_export_base.csv" for env in ("development", "production")]
CATALOG = REPO / "catalog_source"

BASE_OWN_NAMES = [
    ('block_granite_red_wendeng_3024c4aa-9135-5c94-a62f-44fd5b828ba6', 'Red Wendeng-z'),
    ('block_granite_red_wendeng_3024c4aa-9135-5c94-a62f-44fd5b828ba6', 'Red Wendeng-z Granite'),
    ('block_granite_white_52935684-0cf2-5bed-b465-653420722ebb', 'White G'),
    ('block_limestone_hauteville_f7e251bd-2ef1-5fd0-9c6a-9a39d791e2b6', 'Hauteville C'),
    ('block_limestone_karina_0e9c5235-c340-503e-becd-4815cd9a7138', 'Karina V'),
    ('block_limestone_karina_0e9c5235-c340-503e-becd-4815cd9a7138', 'Karina V Limestone'),
    ('block_limestone_rosal_5fab98eb-9eca-53ff-97dc-a472a6eca85b', 'Rosal B'),
    ('block_limestone_sandy_ec3127e4-b83e-52b3-8a13-c84d9860705a', 'Sandy P'),
    ('block_marble_ariston_d3ae17db-5eeb-5c06-bca6-f81773fd9763', 'Ariston V'),
    ('block_tuff_armenia_ae5ee8ce-292c-5bc4-8ee1-f2d7b052c076', 'Armenia L'),
    ('slab_granite_red_wendeng_z_d42bcdb6-6381-40ff-b575-50844ddc4f0c', 'Red Wendeng-z'),
    ('slab_granite_red_wendeng_z_d42bcdb6-6381-40ff-b575-50844ddc4f0c', 'Red Wendeng-z Granite'),
    ('slab_granite_white_g_80be3aef-45c7-4d3f-91d3-54790b67ee6c', 'White G'),
    ('slab_limestone_hauteville_c_b5965390-2ef1-4136-8276-9d1abd6fd01f', 'Hauteville C'),
    ('slab_limestone_karina_v_db69540d-77d4-494f-831b-83c88b48619a', 'Karina V'),
    ('slab_limestone_karina_v_db69540d-77d4-494f-831b-83c88b48619a', 'Karina V Limestone'),
    ('slab_limestone_rosal_b_407a72f0-8dfa-438d-9ae7-02bb1310f3fe', 'Rosal B'),
    ('slab_limestone_sandy_p_ba756aa7-9159-4f44-a18f-0390ef96bcf4', 'Sandy P'),
    ('slab_marble_ariston_v_0b2fa92e-632e-4602-9046-e993e9c36599', 'Ariston V'),
    ('slab_tuff_armenia_l_5f9aaa0e-ca7f-4af8-89c8-821a69a9ae0e', 'Armenia L'),
    ('tile_granite_red_wendeng_z_6b88a872-6872-57c5-ac83-5e68ae43b7b6', 'Red Wendeng-z'),
    ('tile_granite_red_wendeng_z_6b88a872-6872-57c5-ac83-5e68ae43b7b6', 'Red Wendeng-z Granite'),
    ('tile_granite_white_g_1a3be813-ff90-5347-984f-f483f1df8f05', 'White G'),
    ('tile_limestone_hauteville_c_54c2a085-3eb2-5b48-b8d7-ce505cc54ca6', 'Hauteville C'),
    ('tile_limestone_karina_v_92b39baa-de43-53eb-9829-36a9c993f693', 'Karina V'),
    ('tile_limestone_karina_v_92b39baa-de43-53eb-9829-36a9c993f693', 'Karina V Limestone'),
    ('tile_limestone_rosal_b_43292ff6-873c-5954-925d-587faec04e58', 'Rosal B'),
    ('tile_limestone_sandy_p_bd6163c6-758c-525b-ad51-4b40a014510f', 'Sandy P'),
    ('tile_marble_ariston_v_04a4795d-bd1c-58f9-9d58-162ad475a381', 'Ariston V'),
    ('tile_tuff_armenia_l_f02fe7c8-7bb9-504a-af1b-490423099d5c', 'Armenia L'),
]
POST_OWN_NAMES = {
    "slabs": [
        ('slab_granite_g562_maple_red_9aa3dba4-1bb0-4351-821e-da86fd265af1', 'G562'),
        ('slab_granite_g562_maple_red_9aa3dba4-1bb0-4351-821e-da86fd265af1', 'G562 Granite'),
        ('slab_granite_g635_anxi_red_6330c8da-f45a-420f-ba54-d17dff4b328c', 'G635'),
        ('slab_granite_g635_anxi_red_6330c8da-f45a-420f-ba54-d17dff4b328c', 'G635 Granite'),
        ('slab_limestone_hauteville_c_b5965390-2ef1-4136-8276-9d1abd6fd01f', 'Hauteville'),
        ('slab_marble_ariston_v_0b2fa92e-632e-4602-9046-e993e9c36599', 'Ariston'),
        ('slab_marble_connemara_irish_green_cca7afd2-9004-42d8-8996-882334fa6644', 'Connemara'),
        ('slab_marble_silver_wave_silver_shadow_cf266ca5-78e3-4c1e-8eaa-22d9f8cc8c24', 'Silver Wave'),
        ('slab_marble_silver_wave_silver_shadow_cf266ca5-78e3-4c1e-8eaa-22d9f8cc8c24', 'Silver Wave Marble'),
    ],
    "blocks": [
        ('backbone_blocks.json:Limestone:Hauteville C', 'Hauteville'),
        ('backbone_blocks.json:Marble:Ariston V', 'Ariston'),
        ('block_granite_g562_maple_red_817a3922-1d6a-4e33-8305-b7c9c72ec0c8', 'G562'),
        ('block_granite_g562_maple_red_817a3922-1d6a-4e33-8305-b7c9c72ec0c8', 'G562 Granite'),
        ('block_granite_g635_anxi_red_73a905d7-8d51-44ac-bc1a-0fa5a0848e28', 'G635'),
        ('block_granite_g635_anxi_red_73a905d7-8d51-44ac-bc1a-0fa5a0848e28', 'G635 Granite'),
        ('block_granite_tiger_skin_yellow_white_adbf8f16-23af-40bc-952f-8b8d0b8e703a', 'Tiger Skin Granite'),
        ('block_granite_verde_ubatuba_ubatuba_4dcb9d2d-46d0-4e39-893d-ea5510e5890f', 'Verde Ubatuba Granite'),
        ('block_marble_connemara_irish_green_25e8b4d8-7857-46db-bb56-4974ca406966', 'Connemara'),
        ('block_marble_silver_wave_silver_shadow_e689eddc-cd76-4391-aa0f-f0c3b6486f8e', 'Silver Wave'),
        ('block_marble_silver_wave_silver_shadow_e689eddc-cd76-4391-aa0f-f0c3b6486f8e', 'Silver Wave Marble'),
    ],
    "tiles": [
        ('tile_granite_g562_maple_red_c41e9e83-ac0f-56fb-851d-19cdf90de32b', 'G562'),
        ('tile_granite_g562_maple_red_c41e9e83-ac0f-56fb-851d-19cdf90de32b', 'G562 Granite'),
        ('tile_granite_g635_anxi_red_83914233-b16a-58db-af1e-b162ee7bf86a', 'G635'),
        ('tile_granite_g635_anxi_red_83914233-b16a-58db-af1e-b162ee7bf86a', 'G635 Granite'),
        ('tile_limestone_hauteville_c_54c2a085-3eb2-5b48-b8d7-ce505cc54ca6', 'Hauteville'),
        ('tile_limestone_rosal_b_43292ff6-873c-5954-925d-587faec04e58', 'Rosal'),
        ('tile_marble_ariston_v_04a4795d-bd1c-58f9-9d58-162ad475a381', 'Ariston'),
        ('tile_marble_connemara_irish_green_4c805b37-1ad8-5d36-9e97-35cccd09a549', 'Connemara'),
        ('tile_marble_silver_wave_silver_shadow_e797ba7b-f60c-5f4f-b527-8cf6201fd60c', 'Silver Wave'),
        ('tile_marble_silver_wave_silver_shadow_e797ba7b-f60c-5f4f-b527-8cf6201fd60c', 'Silver Wave Marble'),
    ],
}


@pytest.mark.parametrize("base", BASES, ids=lambda p: p.parent.name)
def test_each_base_row_keeps_its_own_other_spellings(base):
    with base.open(encoding="utf-8-sig", newline="") as h:
        aliases = {r["Key"]: set(filter(None, r["Aliases"].split("|"))) for r in csv.DictReader(h)}
    assert [(k, a) for k, a in BASE_OWN_NAMES if a not in aliases.get(k, set())] == []


@pytest.mark.parametrize("category", sorted(POST_OWN_NAMES))
def test_each_backbone_post_keeps_its_own_other_spellings(category):
    name = f"backbone_{category}.json"
    posts = json.loads((CATALOG / name).read_text(encoding="utf-8"))["posts"]
    aliases = {p.get("key") or f"{name}:{p['stone_type']}:{p['variant']}": set(p["aliases"]) for p in posts}
    assert [(k, a) for k, a in POST_OWN_NAMES[category] if a not in aliases.get(k, set())] == []
