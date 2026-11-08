"""robots.txt as RFC 9309 defines it. urllib.robotparser is not used: on the Python this image runs it ignores
wildcards and takes the first matching rule, so a site's "Disallow: /*?" would read as permission."""

from __future__ import annotations

import math
from dataclasses import dataclass
from urllib.parse import unquote


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
        decoded = unquote(target)
        matching = [(len(pattern), allow) for pattern, allow in self.rules if _matches(pattern, decoded)]
        return max(matching, default=(0, True))[1]


def _crawl_delay(value: str) -> float:
    try:
        delay = float(value)
    except ValueError:
        return 0.0
    return delay if math.isfinite(delay) and delay > 0 else 0.0


def _pattern(value: str) -> str:
    decoded = unquote(value)
    return decoded if decoded.startswith(("/", "*")) else f"/{decoded}"


def parse_robots(text: str, token: str) -> Robots:
    """The rules for `token`: every group naming it, or every "*" group when none does."""
    own: list[tuple[str, bool]] = []
    star: list[tuple[str, bool]] = []
    delays = {"own": 0.0, "star": 0.0}
    named, agents, in_rules = False, [], False
    for line in text.splitlines():
        key, separator, value = line.split("#", 1)[0].partition(":")
        key, value = key.strip().lower(), value.strip()
        if not separator:
            continue
        if key == "user-agent":
            if in_rules:
                agents, in_rules = [], False
            agents.append(value.lower())
            named = named or value.lower() == token
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
