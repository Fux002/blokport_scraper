"""robots.txt as RFC 9309 defines it. urllib.robotparser is not used: on the Python this image runs it ignores
wildcards and takes the first matching rule, so a site's "Disallow: /*?" would read as permission."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from urllib.parse import unquote

# RFC 9309: a literal "*" or "$" is written percent-encoded, so those two escapes stay encoded on both sides while
# every other escape is decoded. Decoding them would turn a literal into a wildcard or an end anchor.
_LITERAL_ESCAPE = re.compile(r"%(2a|24)", re.IGNORECASE)
_LITERALS = {"*": "%2A", "$": "%24"}
# The product token of a User-agent line ends at the first character outside these (as Google's parser reads it).
_PRODUCT_TOKEN = re.compile(r"[A-Za-z_-]*")


def _normalise(text: str, *, operators: bool) -> str:
    """Percent-decoded except for a literal "*" or "$". In a target every raw "*" or "$" is a literal; in a pattern a
    raw "*" is the wildcard and a raw "$" the end anchor only as its last character, a literal anywhere else."""
    pieces = _LITERAL_ESCAPE.split(text)
    out = []
    for index, piece in enumerate(pieces):
        if index % 2:
            out.append(f"%{piece.upper()}")
            continue
        decoded = unquote(piece)
        if operators:
            out.append(decoded)
        else:
            out.append("".join(_LITERALS.get(char, char) for char in decoded))
    joined = "".join(out)
    if not operators:
        return joined
    anchored = joined.endswith("$")
    body = (joined[:-1] if anchored else joined).replace("$", _LITERALS["$"])
    return f"{body}$" if anchored else body


def _matches(pattern: str, target: str) -> bool:
    """Glob match in linear passes: a hostile file of many wildcards cannot make it backtrack."""
    anchored = pattern.endswith("$")
    head, *rest = (pattern[:-1] if anchored else pattern).split("*")
    if not target.startswith(head):
        return False
    if not rest:
        return not anchored or target == head
    position = len(head)
    for part in rest[:-1]:
        found = target.find(part, position)
        if found < 0:
            return False
        position = found + len(part)
    tail = rest[-1]
    if anchored:
        return len(target) - len(tail) >= position and target.endswith(tail)
    return target.find(tail, position) >= 0


@dataclass(frozen=True)
class Robots:
    rules: tuple[tuple[str, bool], ...] = ()
    crawl_delay: float = 0.0

    def allows(self, target: str) -> bool:
        """The longest matching rule decides; on a tie, allow wins. No matching rule allows."""
        normalised = _normalise(target, operators=False)
        matching = [(len(pattern), allow) for pattern, allow in self.rules if _matches(pattern, normalised)]
        return max(matching, default=(0, True))[1]


def _crawl_delay(value: str) -> float:
    try:
        delay = float(value)
    except ValueError:
        return 0.0
    return delay if math.isfinite(delay) and delay > 0 else 0.0


def _pattern(value: str) -> str:
    normalised = _normalise(value, operators=True)
    return normalised if normalised.startswith(("/", "*")) else f"/{normalised}"


def _agent(value: str) -> str:
    return "*" if value == "*" else _PRODUCT_TOKEN.match(value).group(0).lower()


def parse_robots(text: str, token: str) -> Robots:
    """The rules for `token`: every group naming it, or every "*" group when none does."""
    own: list[tuple[str, bool]] = []
    star: list[tuple[str, bool]] = []
    delays = {"own": 0.0, "star": 0.0}
    named, agents, in_rules = False, [], False
    for line in text.removeprefix("\ufeff").splitlines():
        key, separator, value = line.split("#", 1)[0].partition(":")
        key, value = key.strip().lower(), value.strip()
        if not separator:
            continue
        if key == "user-agent":
            if in_rules:
                agents, in_rules = [], False
            agents.append(_agent(value))
            named = named or agents[-1] == token
        elif key in ("allow", "disallow", "crawl-delay"):
            in_rules = True
            for scope, rules, applies in (("own", own, token in agents), ("star", star, "*" in agents)):
                if not applies:
                    continue
                if key == "crawl-delay":
                    delays[scope] = max(delays[scope], _crawl_delay(value))
                elif value:
                    rules.append((_pattern(value), key == "allow"))
    return Robots(tuple(own), delays["own"]) if named else Robots(tuple(star), delays["star"])
