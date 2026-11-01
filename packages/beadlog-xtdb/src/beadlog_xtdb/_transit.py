"""The transit-JSON subset the XTDB loader writes and reads, in-package (no transit dependency).

**Writing.** One document is one pgwire parameter of type ``transit`` (OID 16384). The writer
emits transit *verbose* JSON (maps as JSON objects, tagged values as one-key objects), which
XTDB's reader accepts and which needs no cache. The subset is exactly what a bead document uses:
``null``, booleans, integers, finite floats, strings, lists, string-keyed maps, sets
(``~#set``) and typed instants (``~#time/instant``, microsecond precision; the plain ``~t`` tag
keeps only milliseconds).

**Escaping.** A string that begins with ``~``, ``^`` or a backtick would be read as a tag, a cache
reference or a reserved prefix, so the writer prefixes it with ``~`` (``~tilde`` is sent as
``~~tilde``). Map keys follow the same rule. The reader undoes it.

**Reading.** With ``SET fallback_output_format='transit'`` XTDB renders nested and union-typed
columns as transit-JSON, in the *non-verbose* dialect: maps are ``["^ ", k, v, ...]`` arrays,
tagged values are ``["~#tag", rep]`` arrays, and map keys and tags longer than three characters
are cached (``"^0"``, ``"^1"``, ...). :func:`decode` handles both dialects. A nested timestamp
arrives as ``~#time/zoned-date-time`` (``"2026-10-22T00:06Z[UTC]"``, zero seconds omitted) and is
returned as an aware UTC :class:`~datetime.datetime`; a set comes back as a ``frozenset``.
Unknown tags are kept as :class:`Tagged`, never dropped.
"""

from __future__ import annotations

import base64
import json
import math
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

# Prefix characters that make a string unreadable as plain text unless escaped with ``~``.
_ESCAPED_PREFIXES = ("~", "^", "`")
INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1
_CACHE_SIZE = 44
_CACHE_CAPACITY = _CACHE_SIZE * _CACHE_SIZE
_CACHE_BASE = 48
_INSTANT = re.compile(
    r"^([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]{1,9}))?Z$"
)


class TransitError(ValueError):
    """A value outside the supported transit subset, or malformed transit text."""


@dataclass(frozen=True, order=True)
class Instant:
    """A UTC instant at microsecond precision, written as ``~#time/instant``.

    ``at`` must be an aware ``datetime``; it is normalised to UTC. Build one from a canonical
    BeadLog timestamp with :meth:`parse`.
    """

    at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.at, datetime) or self.at.tzinfo is None:
            raise TransitError(f"an Instant needs an aware datetime, got {self.at!r}")
        object.__setattr__(self, "at", self.at.astimezone(UTC))

    @classmethod
    def parse(cls, text: str) -> Instant:
        """Parse ``YYYY-MM-DDTHH:MM:SS[.f]Z`` (a canonical BeadLog timestamp).

        A fraction finer than a microsecond is refused rather than rounded: XTDB keeps
        microseconds, so truncating would silently change the stored instant.
        """
        match = _INSTANT.fullmatch(text) if isinstance(text, str) else None
        if match is None:
            raise TransitError(f"not an RFC 3339 UTC instant: {text!r}")
        year, month, day, hour, minute, second = (int(part) for part in match.groups()[:6])
        fraction = match.group(7) or ""
        if len(fraction) > 6 and fraction[6:].strip("0"):
            raise TransitError(f"{text!r} is finer than XTDB's microsecond precision")
        try:
            moment = datetime(
                year, month, day, hour, minute, second, int(fraction[:6].ljust(6, "0") or 0), UTC
            )
        except ValueError as exc:
            raise TransitError(f"not a valid instant: {text!r}") from exc
        return cls(moment)

    def iso(self) -> str:
        """``YYYY-MM-DDTHH:MM:SS[.ffffff]Z`` with trailing fraction zeros dropped."""
        text = self.at.strftime("%Y-%m-%dT%H:%M:%S")
        if self.at.microsecond:
            text += "." + f"{self.at.microsecond:06d}".rstrip("0")
        return text + "Z"

    def plus_microseconds(self, count: int) -> Instant:
        """This instant moved by ``count`` microseconds (the same-instant split, format ADR A5)."""
        return Instant(self.at + timedelta(microseconds=count))


@dataclass(frozen=True)
class Tagged:
    """A transit tag the reader has no decoder for, kept verbatim."""

    tag: str
    value: Any


@dataclass(frozen=True)
class Keyword:
    """A transit keyword (``~:name``); XTDB uses them for its own error and type payloads."""

    name: str


@dataclass(frozen=True)
class Symbol:
    """A transit symbol (``~$name``)."""

    name: str


def escape_string(text: str) -> str:
    """``text`` as transit-JSON spells it: a leading ``~``, ``^`` or backtick gains a ``~``."""
    return "~" + text if text.startswith(_ESCAPED_PREFIXES) else text


def unescape_string(text: str) -> str:
    """Inverse of :func:`escape_string` for a string already known to be a plain string."""
    if len(text) > 1 and text[0] == "~" and text[1] in _ESCAPED_PREFIXES:
        return text[1:]
    return text


# -- writing -------------------------------------------------------------------------------


def to_wire(value: Any) -> Any:
    """``value`` as a JSON-able verbose-transit structure (escaped strings and tagged values)."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if not INT64_MIN <= value <= INT64_MAX:
            raise TransitError(f"integer {value} does not fit XTDB's 64-bit integer")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TransitError(f"non-finite float {value!r} has no supported transit form")
        return value
    if isinstance(value, str):
        return escape_string(value)
    if isinstance(value, Instant):
        return {"~#time/instant": value.iso()}
    if isinstance(value, datetime):
        return to_wire(Instant(value))
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TransitError(f"map keys must be strings, got {key!r}")
            out[escape_string(key)] = to_wire(item)
        return out
    if isinstance(value, (set, frozenset)):
        # A deterministic order keeps the wire text, and therefore any digest of it, stable.
        members = sorted((to_wire(item) for item in value), key=lambda item: json.dumps(item))
        return {"~#set": members}
    if isinstance(value, (list, tuple)):
        return [to_wire(item) for item in value]
    raise TransitError(f"no transit encoding for {type(value).__name__}: {value!r}")


def encode(value: Any) -> str:
    """The transit-JSON text of ``value``."""
    try:
        return json.dumps(to_wire(value), ensure_ascii=False, separators=(",", ":"))
    except ValueError as exc:
        raise TransitError(str(exc)) from exc


def encode_bytes(value: Any) -> bytes:
    """:func:`encode` as UTF-8 (a lone surrogate is a :class:`TransitError`)."""
    try:
        return encode(value).encode("utf-8")
    except UnicodeEncodeError as exc:
        raise TransitError(f"text is not valid Unicode: {exc.reason}") from exc


# -- reading -------------------------------------------------------------------------------


def _cache_code(index: int) -> str:
    if index < _CACHE_SIZE:
        return "^" + chr(_CACHE_BASE + index)
    return "^" + chr(_CACHE_BASE + index // _CACHE_SIZE) + chr(_CACHE_BASE + index % _CACHE_SIZE)


def _cache_index(code: str) -> int:
    if len(code) == 2:
        return ord(code[1]) - _CACHE_BASE
    return (ord(code[1]) - _CACHE_BASE) * _CACHE_SIZE + ord(code[2]) - _CACHE_BASE


def _parse_zoned(text: str) -> datetime:
    """``2026-10-22T00:06Z[UTC]`` (seconds optional, zone suffix optional) as an aware UTC time."""
    bare = text.split("[", 1)[0]
    try:
        return datetime.fromisoformat(bare).astimezone(UTC)
    except ValueError as exc:
        raise TransitError(f"not a transit timestamp: {text!r}") from exc


def _hashable(value: Any) -> Any:
    if isinstance(value, list):
        return tuple(_hashable(item) for item in value)
    if isinstance(value, dict):
        return tuple((_hashable(k), _hashable(v)) for k, v in value.items())
    if isinstance(value, set):
        return frozenset(_hashable(item) for item in value)
    return value


class _Reader:
    def __init__(self) -> None:
        self._cache: list[str] = []

    # Cacheable: a map key, or a tag/keyword/symbol, longer than three characters.
    def _remember(self, text: str, *, as_key: bool) -> None:
        if len(text) > 3 and (as_key or text[:2] in ("~:", "~$", "~#")):
            if len(self._cache) >= _CACHE_CAPACITY:
                self._cache.clear()
            self._cache.append(text)

    def _text(self, text: str, *, as_key: bool) -> str:
        if text.startswith("^") and text != "^ " and len(text) in (2, 3):
            try:
                return self._cache[_cache_index(text)]
            except IndexError as exc:
                raise TransitError(f"transit cache reference {text!r} before it was set") from exc
        self._remember(text, as_key=as_key)
        return text

    def read(self, node: Any, *, as_key: bool = False) -> Any:
        if isinstance(node, str):
            return self._string(self._text(node, as_key=as_key))
        if isinstance(node, list):
            return self._array(node)
        if isinstance(node, dict):
            return self._verbose_map(node)
        return node

    def _array(self, items: list[Any]) -> Any:
        if not items:
            return []
        head = items[0]
        if isinstance(head, str):
            if head == "^ ":
                return self._pairs(items[1:])
            # Resolve the head once (it may be a cache reference or a cacheable tag); a plain
            # list must not register the same string in the cache twice.
            decoded = self._text(head, as_key=False)
            if len(items) == 2 and decoded.startswith("~#") and len(decoded) > 2:
                return self._tagged(decoded[2:], self.read(items[1]))
            return [self._string(decoded), *(self.read(item) for item in items[1:])]
        return [self.read(item) for item in items]

    def _pairs(self, flat: list[Any]) -> dict[Any, Any]:
        if len(flat) % 2:
            raise TransitError("a transit map needs key/value pairs")
        out: dict[Any, Any] = {}
        for index in range(0, len(flat), 2):
            key = self.read(flat[index], as_key=True)
            out[_hashable(key)] = self.read(flat[index + 1])
        return out

    def _verbose_map(self, node: dict[str, Any]) -> Any:
        if len(node) == 1:
            ((key, value),) = node.items()
            if key.startswith("~#") and len(key) > 2:
                return self._tagged(key[2:], self.read(value))
        return {_hashable(self._string(key)): self.read(value) for key, value in node.items()}

    def _tagged(self, tag: str, rep: Any) -> Any:
        if tag == "set":
            return frozenset(_hashable(item) for item in rep)
        if tag == "list":
            return list(rep)
        if tag == "cmap":
            return {_hashable(rep[i]): rep[i + 1] for i in range(0, len(rep), 2)}
        if tag in ("time/instant", "time/zoned-date-time") and isinstance(rep, str):
            return _parse_zoned(rep)
        if tag == "u" and isinstance(rep, str):
            return uuid.UUID(rep)
        if tag == "'":
            return rep
        return Tagged(tag, rep)

    def _string(self, text: str) -> Any:
        if not text.startswith("~"):
            return text
        if len(text) == 1:
            return text
        code, rest = text[1], text[2:]
        if code in _ESCAPED_PREFIXES:
            return text[1:]
        if code == ":":
            return Keyword(rest)
        if code == "$":
            return Symbol(rest)
        if code == "_":
            return None
        if code == "?":
            return rest == "t"
        if code in "in":
            return int(rest)
        if code == "d":
            return float(rest)
        if code == "z":
            return {"NaN": math.nan, "INF": math.inf, "-INF": -math.inf}.get(rest, rest)
        if code == "t":
            return _parse_zoned(rest if "T" in rest else rest + "T00:00:00Z")
        if code == "m":
            return datetime.fromtimestamp(int(rest) / 1000, UTC)
        if code == "u":
            return uuid.UUID(rest)
        if code == "b":
            return base64.b64decode(rest)
        if code in "cr":
            return rest
        return Tagged(code, rest)


def decode(text: str) -> Any:
    """Decode transit-JSON text (either dialect) into Python values.

    Maps become ``dict``, sets ``frozenset``, booleans ``bool`` and timestamps aware UTC
    ``datetime``; ``~:name`` becomes :class:`Keyword`. Raises :class:`TransitError` on text that
    is not valid transit-JSON.
    """
    try:
        node = json.loads(text)
    except ValueError as exc:
        raise TransitError(f"not JSON: {exc}") from exc
    return _Reader().read(node)


__all__ = [
    "INT64_MAX",
    "INT64_MIN",
    "Instant",
    "Keyword",
    "Symbol",
    "Tagged",
    "TransitError",
    "decode",
    "encode",
    "encode_bytes",
    "escape_string",
    "to_wire",
    "unescape_string",
]
