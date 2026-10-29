"""JSON encoding of the port's plain values, driven by their type hints.

Used by the HTTP/JSON adapter (client) and the reference server (`http_json_server.py`), so both
sides agree on the wire format: Decimal → string, datetime/date → ISO 8601, Enum → its value,
dataclass → object, tuple/list/set → array.
"""

import dataclasses
import types
import typing
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any


def encode(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (int, float, str)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: encode(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, (list, tuple)):
        return [encode(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted(encode(v) for v in value)
    if isinstance(value, dict):
        return {str(k): encode(v) for k, v in value.items()}
    raise TypeError(f"cannot encode {type(value).__name__}")


def decode(tp: Any, data: Any) -> Any:
    if data is None or tp is Any:
        return data
    origin, args = typing.get_origin(tp), typing.get_args(tp)
    if origin in (typing.Union, types.UnionType):
        options = [a for a in args if a is not type(None)]
        if len(options) == 1:
            return decode(options[0], data)
        raise TypeError(f"cannot decode ambiguous union {tp}")
    if origin is list:
        return [decode(args[0], x) for x in data]
    if origin is tuple:
        return tuple(decode(args[0], x) for x in data)  # only tuple[X, ...] is used in the contract
    if origin in (set, frozenset):
        return frozenset(decode(args[0], x) for x in data)
    if origin is dict:
        return {k: decode(args[1], v) for k, v in data.items()}
    if isinstance(tp, type):
        if issubclass(tp, Enum):
            return tp(data)
        if tp is Decimal:
            return Decimal(data)
        if tp is datetime:
            return datetime.fromisoformat(data)
        if tp is date:
            return date.fromisoformat(data)
        if dataclasses.is_dataclass(tp):
            hints = typing.get_type_hints(tp)
            return tp(**{f.name: decode(hints[f.name], data[f.name]) for f in dataclasses.fields(tp) if f.name in data})
    return data
