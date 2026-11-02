"""Dangling-edge report: XTDB enforces no foreign keys, so a query finds the broken ones.

An edge whose ``to_id`` is not a ``beads._id`` at the current valid time is *dangling*. The
loader writes such an edge anyway (a target may be external, or loaded in another run) and
reports it, rather than refusing the load (bh-w56ts E6, contract 4); the bh corpus has seven
legitimate ones.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ._layout import BEAD_EDGES, BEADS

EXTERNAL_PREFIX = "external:"
DANGLING_EDGE_SQL = (
    f"SELECT e.from_id, e.type, e.to_id FROM {BEAD_EDGES} AS e "
    f"WHERE NOT EXISTS (SELECT 1 FROM {BEADS} AS b WHERE b._id = e.to_id) "
    "ORDER BY e.from_id, e.type, e.to_id"
)
DANGLING_EDGE_AS_OF_SQL = (
    f"SELECT e.from_id, e.type, e.to_id FROM {BEAD_EDGES} FOR VALID_TIME AS OF %s AS e "
    f"WHERE NOT EXISTS (SELECT 1 FROM {BEADS} FOR VALID_TIME AS OF %s AS b WHERE b._id = e.to_id) "
    "ORDER BY e.from_id, e.type, e.to_id"
)


@dataclass(frozen=True, order=True)
class DanglingEdge:
    from_id: str
    type: str
    to_id: str

    @property
    def external(self) -> bool:
        """True for a target outside the hive (``external:<system>:<ref>``)."""
        return self.to_id.startswith(EXTERNAL_PREFIX)


@dataclass(frozen=True)
class DanglingReport:
    """Edges pointing at no bead, split into declared-external and unresolved targets."""

    edges: tuple[DanglingEdge, ...]

    @property
    def external(self) -> tuple[DanglingEdge, ...]:
        return tuple(edge for edge in self.edges if edge.external)

    @property
    def unresolved(self) -> tuple[DanglingEdge, ...]:
        return tuple(edge for edge in self.edges if not edge.external)

    def __len__(self) -> int:
        return len(self.edges)


def find_dangling_edges(connection: Any, *, valid_as_of: datetime | None = None) -> DanglingReport:
    """Run the anti-join and return the report (current valid time, or ``valid_as_of``)."""
    if valid_as_of is None:
        rows = connection.execute(DANGLING_EDGE_SQL).fetchall()
    else:
        if valid_as_of.tzinfo is None:
            raise ValueError("valid_as_of needs an aware datetime")
        rows = connection.execute(DANGLING_EDGE_AS_OF_SQL, [valid_as_of, valid_as_of]).fetchall()
    return DanglingReport(tuple(DanglingEdge(str(a), str(b), str(c)) for a, b, c in rows))


__all__ = [
    "DANGLING_EDGE_AS_OF_SQL",
    "DANGLING_EDGE_SQL",
    "EXTERNAL_PREFIX",
    "DanglingEdge",
    "DanglingReport",
    "find_dangling_edges",
]
