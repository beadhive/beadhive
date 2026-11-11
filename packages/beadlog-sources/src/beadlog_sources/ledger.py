"""The ledger directory port: which registered ledger owns a bead-id token.

Phase B2 of the backfill pipeline resolves each ``Ref(kind="bead-token")`` against the ledgers
the run knows about. A **ledger** is a named bead store (a hive, a bd database) that exports the
set of ids it holds. The port answers two questions, from those exported id sets only:

- :meth:`LedgerDirectory.contains`: does token ``T`` exist in ledger ``L``?
- :meth:`LedgerDirectory.owners`: which registered ledgers hold token ``T``?

Ownership is exact id-set membership. Nothing here derives a ledger from a token's prefix or
shape (bh-ear6v): a token whose prefix looks like a known ledger's but is not in that ledger's
id set is :data:`UNRESOLVED`. A token held by two ledgers is :data:`AMBIGUOUS`, never guessed.

This module is a neutral port: real adapters (bd routes, an HQ hive registry) live in the
packages that own that knowledge. :class:`CachedLedgerDirectory` is the reusable base for them:
register each ledger with a loader of its id set; the set is read at most once per run. A test
fake is :class:`beadlog_sources.testing.ledger.InMemoryLedgerDirectory`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .evidence import Ref

LOCAL = "local"
FOREIGN = "foreign"
AMBIGUOUS = "ambiguous"
UNRESOLVED = "unresolved"
RESOLUTION_KINDS = (LOCAL, FOREIGN, AMBIGUOUS, UNRESOLVED)

BEAD_TOKEN = "bead-token"


class LedgerError(ValueError):
    """A ledger registration or resolution request that is malformed."""


@runtime_checkable
class LedgerDirectory(Protocol):
    """Read-only view of the registered ledgers and their exported id sets."""

    def ledgers(self) -> tuple[str, ...]:
        """The registered ledger names, sorted."""
        ...

    def contains(self, ledger: str, token: str) -> bool:
        """Whether ``token`` is in ``ledger``'s id set (``False`` for an unregistered ledger)."""
        ...

    def owners(self, token: str) -> tuple[str, ...]:
        """Every registered ledger whose id set holds ``token``, sorted."""
        ...


@dataclass(frozen=True)
class Resolution:
    """The outcome of resolving one token.

    ``kind`` is one of :data:`RESOLUTION_KINDS`. ``ledger`` names the owning ledger for
    ``local`` and ``foreign`` (``None`` otherwise). ``candidates`` lists every owner for
    ``ambiguous``, and for ``local`` the *other* (foreign) owners: an informational shadow so a
    cross-ledger collision can be flagged without losing the local link. ``token`` is the id
    as resolved.
    """

    kind: str
    token: str
    ledger: str | None = None
    candidates: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in RESOLUTION_KINDS:
            raise LedgerError(f"unknown resolution kind {self.kind!r}")


def resolve(directory: LedgerDirectory, token: str, *, local: str) -> Resolution:
    """Resolve ``token`` against ``directory``, with ``local`` the ledger being backfilled.

    Local first (the baseline order of ADR phase B2): if ``local`` is among the owners the
    result is ``local``, with any other owners in ``candidates``. Otherwise one owner is
    ``foreign``, no owner is ``unresolved``, and two or more foreign owners are ``ambiguous``.
    """
    owners = directory.owners(token)
    if not owners:
        return Resolution(UNRESOLVED, token)
    if local in owners:
        others = tuple(o for o in owners if o != local)
        return Resolution(LOCAL, token, ledger=local, candidates=others)
    if len(owners) > 1:
        return Resolution(AMBIGUOUS, token, candidates=tuple(owners))
    return Resolution(FOREIGN, token, ledger=owners[0])


def resolve_ref(directory: LedgerDirectory, ref: Ref, *, local: str) -> Resolution | None:
    """Resolve an evidence :class:`Ref`; ``None`` when it is not a ``bead-token`` ref."""
    if ref.kind != BEAD_TOKEN:
        return None
    return resolve(directory, ref.raw, local=local)


class CachedLedgerDirectory:
    """A directory over lazily loaded id sets, each read at most once per run.

    ``loaders`` maps a ledger name to a zero-argument callable returning its exported ids.
    Adapters supply the loaders; the first query that needs a ledger's ids calls its loader
    and keeps the frozen result.
    """

    def __init__(self, loaders: Mapping[str, Callable[[], Iterable[str]]]) -> None:
        for name in loaders:
            if not isinstance(name, str) or not name:
                raise LedgerError("ledger name must be a non-empty string")
        self._loaders = dict(loaders)
        self._ids: dict[str, frozenset[str]] = {}

    def ledgers(self) -> tuple[str, ...]:
        """The registered ledger names, sorted."""
        return tuple(sorted(self._loaders))

    def ids(self, ledger: str) -> frozenset[str]:
        """The id set of ``ledger`` (loaded once); an unregistered ledger raises."""
        if ledger not in self._loaders:
            raise LedgerError(f"ledger {ledger!r} is not registered")
        if ledger not in self._ids:
            self._ids[ledger] = frozenset(self._loaders[ledger]())
        return self._ids[ledger]

    def contains(self, ledger: str, token: str) -> bool:
        """Whether ``token`` is in ``ledger``'s id set (``False`` for an unregistered ledger)."""
        return ledger in self._loaders and token in self.ids(ledger)

    def owners(self, token: str) -> tuple[str, ...]:
        """Every registered ledger whose id set holds ``token``, sorted."""
        return tuple(name for name in self.ledgers() if token in self.ids(name))
