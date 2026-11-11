"""An in-memory :class:`~beadlog_sources.ledger.LedgerDirectory` for tests.

::

    from beadlog_sources.testing.ledger import InMemoryLedgerDirectory

    directory = InMemoryLedgerDirectory({"bh": ["bh-1", "bh-2"], "hq": ["hq-9"]})
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from ..ledger import CachedLedgerDirectory


class InMemoryLedgerDirectory(CachedLedgerDirectory):
    """A directory over fixed id sets, with a ``loads`` counter per ledger."""

    def __init__(self, ledgers: Mapping[str, Iterable[str]]) -> None:
        self.loads: dict[str, int] = {}
        frozen = {name: tuple(ids) for name, ids in ledgers.items()}
        super().__init__({name: self._loader(name, ids) for name, ids in frozen.items()})

    def _loader(self, name: str, ids: tuple[str, ...]):  # noqa: ANN202
        def load() -> tuple[str, ...]:
            self.loads[name] = self.loads.get(name, 0) + 1
            return ids

        return load
