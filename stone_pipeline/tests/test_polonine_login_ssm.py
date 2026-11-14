"""Polonine's customer login (the owner, 14 Nov: "Yes, store it"), wired from SSM into the PRODUCE only: the sync
service's config container runs the live scrape and its separate price pass, while the ad-hoc scraper task never
scrapes, so it never receives the login (least privilege). Dev by its known name, prod by explicit SSM-name vars, both
only for a brand that switches the login on; valueFrom only, never a plain value. Read from the Terraform text, as the
other infra wiring tests do."""

from __future__ import annotations

import re
from pathlib import Path

_INFRA = Path(__file__).resolve().parents[2] / "infra"
_NAMES = ("SCRAPER_POLONINE_USERNAME", "SCRAPER_POLONINE_PASSWORD")   # scrapers/polonine.py LOGIN_*_ENV


def _main() -> str:
    return (_INFRA / "main.tf").read_text(encoding="utf-8")


def _local(name: str) -> str:
    """The text of one entry of main.tf's locals block, up to the next top-level local."""
    found = re.search(rf"^\s{{2}}{name}\s*=\s*(.*?)(?=^\s{{2}}\w+\s*=|^}})", _main(), flags=re.S | re.M)
    assert found, f"no local {name}"
    return found.group(1)


def test_the_login_reaches_the_produce_and_not_the_ad_hoc_task():
    main = _main()
    for env in ("dev", "prod"):
        produce = _local(f"{env}_produce_secrets")
        assert all(name in produce for name in _NAMES), env
        assert f"local.{env}_ssm_secrets" in produce                   # the produce keeps every runtime secret it had
        assert not any(name in _local(f"{env}_ssm_secrets") for name in _NAMES)
        assert re.search(rf"produce_secret_arns\s*=\s*local\.{env}_produce_secrets", main), env
    assert not re.search(r"\bssm_secret_arns\s*=\s*local\.\w+_produce_secrets", main)


def test_dev_resolves_the_known_names_only_for_a_brand_that_switches_the_login_on():
    main = _main()
    for name in _NAMES:
        block = re.search(rf'data "aws_ssm_parameter" "\w+_dev" \{{[^}}]*"/\$\{{var\.brand\}}-dev/{name}"[^}}]*\}}',
                          main)
        assert block and "count = var.polonine_login ? 1 : 0" in block.group(0), name


def test_prod_takes_the_names_from_the_brand_and_only_blokport_sets_them():
    blokport = (_INFRA / "brands" / "blokport.tfvars").read_text(encoding="utf-8")
    assert re.search(r"^polonine_login\s*=\s*true", blokport, flags=re.M)
    assert re.search(r'^polonine_username_ssm_name\s*=\s*"/blokport-prod/SCRAPER_POLONINE_USERNAME"', blokport, flags=re.M)
    assert re.search(r'^polonine_password_ssm_name\s*=\s*"/blokport-prod/SCRAPER_POLONINE_PASSWORD"', blokport, flags=re.M)
    for brand in ("calcport", "wudport"):
        other = (_INFRA / "brands" / f"{brand}.tfvars").read_text(encoding="utf-8")
        assert "polonine" not in other.lower(), brand                # the variables' defaults: off, empty
    variables = (_INFRA / "variables.tf").read_text(encoding="utf-8")
    for var, default in (("polonine_login", "false"), ("polonine_username_ssm_name", '""'),
                         ("polonine_password_ssm_name", '""')):
        assert re.search(rf'variable "{var}" \{{[^}}]*default\s*=\s*{re.escape(default)}', variables), var


def test_the_produce_secrets_are_value_from_only():
    sync = (_INFRA / "modules" / "sync_service" / "main.tf").read_text(encoding="utf-8")
    assert re.search(r"\[for k, v in var\.produce_secret_arns : \{ name = k, valueFrom = v \}\]", sync)


def test_the_prod_login_is_wired_whole_or_not_at_all():
    """f8's should: one prod name without the other would wire half a login, silent at plan time. Each prod data source
    refuses the plan unless both names are set and the brand switches the login on."""
    main = _main()
    for which in ("username", "password"):
        block = re.search(rf'data "aws_ssm_parameter" "polonine_{which}" \{{(.*?)^\}}', main, flags=re.S | re.M)
        assert block, which
        body = block.group(1)
        assert "precondition" in body, which
        assert 'var.polonine_username_ssm_name != ""' in body and 'var.polonine_password_ssm_name != ""' in body, which
        assert "var.polonine_login" in body, which
