"""Wave 4d: the config API is a route table (one handler per route), not a 440-line dispatcher."""

from __future__ import annotations

from collections import Counter

from stone_pipeline.config import server


def test_every_route_is_unique_and_literal_routes_precede_capturing_ones():
    keys = Counter((m, p) for m, p, _ in server._ROUTES)
    assert not [k for k, n in keys.items() if n > 1]
    earlier: list[tuple[str, tuple[str, ...]]] = []
    for method, pattern, _ in server._ROUTES:
        if not any(tok.startswith("{") for tok in pattern):
            shadow = [p for m, p in earlier if m == method and server._match(p, list(pattern)) is not None]
            assert not shadow, f"literal {pattern} is shadowed by the earlier capturing route {shadow}"
        earlier.append((method, pattern))


def test_a_known_path_with_another_method_is_405_naming_the_allowed_methods():
    code, body = server.dispatch("POST", ["adapters"], {})
    assert code == 405 and "GET" in body["error"] and "/config/v1/adapters" in body["error"]
    code, body = server.dispatch("GET", ["review", "decide"], None)
    assert code == 405 and "PUT" in body["error"] and "DELETE" in body["error"]


def test_an_unknown_path_is_404():
    assert server.dispatch("GET", ["nonsense"], None)[0] == 404
    assert server.dispatch("GET", ["review", "nonsense"], None)[0] == 404
    assert server.dispatch("GET", [], None)[0] == 404
