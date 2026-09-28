"""The ``bd`` CLI argv routes behind Beadhive's :mod:`beadhive_core` ports (bh-o3xuf).

A **library package** (``docs/design/package-class-library-vs-plugin-adr.md``): no
``plugin.json``, and it never imports the root ``beadhive`` distribution. It holds every ``bd``
argv route the API-first cutover left in the shell:

* the **CLI-compatibility** routes of the migrated cohorts — :class:`CliIssues`,
  :class:`CliMoleculeFiler`, and the read routes in :mod:`beadhive_bd_cli.reads` (``show``,
  ``children``, ``child_rows``, ``ready``) — which the shell selects only when no capable Beads
  session opens and the hive's ``work.beads.route`` allows the ``bd`` route;
* the operations Beads 1.3 has **no API for** at all, always served over ``bd``:
  :class:`CliLeases`, :class:`CliStateReads`, :class:`CliStateOperations`,
  :class:`CliGateOperations`, :class:`CliPlanningGates`, and every
  ``beadhive_core.COORDINATION_OPERATIONS`` gate / merge-slot / heartbeat / reclaim wrapper in
  :mod:`beadhive_bd_cli.coordination`.

Every route runs through the caller's :class:`BdTransport`; the shell resolves this package lazily
by name (``beadhive.bd_cli``) and supplies its own ``bd`` invocation seam as the transport.
"""

from __future__ import annotations

from . import coordination, reads
from .lifecycle import CliIssues, CliLeases, CliStateReads
from .planning import KICKOFF_REASON, RELEASE_HOLD_MARKER, CliMoleculeFiler, CliPlanningGates
from .review import CliGateOperations, CliStateOperations
from .transport import (
    BdResult,
    BdTransport,
    SubprocessBd,
    err_line,
    names_bead,
    parse_json_tail,
)

DISTRIBUTION = "beadhive-bd-cli"

__all__ = [
    "DISTRIBUTION",
    "KICKOFF_REASON",
    "RELEASE_HOLD_MARKER",
    "BdResult",
    "BdTransport",
    "CliGateOperations",
    "CliIssues",
    "CliLeases",
    "CliMoleculeFiler",
    "CliPlanningGates",
    "CliStateOperations",
    "CliStateReads",
    "SubprocessBd",
    "coordination",
    "err_line",
    "names_bead",
    "parse_json_tail",
    "reads",
]
