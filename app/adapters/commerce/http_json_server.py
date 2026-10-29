"""Reference server for the HTTP/JSON commerce contract (see `http_json.py`).

`handle()` turns any `CommercePort` implementation into the wire contract, so it doubles as
an executable specification: the conformance tests run the HTTP adapter against it. A company
would write the same thing in its own language and framework.
"""

import typing
from typing import Any

import httpx

from app.adapters.commerce.codec import decode, encode
from app.core.types import DataAccessError
from app.ports.commerce import CommercePort


def handle(commerce: CommercePort, function: str, body: dict[str, Any]) -> tuple[int, dict]:
    method = getattr(CommercePort, function, None)
    if function.startswith("_") or not callable(method):
        return 404, {"error": f"unknown function {function}"}
    hints = typing.get_type_hints(method)
    unknown = [k for k in body if k not in hints]
    if unknown:
        return 422, {"error": f"unknown arguments {unknown}"}
    try:
        kwargs = {name: decode(hints[name], value) for name, value in body.items()}
        result = getattr(commerce, function)(**kwargs)
    except DataAccessError as exc:
        return 409, {"error": str(exc)}
    return 200, {"result": encode(result)}


def as_transport(commerce: CommercePort) -> httpx.MockTransport:
    """An in-process HTTP transport serving `commerce` (no network, for tests and demos)."""

    def route(request: httpx.Request) -> httpx.Response:
        import json

        status, payload = handle(commerce, request.url.path.rstrip("/").rsplit("/", 1)[-1],
                                 json.loads(request.content or b"{}"))
        return httpx.Response(status, json=payload)

    return httpx.MockTransport(route)
