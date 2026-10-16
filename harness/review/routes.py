"""A small route table for the review server, so each view's API can live in its own module.

A handler takes a `Request` and returns a JSON-serialisable value (sent with 200) or raises
`HTTPError` with a status and a message the page shows as is. Register one with:

    @route("GET", r"/api/things/(\\d+)")
    def get_thing(req: Request): ...

Patterns are full-match regexes against the path without its query string.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


class HTTPError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Request:
    method: str
    path: str
    match: re.Match
    query: dict[str, str] = field(default_factory=dict)
    body: Any = None


@dataclass
class Route:
    method: str
    pattern: re.Pattern
    fn: Callable[[Request], Any]


ROUTES: list[Route] = []


def route(method: str, pattern: str) -> Callable:
    def register(fn: Callable[[Request], Any]) -> Callable[[Request], Any]:
        ROUTES.append(Route(method, re.compile(pattern), fn))
        return fn

    return register


def resolve(method: str, path: str) -> tuple[Route, re.Match] | tuple[None, None]:
    """The first route whose method and pattern match, else (None, None)."""
    for r in ROUTES:
        if r.method == method and (m := r.pattern.fullmatch(path)):
            return r, m
    return None, None


def path_exists(path: str) -> bool:
    """True when some method serves `path`, so the server can answer 405 instead of 404."""
    return any(r.pattern.fullmatch(path) for r in ROUTES)
