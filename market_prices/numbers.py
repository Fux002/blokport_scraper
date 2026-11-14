"""A source's own number notation, declared once and read one way: a sheet layout's `number` and a declared-HTML site's
`number` (the manager's ruling, 14 Nov: one logic). A text is a number only in the declared notation; anything else (a
range, a word, another notation) is no number, never a guess."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, model_validator


class NumberFormat(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decimal: Literal[".", ","]
    thousands: Literal["", ",", ".", " ", "'"]

    @model_validator(mode="after")
    def _separators_differ(self) -> "NumberFormat":
        if self.decimal == self.thousands:
            raise ValueError(f"the decimal and thousands separators are both {self.decimal!r}")
        return self


def parse_number(text: str, number: NumberFormat) -> Optional[Decimal]:
    """The text as a number in the declared notation: digits, at most one decimal separator, and thousands separators
    only in groups of three. Anything else is no number."""
    decimal, thousands = re.escape(number.decimal), re.escape(number.thousands)
    fraction = rf"(?:{decimal}\d+)?"
    plain = rf"\d+{fraction}"
    grouped = rf"\d{{1,3}}(?:{thousands}\d{{3}})+{fraction}" if number.thousands else plain
    if not re.fullmatch(rf"{plain}|{grouped}", text):
        return None
    canonical = text.replace(number.thousands, "") if number.thousands else text
    try:
        return Decimal(canonical.replace(number.decimal, "."))
    except InvalidOperation:
        return None
