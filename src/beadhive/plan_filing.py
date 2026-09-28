"""Top-level adapter selecting the ``beadhive_core`` molecule-filing handler (bh-sy36q.2).

The one composition seam for `bh plan file`'s (and `plan_file`'s MCP tool) mutating half: compile
the validated molecule spec ONCE (:func:`beadhive_core.planning.compile_molecule`) and submit it
through whichever route is selected BEFORE the first Beads operation, never as a retry after one
fails — the same rule :mod:`beadhive.work_queue` (bh-l5sxi.2) and :mod:`beadhive.work_lifecycle`
(bh-sy36q.1) already use:

* **``plan.batch-apply.atomic`` (api-ready)** when the hive's one supervised Beads v1.3 service
  (``bh host beads``) can be reached: :class:`SessionMoleculeFiler` submits the compiled request
  and resolves its key-to-id map in ONE HTTP call.
* **cli-compatibility** otherwise (an embedded-Dolt hive Beads 1.3 cannot serve, no service
  running, a missing capability), when the hive's ``work.beads.route`` allows it (bh-m36pc; the
  default ``api`` fails closed instead): ``CliMoleculeFiler`` walks the identical compiled item
  list one ``bd create`` / ``bd dep add`` at a time. It is a thin interpreter of the SAME
  :class:`~beadhive_core.planning.CompiledMolecule`, not a second implementation of the molecule
  contract — the compiler is the one place that decides what a spec lowers to, on both routes.

The molecule's gate/kickoff/swarm conventions (``plan.gate.create`` / ``plan.kickoff.update``,
always cli-compatibility — v1.3 has no HTTP route for either) are ``CliPlanningGates``, the
SAME ``bd`` calls `bh plan repair` shares, so both callers share one implementation of the
kickoff-gate contract, never two that could drift.

Both ``bd`` routes live in the ``beadhive-bd-cli`` library package (bh-o3xuf), resolved lazily
by name through :mod:`beadhive.bd_cli` and run over root's own ``bd`` invocation seam.

``beadhive_core`` is resolved lazily by name; ``src/beadhive`` never imports a workspace package
statically (``scripts/check_package_imports.py``).
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import adopt, bd, bd_cli, beads_routing, config, log, molecule, registry
from .identity import workspace_identity

_CORE_MODULE = "beadhive_core"


def _core() -> Any:
    return importlib.import_module(_CORE_MODULE)


class PlanError(Exception):
    """A planning operation could not satisfy its contract (``beadhive.plan.PlanError``).

    Defined here, not in :mod:`beadhive.plan`, because ``plan`` imports this module; the retired
    ``beadhive.modules.planning.PlanningError`` it replaces was the same plain exception
    (bh-sy36q.6)."""


def molecule_graph(issues: Any) -> Any:
    """The spec's dependency order and roots — :class:`beadhive_core.MoleculeGraph`, the ONE
    implementation the compiler also lowers through, so `bh plan show` / `--dry-run` / the
    `plan_file` preview can never order a molecule differently than filing does (bh-sy36q.6
    retired the root-side duplicate). A malformed graph raises ``ValueError``, as it always has."""
    core = _core()
    try:
        return core.MoleculeGraph.from_issues(issues)
    except core.PlanningError as exc:
        raise ValueError(str(exc)) from exc


class TelemetryRoutingObserver:
    def selected(self, name: str, kind: str) -> None:
        log.get_logger("beadhive.plan").info("route_selected", operation=name, route=kind)


# ---- route selection ---------------------------------------------------------------------------


def hive_session(main: Path, entry: Any) -> Any:
    """An unopened session for this cohort's ``PLANNING_CAPABILITIES`` — see
    :func:`beadhive.beads_routing.hive_session` (the one composition decision, including the
    ``BH_BEADS_ROUTE=cli`` rollback)."""
    return beads_routing.hive_session(main, entry, _core().PLANNING_CAPABILITIES)


#: The session seam: ``(main, entry)`` -> an unopened ``BeadsSession``. Tests substitute a
#: transport-fixture session here; production resolves the hive's supervised service.
session_factory = hive_session


def _unavailable_errors() -> tuple[type[BaseException], ...]:
    return (*beads_routing.unavailable_errors(), OSError, ValueError)


@contextmanager
def _filer(main: Path, entry: Any) -> Iterator[Any]:
    """The ``MoleculeFiler`` port, its route selected before the first Beads operation.

    Only OPENING the session is inside the fallback's try/except (mirrors
    ``work_lifecycle.SelectedIssues``): once a route is selected, using it is outside any
    exception mapping, so a genuine failure from the caller's own filing call is never
    misdiagnosed as "service unavailable" and silently rerouted mid-command.
    """
    core = _core()
    try:
        session = session_factory(main, entry)
        session.open()
    except _unavailable_errors() as exc:
        beads_routing.allow_cli_route(entry, exc)
        log.get_logger("beadhive.plan").info(
            "planning_route_fallback", operation=core.BATCH_APPLY_ROUTE, detail=str(exc)
        )
        yield bd_cli.molecule_filer(main)
        return
    try:
        yield core.SessionMoleculeFiler(session, observer=TelemetryRoutingObserver())
    finally:
        session.close()


# ---- composition --------------------------------------------------------------------------------


def identity_labels(cwd: Path) -> tuple[str, ...]:
    """The provider/org/repo identity labels for ``cwd``, as INDIVIDUAL entries (BatchApply's
    ``labels`` is the complete authoritative list, unlike ``bd create``'s comma-joined ``-l``
    flag — no comma-splitting hack is needed here)."""
    ident = workspace_identity(cwd)
    return (
        ()
        if ident is None
        else tuple(f"{k}:{v}" for k, v in zip(("provider", "org", "repo"), ident, strict=True))
    )


def _dimension_labels(item: dict, dimension_fields: tuple[str, ...]) -> list[str]:
    return [
        f"{field}:{item[field]}" for field in dimension_fields if item.get(field) not in (None, "")
    ]


def import_epic(epic: dict, dimension_fields: tuple[str, ...], cwd: Path, actor: str) -> str:
    """Birth an epic carrying native ``source_system`` provenance via ``bd import`` — the
    sanctioned path for a field settable only at bead creation (no ``bd create``/``update`` flag
    exists). ``ApplyCreateItem`` has no member for it either, so this is the one piece of filing
    BatchApply genuinely cannot express — every other molecule primitive (child issues, the
    parent-child and declared-dependency edges, any adopted-report link) still compiles into the
    ONE BatchApply request that follows, addressing this epic by the id returned here rather than
    a request-local key. Only called when ``adopt.provenance_of(epic)`` names a ``source_system``.
    """
    labels = _dimension_labels(epic, dimension_fields) + list(identity_labels(cwd))
    record = adopt.epic_import_record(epic, labels)
    result = bd.run(
        ["import", "-", "--json"], cwd, actor=actor, capture=True, text_input=json.dumps(record)
    )
    data = json.loads(result.stdout or "null") if result.returncode == 0 and result.stdout else None
    ids = data.get("ids") if isinstance(data, dict) else None
    if result.returncode != 0 or not ids:
        raise PlanError(f"bd import failed ({(result.stderr or '').strip() or 'no id returned'})")
    return str(ids[0])


def file(spec: dict, cwd: Path, actor: str, cfg: Any) -> Any:
    """File one validated molecule spec: compile once, submit once, then open the gate/kickoff/
    swarm conventions the compiled request could not carry. Returns a ``beadhive_core.FileOutcome``.

    Raises :class:`PlanError` (``plan.py``'s ``PlanError``) on any
    refusal — the compiled request over the 100-item cap, a refused/indeterminate BatchApply
    submission, or a failed gate/kickoff/swarm convention — so the CLI and MCP callers' existing
    ``except PlanError`` handling covers this path unchanged.
    """
    core = _core()
    entry = registry.entry_for_dir(cfg, cwd)
    epic = spec["epic"]
    issues = spec.get("issues") or ()
    dimension_fields = molecule.DIMENSION_FIELDS
    source_system, external_ref = adopt.provenance_of(epic)
    epic_id = import_epic(epic, dimension_fields, cwd, actor) if source_system else None
    release_breaking_handles = (
        tuple(
            str(issue["handle"])
            for issue in issues
            if str(issue.get("release") or "") == "breaking"
        )
        if config.release_enforce_hold(cfg, entry)
        else ()
    )
    try:
        with _filer(cwd, entry) as filer:
            commands = core.PlanningCommands(filer, bd_cli.planning_gates(cwd))
            return commands.file(
                spec,
                actor=actor,
                dimension_fields=dimension_fields,
                identity_labels=identity_labels(cwd),
                epic_id=epic_id,
                epic_external_ref="" if epic_id else external_ref,
                adopted_reports=tuple(adopt.adopts_of(epic)),
                release_breaking_handles=release_breaking_handles,
            )
    except (core.PlanningError, core.MoleculeFilingFailed, core.GateCreateFailed) as exc:
        raise PlanError(str(exc)) from exc
