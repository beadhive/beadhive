"""RFC 8785 canonical JSON over the BeadLog v0.1 admissible value domain, and deep freezing.

The format ADR (section 4, rule 2) restricts identity material to strings, booleans, null,
arrays, objects and integers in the I-JSON safe range. Under that restriction RFC 8785 needs no
ECMAScript number formatting: integers print in plain decimal and anything else is refused.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any

MAX_SAFE_INTEGER = 2**53 - 1

_SHORT_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


class AdmissibleValueError(ValueError):
    """A value outside the v0.1 identity domain (a float, an unsafe integer, a lone surrogate)."""


def _string(text: str) -> str:
    out: list[str] = []
    for character in text:
        code = ord(character)
        if 0xD800 <= code <= 0xDFFF:
            raise AdmissibleValueError(f"lone surrogate U+{code:04X} in identity material")
        escaped = _SHORT_ESCAPES.get(character)
        if escaped is not None:
            out.append(escaped)
        elif code < 0x20:
            out.append(f"\\u{code:04x}")
        else:
            out.append(character)
    return '"' + "".join(out) + '"'


def _utf16_key(key: str) -> bytes:
    # RFC 8785 sorts member names by UTF-16 code units; big-endian UTF-16 bytes compare the same.
    return key.encode("utf-16-be", "surrogatepass")


def canonical_json(value: Any) -> str:
    """Serialize ``value`` with RFC 8785 (JCS) restricted to the v0.1 admissible domain.

    Raises :class:`AdmissibleValueError` for non-integer numbers (``2.0`` included), integers
    outside +/-(2**53 - 1), lone surrogates, non-string object keys and non-JSON types.
    """
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        if abs(value) > MAX_SAFE_INTEGER:
            raise AdmissibleValueError(f"integer {value} is outside the I-JSON safe range")
        return str(value)
    if isinstance(value, float):
        raise AdmissibleValueError(f"non-integer number {value!r} in identity material")
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, Mapping):
        for key in value:
            if not isinstance(key, str):
                raise AdmissibleValueError(f"object key {key!r} is not a string")
        members = sorted(value, key=_utf16_key)
        return (
            "{" + ",".join(f"{_string(key)}:{canonical_json(value[key])}" for key in members) + "}"
        )
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return "[" + ",".join(canonical_json(item) for item in value) + "]"
    raise AdmissibleValueError(f"{type(value).__name__} is not a JSON value")


def canonical_bytes(value: Any) -> bytes:
    """UTF-8 bytes of :func:`canonical_json` (what ids hash and merges compare)."""
    return canonical_json(value).encode("utf-8")


def freeze(value: Any) -> Any:
    """Deep-freeze a JSON value: objects become read-only mappings, arrays become tuples."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    return value


def thaw(value: Any) -> Any:
    """Inverse of :func:`freeze`: plain ``dict`` / ``list`` JSON values, safe to mutate."""
    if isinstance(value, Mapping):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw(item) for item in value]
    return value
