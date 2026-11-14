"""A page as a small element tree, for the declared-HTML collector (market_prices/collectors/declared_html.py). Built
with the standard library's html.parser: no new dependency, no regular expression over HTML. A tree is bounded
(config.DECLARED_HTML_MAX_ELEMENTS), and every walk is iterative, so a hostile page can neither exhaust memory nor
the recursion limit.

Struck text (<del>, <s>, <strike>) is a former price: `text` never reads it, unless asked for it alone (struck=True)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Iterator, Optional

from market_prices import config
from market_prices.text import one_line

# elements that never hold content, so a start tag closes itself
_VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track",
                   "wbr"})
_STRUCK = frozenset({"del", "s", "strike"})
_NOT_TEXT = frozenset({"script", "style", "template", "noscript"})
_SPACE = re.compile(r"\s+")


class PageTooLarge(ValueError):
    """The page holds more elements than the cap: it is not read (never half-read)."""


@dataclass(eq=False)
class Element:
    tag: str
    attrs: dict[str, str]
    children: list = field(default_factory=list)            # Element or str, in page order

    @property
    def classes(self) -> frozenset[str]:
        return frozenset((self.attrs.get("class") or "").split())


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Element("#root", {})
        self.stack = [self.root]
        self.count = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        self.count += 1
        if self.count > config.DECLARED_HTML_MAX_ELEMENTS:
            raise PageTooLarge(f"the page holds over {config.DECLARED_HTML_MAX_ELEMENTS} elements")
        element = Element(tag, {name: value or "" for name, value in attrs})
        self.stack[-1].children.append(element)
        if tag not in _VOID:
            self.stack.append(element)

    def handle_startendtag(self, tag: str, attrs: list) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.stack.pop()

    def handle_endtag(self, tag: str) -> None:
        # an end tag closes the nearest open element of its name; one with no open match is ignored
        for at in range(len(self.stack) - 1, 0, -1):
            if self.stack[at].tag == tag:
                del self.stack[at:]
                return

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def parse(page: str) -> Element:
    builder = _Builder()
    builder.feed(page)
    builder.close()
    return builder.root


def descendants(element: Element) -> Iterator[Element]:
    """Every element under `element`, in page order (itself not included)."""
    stack = list(reversed([child for child in element.children if isinstance(child, Element)]))
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed([child for child in current.children if isinstance(child, Element)]))


def matches(element: Element, tag: Optional[str] = None, class_: Optional[str] = None,
            attribute: Optional[str] = None) -> bool:
    return ((tag is None or element.tag == tag) and (class_ is None or class_ in element.classes)
            and (attribute is None or attribute in element.attrs))


def first(element: Element, tag: Optional[str] = None, class_: Optional[str] = None,
          attribute: Optional[str] = None) -> Optional[Element]:
    """The first element under `element` that matches, in page order."""
    return next((found for found in descendants(element) if matches(found, tag, class_, attribute)), None)


def locate(element: Element, class_: str, struck: bool) -> Optional[tuple[Element, bool]]:
    """The first element under `element` with the class token, and whether it sits inside struck text. A reading of
    current text (struck=False) skips struck subtrees, so a former price that carries the same class is never taken
    for the current one; a reading of struck text takes the first match wherever it is."""
    stack = [(child, child.tag in _STRUCK) for child in reversed(element.children) if isinstance(child, Element)]
    while stack:
        current, inside = stack.pop()
        if inside and not struck:
            continue
        if class_ in current.classes:
            return current, inside
        stack.extend((child, inside or child.tag in _STRUCK)
                     for child in reversed(current.children) if isinstance(child, Element))
    return None


def text(element: Element, struck: bool = False, inside_struck: bool = False,
         hidden: frozenset[str] = frozenset()) -> Optional[str]:
    """The element's text as one clean line, its text nodes joined by a space. Struck text is left out, or, with
    struck=True, is the only text read; `inside_struck` says the element itself sits inside struck text. Script and
    style content is never text, nor is anything under an element carrying one of the `hidden` classes (a platform's
    own visually hidden markup)."""
    parts: list[str] = []
    stack: list[tuple[object, bool]] = [(element, inside_struck or element.tag in _STRUCK)]
    while stack:
        node, inside_struck = stack.pop()
        if isinstance(node, str):
            if inside_struck == struck:
                parts.append(node)
            continue
        if node.tag in _NOT_TEXT or node.classes & hidden:
            continue
        stack.extend((child, inside_struck or (isinstance(child, Element) and child.tag in _STRUCK))
                     for child in reversed(node.children))
    return collapse(" ".join(parts))


def collapse(value: str) -> Optional[str]:
    """One clean line (text.one_line); None when nothing is left. The parser has already decoded the page's
    entities, so nothing is decoded twice."""
    return one_line(value)



_PAIRED_CELLS = {"th": ("td", "th"), "td": ("td",), "dt": ("dd",)}


def label_key(label: str) -> str:
    """How a label is compared: whole, case, a trailing colon and whitespace aside ("MATERIAL :" is "material")."""
    return _SPACE.sub(" ", label).strip().rstrip(":").strip().casefold()


def labelled(container: Element, label: str) -> Optional[str]:
    """The value the container pairs with `label`, in the first structure that carries it, in page order: a th or td
    cell and the next cell of its row, a dt and its dd, or any other element whose whole text is the label and the
    text that follows it in its parent. Struck subtrees are never read."""
    wanted = label_key(label)
    stack: list = [(container, None)]
    while stack:
        element, parent = stack.pop()
        if element.tag in _STRUCK or element.tag in _NOT_TEXT:
            continue
        if parent is not None and (own := text(element)) is not None and label_key(own) == wanted:
            return _paired(element, parent)
        stack.extend((child, element) for child in reversed(element.children) if isinstance(child, Element))
    return None


def _paired(label_element: Element, parent: Element) -> Optional[str]:
    after = parent.children[parent.children.index(label_element) + 1:]
    partners = _PAIRED_CELLS.get(label_element.tag)
    if partners is not None:
        cell = next((node for node in after if isinstance(node, Element) and node.tag in partners), None)
        return text(cell) if cell is not None else None
    parts = [node if isinstance(node, str) else (text(node) or "") for node in after
             if not (isinstance(node, Element) and (node.tag in _STRUCK or node.tag in _NOT_TEXT))]
    return collapse(" ".join(parts))
