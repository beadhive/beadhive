"""The SourceReader port and reader/strategy discovery.

A **reader** turns one kind of source (git history, design docs, a planning tree, a tracker) into
typed :class:`~beadlog_sources.evidence.Artifact` records. It is *pure*: ``read`` takes a
:class:`ReadRequest` and returns artifacts; it never touches a store and never decides what is
already a bead (that is the later collation and projection phases, see
``docs/design/backfill-history-pipeline-adr.md``).

Discovery never depends on which distribution is installed. A name resolves from, in order of
the merge:

- the installed entry-point group (``beadlog.readers`` for readers, ``beadlog.sources`` for
  extract strategies), registered by the ``beadlog-sources`` distribution *and* by the ``beadhive``
  wheel, which vendors this library; and
- a static built-in table (:data:`BUILTIN_READERS`, :data:`BUILTIN_STRATEGIES`) so a plain source
  tree, a vendored copy or a wheel whose metadata lacks the entry points still finds them.

The same name from both sources is one registration when both point at the same target (the
normal case: two distributions installing the same entry point) and a
:class:`DuplicateRegistrationError` when the targets differ.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from importlib import import_module, metadata
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from .evidence import Artifact, Category

# Entry-point group under which installed distributions register source readers.
READERS_GROUP = "beadlog.readers"
# Entry-point group of extract strategies (same value as contracts.ENTRY_POINT_GROUP).
STRATEGIES_GROUP = "beadlog.sources"

# Static registries: name -> "module:attribute". Entry points of the same names are checked
# against these. Real readers (git, docs, planning, github) register here as they land.
BUILTIN_READERS: Mapping[str, str] = MappingProxyType({})
BUILTIN_STRATEGIES: Mapping[str, str] = MappingProxyType(
    {
        "bd": "beadlog_sources.bd:BdBaselineStrategy",
        "bd-history": "beadlog_sources.bd_timeline:BdHistoryStrategy",
        "docs": "beadlog_sources.docs:DocsStrategy",
        "git-commits": "beadlog_sources.commits:GitCommitsStrategy",
        "planning": "beadlog_sources.planning:PlanningStrategy",
    }
)

_EMPTY: Mapping[str, Any] = MappingProxyType({})


class DuplicateRegistrationError(ValueError):
    """One name registered twice with different targets."""


@dataclass(frozen=True)
class ReadRequest:
    """One read run of a reader.

    ``root`` is the repository (or source root) to read; ``options`` any JSON object the reader
    understands (recorded as the run's ``ReaderHeader.options``); ``cursor`` the reader's previous
    cursor for an incremental read (``None``: read everything).
    """

    root: Path
    options: Mapping[str, Any] = field(default=_EMPTY, hash=False)
    cursor: Any = field(default=None, hash=False)


@runtime_checkable
class SourceReader(Protocol):
    """Port: read a source into artifacts. Pure: no store, no collation, no bead decisions."""

    name: str
    version: str
    categories: tuple[Category, ...]

    def read(self, request: ReadRequest) -> Iterable[Artifact]:
        """Yield the artifacts of the source named by ``request``."""
        ...


@dataclass(frozen=True)
class Registration:
    """One discovered name: its ``target`` (``module:attr``, ``None`` when unknown) and where it
    came from (``entry-point`` or ``builtin``)."""

    name: str
    target: str | None
    origin: str
    _load: Callable[[], Any] = field(repr=False, compare=False)

    def load(self) -> Any:
        """Import and return the registered object."""
        return self._load()


def _loader(target: str) -> Callable[[], Any]:
    def load() -> Any:
        module, _, attr = target.partition(":")
        obj: Any = import_module(module)
        for part in attr.split("."):
            obj = getattr(obj, part)
        return obj

    return load


def _same(left: Registration, right: Registration) -> bool:
    if left.target is not None and right.target is not None:
        return left.target == right.target
    # A target is unknown (an injected entry point carries no value): compare what they load.
    return bool(left.load() is right.load())


def discover(
    group: str,
    builtin: Mapping[str, str],
    entry_points: Callable[[], Iterable[Any]] | None = None,
) -> dict[str, Registration]:
    """Every registration of ``group``: installed entry points merged with the ``builtin`` table.

    ``entry_points`` is the injected seam (default: ``importlib.metadata`` for ``group``). Names
    come back sorted. A name registered more than once with different targets raises
    :class:`DuplicateRegistrationError`; the same target (two distributions shipping the same
    entry point, or an entry point equal to the built-in) is one registration.
    """
    if entry_points is None:

        def entry_points() -> Iterable[Any]:
            return metadata.entry_points(group=group)

    found: dict[str, Registration] = {}

    def add(entry: Registration) -> None:
        known = found.get(entry.name)
        if known is None:
            found[entry.name] = entry
        elif not _same(known, entry):
            raise DuplicateRegistrationError(
                f"{group}: {entry.name!r} is registered twice "
                f"({known.target or known.origin} and {entry.target or entry.origin})"
            )

    for point in sorted(entry_points(), key=lambda p: p.name):
        add(Registration(point.name, getattr(point, "value", None), "entry-point", point.load))
    for name in sorted(builtin):
        add(Registration(name, builtin[name], "builtin", _loader(builtin[name])))
    return dict(sorted(found.items()))


def discover_readers(
    entry_points: Callable[[], Iterable[Any]] | None = None,
    builtin: Mapping[str, str] | None = None,
) -> dict[str, Registration]:
    """Readers of ``beadlog.readers`` plus :data:`BUILTIN_READERS`."""
    return discover(READERS_GROUP, BUILTIN_READERS if builtin is None else builtin, entry_points)


def discover_strategies(
    entry_points: Callable[[], Iterable[Any]] | None = None,
    builtin: Mapping[str, str] | None = None,
) -> dict[str, Registration]:
    """Strategies of ``beadlog.sources`` plus :data:`BUILTIN_STRATEGIES`."""
    return discover(
        STRATEGIES_GROUP, BUILTIN_STRATEGIES if builtin is None else builtin, entry_points
    )


def load_reader(registration: Registration) -> SourceReader:
    """Load a registered reader (a class is instantiated); it must satisfy :class:`SourceReader`."""
    loaded = registration.load()
    reader = loaded() if isinstance(loaded, type) else loaded
    if not isinstance(reader, SourceReader):
        raise TypeError(f"reader {registration.name!r} is not a SourceReader")
    return reader
