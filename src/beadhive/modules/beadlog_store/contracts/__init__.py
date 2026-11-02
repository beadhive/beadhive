"""The ``beadlog.store`` capability, API version 1 (beadlog-format-adr.md amendment A6).

Standard library only and store-neutral. A provider (a plugin package providing
``beadlog.store@1``) returns a :class:`BeadlogStore`; ``bh backfill apply --store <name>`` writes a
plan through its :meth:`~BeadlogStore.loader` inside the backfill safety envelope, and
``bh backfill export --store <name>`` writes its :meth:`~BeadlogStore.snapshot` as a ``.bdlg``.
BeadLog events travel as opaque objects here (``beadlog.BeadEvent`` in practice): the contract
does not import the format library.

Nothing in this contract may carry a secret. A store receives its credential as a
:data:`PasswordSource` it calls only when it connects, and every message a store or the
composition root raises names keys, ids and remedies, never a value.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

#: The capability id every provider declares in ``plugin.json`` ``capabilities.provides``.
BEADLOG_STORE_CAPABILITY_ID = "beadlog.store"
#: The capability's API major; a provider declaring another major is not composed.
BEADLOG_STORE_API_VERSION = 1
#: The built-in store ``--store`` defaults to: the hive's bd store, composed without a plugin.
BUILTIN_STORE = "bd"

#: Called by a store when (and only when) it opens a connection; ``None`` = no password.
PasswordSource = Callable[[], "str | None"]


class BeadlogStoreError(Exception):
    """A BeadLog store refused or failed. ``str(exc)`` is operator-facing and never carries a
    configuration value or a secret."""


class BeadlogStoreUnavailable(BeadlogStoreError):
    """The requested store cannot be composed: unknown name, plugin not installed, disabled,
    invalid configuration or not the selected owner. The message says what to do."""


@runtime_checkable
class BeadlogStoreLoader(Protocol):
    """The beadlog StoreLoader shape (``beadhive_backfill.StoreLoader``): what the backfill
    envelope reads the store back through and writes a plan through."""

    @property
    def name(self) -> str: ...

    @property
    def version(self) -> str: ...

    @property
    def capabilities(self) -> frozenset[str]: ...

    @property
    def target(self) -> str:
        """A displayable name of the store (never a credential)."""
        ...

    def export(self) -> Iterable[Any]:
        """The store's history as BeadLog events, each with its original event id."""
        ...

    def load(self, events: Sequence[Any], *, dry_run: bool) -> Mapping[str, Any]:
        """Apply (or with ``dry_run`` only plan) ``events``; return the loss report."""
        ...


@dataclass(frozen=True)
class StoreSnapshot:
    """A store's current state as BeadLog events (what ``bh backfill export`` archives).

    ``notes`` counts, per stable code, what the read could not carry back into BeadLog; a store
    reports it rather than dropping it silently."""

    target: str
    events: tuple[Any, ...]
    notes: Mapping[str, int] = field(default_factory=dict)


@runtime_checkable
class BeadlogStore(Protocol):
    """One bound BeadLog store: the object a ``beadlog.store@1`` provider's factory returns.

    Building it opens nothing; the first :meth:`loader` or :meth:`snapshot` connects."""

    @property
    def store(self) -> str:
        """The ``--store`` name this store answers to (for example ``xtdb``)."""
        ...

    def loader(self) -> BeadlogStoreLoader:
        """The loader the backfill envelope uses (also its reader and pre-state source)."""
        ...

    def snapshot(self, *, group: str, extracted_at: str) -> StoreSnapshot:
        """The current state of bead group ``group`` as events stamped ``extracted_at``."""
        ...

    def close(self) -> None:
        """Release any connection; idempotent."""
        ...


__all__ = [
    "BEADLOG_STORE_API_VERSION",
    "BEADLOG_STORE_CAPABILITY_ID",
    "BUILTIN_STORE",
    "BeadlogStore",
    "BeadlogStoreError",
    "BeadlogStoreLoader",
    "BeadlogStoreUnavailable",
    "PasswordSource",
    "StoreSnapshot",
]
