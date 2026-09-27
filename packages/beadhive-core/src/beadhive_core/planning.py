"""Molecule filing: Beadhive planning policy over the pinned Beads v1.3 session.

Beadhive owns the molecule *contract* — DAG order, root discovery, identity/dimension labels, the
kickoff-gate and swarm conventions — but never a second store or a second wire schema. Filing
compiles a validated Beadhive molecule spec ONCE into a single Beads v1.3.0 ``BatchApply`` request
(:func:`compile_molecule`): every issue create carries a stable request-local key derived from its
handle, and the epic-parent plus every declared dependency is a ``dep_add`` item in the SAME
request. The response's key-to-id map is the only fact the request cannot carry, and it is what
every later Beadhive operation (kickoff gates, release-hold gates, the swarm) addresses issues by.

``plan.batch-apply.atomic`` is **api-ready** (bh-sy36q.2: real-service atomicity, key resolution
and precondition-refusal proof closed the gap the bh-97fo0.3 matrix originally recorded — see
``test_core_planning_real_service.py``). ``SessionMoleculeFiler`` serves it over
:class:`beadhive_beads_client.BeadsSession`. The molecule's gate and state conventions — a `bd
gate` blocking each root, `kickoff=pending`, the `bd swarm create`, and the `release-hold:` gate —
have no v1.3 HTTP route (``plan.gate.*``, ``plan.kickoff.*`` stay ``cli-compatibility``) and are
therefore the narrow :class:`PlanningGates` port, which the compatibility shell implements over its
named ``bd`` routes. Molecule verification and repair are NOT this module's concern (bh-sy36q.2
narrows them out; see the bead's NOTES) — they stay on the existing ``bd``-read implementation
unchanged.

The compiler is PURE: given the same spec, identity labels and dimension-field vocabulary, it
always returns the same :class:`CompiledMolecule`. A `--dry-run` preview and a real filing compile
the identical request, so preview can never drift from what apply actually sends, and BatchApply
has no server-side validate-only mode — preview is this local compile, never a server dry run.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from beadhive_beads_client import (
    BeadsSession,
    IncompatibleService,
    IndeterminateWrite,
    ServiceProblem,
    SessionTimeout,
)
from beads_v1_3.models import (
    ApplyBatchRequest,
    ApplyCreateItem,
    ApplyDepAddItem,
    ApplyItem,
    ApplyItemKind,
    Ref,
)

from .routing import ApiRoute, RoutingObserver, default_table


def _api(name: str) -> str:
    route = default_table().route(name)
    if not isinstance(route, ApiRoute):
        raise RuntimeError(f"{name} is {route.kind.value}, not api-ready")
    return route.name


#: The api-ready BatchApply route :class:`SessionMoleculeFiler` serves over ``BeadsSession``.
BATCH_APPLY_ROUTE = _api("plan.batch-apply.atomic")
#: The gate/kickoff/swarm rows the :class:`PlanningGates` port stands in for.
PLANNING_GATE_ROUTES = tuple(
    default_table().select_cli(name).name for name in ("plan.gate.create", "plan.kickoff.update")
)
#: Every named route this cohort touches, for traceability (see the package README).
PLANNING_ROUTES = (BATCH_APPLY_ROUTE, *PLANNING_GATE_ROUTES)

#: Capabilities a planning session must negotiate before any read or write is attempted.
PLANNING_CAPABILITIES = frozenset({"project.enforce", "issues.get", "issues.batchApply"})

#: `BatchApply` bounds `items` to at most this many entries (bh-97fo0.3 client evidence); an
#: atomic graph over the cap is refused rather than silently split into non-atomic requests.
BATCH_ITEM_CAP = 100

#: The dependency edge type a declared molecule ``deps`` entry lowers to (a sibling "blocks" the
#: issue that names it as a dependency — see ``_epic_molecule``'s read side, unchanged by this
#: cohort, for the matching reverse selector).
BLOCKS_EDGE = "blocks"
#: The edge type an issue-to-epic (or adopted-report-to-epic) ownership edge lowers to.
PARENT_CHILD_EDGE = "parent-child"

#: The compiled epic's request-local key; issue keys carry an ``issue:`` prefix so a molecule
#: handle can never collide with it.
EPIC_KEY = "epic"


def issue_key(handle: str) -> str:
    """The stable request-local key one issue handle compiles to (never collides with
    :data:`EPIC_KEY`, whatever a spec author names a handle)."""
    return f"issue:{handle}"


Issue = Mapping[str, Any]


# ---- pure domain: molecule DAG order -------------------------------------------------------


class PlanningError(Exception):
    """A planning application operation could not satisfy its contract."""


@dataclass(frozen=True, slots=True)
class MoleculeGraph:
    """Validated dependency order over planner-local issue handles."""

    order: tuple[str, ...]
    roots: tuple[str, ...]

    @classmethod
    def from_issues(cls, issues: Sequence[Mapping[str, Any]]) -> MoleculeGraph:
        handles = tuple(str(issue["handle"]) for issue in issues)
        if len(set(handles)) != len(handles):
            raise PlanningError("molecule issue handles must be unique")
        known = set(handles)
        dependencies = {
            handle: tuple(str(dep) for dep in (issue.get("deps") or ()))
            for handle, issue in zip(handles, issues, strict=True)
        }
        unknown = sorted(
            {dep for deps in dependencies.values() for dep in deps if dep not in known}
        )
        if unknown:
            raise PlanningError(f"molecule dependencies name unknown handles: {', '.join(unknown)}")

        indegree = {handle: len(dependencies[handle]) for handle in handles}
        ready = [handle for handle in handles if indegree[handle] == 0]
        ordered: list[str] = []
        while ready:
            current = ready.pop(0)
            ordered.append(current)
            for handle in handles:
                if current in dependencies[handle]:
                    indegree[handle] -= 1
                    if indegree[handle] == 0:
                        ready.append(handle)
        if len(ordered) != len(handles):
            raise PlanningError("molecule dependency graph contains a cycle")
        return cls(tuple(ordered), tuple(handle for handle in handles if not dependencies[handle]))

    def ordered_issues(self, issues: Sequence[Mapping[str, Any]]) -> tuple[Mapping[str, Any], ...]:
        by_handle = {str(issue["handle"]): issue for issue in issues}
        return tuple(by_handle[handle] for handle in self.order)


# ---- pure compiler: molecule spec -> one BatchApply request --------------------------------


class MoleculeTooLarge(PlanningError):
    """The compiled request would exceed BatchApply's 100-item cap.

    Refused outright rather than silently split: splitting an atomic dependency graph across
    requests would change what the end-gate cycle check can see, and each half would land (or
    fail) independently of the other.
    """

    def __init__(self, total: int, cap: int = BATCH_ITEM_CAP) -> None:
        self.total = total
        self.cap = cap
        super().__init__(
            f"molecule compiles to {total} BatchApply items (creates + edges), over the cap of "
            f"{cap} — refusing to file; split the molecule by hand (no automatic chunking)"
        )


@dataclass(frozen=True)
class CompiledMolecule:
    """The exact ``BatchApply`` request one molecule spec lowers to, plus the bookkeeping later
    Beadhive operations (kickoff gates, release-hold gates) need to address the ids it mints."""

    items: tuple[ApplyItem, ...]
    graph: MoleculeGraph
    epic_key: str
    issue_keys: Mapping[str, str]
    create_count: int
    edge_count: int

    def request(self, *, actor: str, provenance: str = "") -> ApplyBatchRequest:
        kwargs: dict[str, Any] = {"actor": actor, "items": list(self.items)}
        if provenance:
            kwargs["provenance"] = provenance
        return ApplyBatchRequest(**kwargs)


def _dimension_labels(item: Mapping[str, Any], dimension_fields: Sequence[str]) -> list[str]:
    return [
        f"{field}:{item[field]}" for field in dimension_fields if item.get(field) not in (None, "")
    ]


def _labels_for(
    item: Mapping[str, Any], dimension_fields: Sequence[str], identity_labels: Sequence[str]
) -> list[str]:
    return _dimension_labels(item, dimension_fields) + list(identity_labels)


def _create_item(
    key: str,
    item: Mapping[str, Any],
    *,
    issue_type: str,
    dimension_fields: Sequence[str],
    identity_labels: Sequence[str],
    acceptance: str = "",
    priority: int | None = None,
    external_ref: str = "",
) -> ApplyItem:
    create = ApplyCreateItem(
        title=str(item["title"]),
        key=key,
        issue_type=issue_type,
        labels=_labels_for(item, dimension_fields, identity_labels),
    )
    if item.get("description"):
        create.description = str(item["description"])
    if item.get("design"):
        create.design = str(item["design"])
    if acceptance:
        create.acceptance_criteria = acceptance
    if priority is not None:
        create.priority = priority
    if external_ref:
        create.external_ref = external_ref
    return ApplyItem(kind=ApplyItemKind.CREATE, create=create)


def _dep_add_item(*, source: Ref, target: Ref, type_: str) -> ApplyItem:
    return ApplyItem(
        kind=ApplyItemKind.DEP_ADD,
        dep_add=ApplyDepAddItem(source=source, target=target, type_=type_),
    )


def compile_molecule(
    spec: Mapping[str, Any],
    *,
    dimension_fields: Sequence[str],
    identity_labels: Sequence[str] = (),
    epic_id: str | None = None,
    epic_external_ref: str = "",
    adopted_reports: Sequence[str] = (),
    cap: int = BATCH_ITEM_CAP,
) -> CompiledMolecule:
    """Compile one validated molecule spec into a single ordered ``BatchApply`` plan.

    ``epic_id`` names an ALREADY-EXISTING epic (an adopted molecule whose epic was born via ``bd
    import`` to carry native ``source_system`` provenance — a field ``ApplyCreateItem`` has no
    member for). When given, no epic create item is emitted and every edge addresses the epic by
    that id instead of a request-local key; every other molecule primitive — child creates, the
    parent-child and declared-dependency edges, and any adopted-report link — still compiles into
    the ONE request. ``adopted_reports`` are pre-existing report ids linked child-of the epic
    (child-owns-report direction: the epic never blocks on an open report).

    Raises :class:`MoleculeTooLarge` before returning anything if the compiled request would
    exceed ``cap`` items — this function never mutates anything, so raising here is side-effect
    free by construction, not merely a promise the caller has to keep.
    """
    epic = spec["epic"]
    issues = spec.get("issues") or ()
    graph = MoleculeGraph.from_issues(issues)
    keys = {handle: issue_key(handle) for handle in graph.order}
    epic_ref = Ref(id=epic_id) if epic_id else Ref(key=EPIC_KEY)

    items: list[ApplyItem] = []
    if epic_id is None:
        items.append(
            _create_item(
                EPIC_KEY,
                epic,
                issue_type="epic",
                dimension_fields=dimension_fields,
                identity_labels=identity_labels,
                external_ref=epic_external_ref,
            )
        )
    by_handle = {str(issue["handle"]): issue for issue in issues}
    for handle in graph.order:
        issue = by_handle[handle]
        priority = issue.get("priority")
        items.append(
            _create_item(
                keys[handle],
                issue,
                issue_type=str(issue.get("type") or "task"),
                dimension_fields=dimension_fields,
                identity_labels=identity_labels,
                acceptance=str(issue.get("acceptance") or ""),
                priority=int(priority) if priority not in (None, "") else None,
            )
        )
    for handle in graph.order:
        items.append(
            _dep_add_item(source=Ref(key=keys[handle]), target=epic_ref, type_=PARENT_CHILD_EDGE)
        )
    for handle in graph.order:
        for dep in by_handle[handle].get("deps") or ():
            items.append(
                _dep_add_item(
                    source=Ref(key=keys[handle]), target=Ref(key=keys[str(dep)]), type_=BLOCKS_EDGE
                )
            )
    for report_id in adopted_reports:
        items.append(
            _dep_add_item(source=Ref(id=report_id), target=epic_ref, type_=PARENT_CHILD_EDGE)
        )

    if len(items) > cap:
        raise MoleculeTooLarge(len(items), cap)

    create_count = len(graph.order) + (0 if epic_id else 1)
    edge_count = len(items) - create_count
    return CompiledMolecule(tuple(items), graph, EPIC_KEY, keys, create_count, edge_count)


# ---- ports -----------------------------------------------------------------------------------


class MoleculeFilingFailed(RuntimeError):
    """The BatchApply request was refused or its outcome is unknown."""

    def __init__(self, detail: str = "") -> None:
        self.detail = detail
        super().__init__(detail or "molecule filing failed")


@dataclass(frozen=True)
class FilingOutcome:
    """The compiled request's landed effect: every key resolved to the id it was bound to."""

    ids: Mapping[str, str]

    def issue_id(self, handle: str) -> str:
        return self.ids[issue_key(handle)]


class MoleculeFiler(Protocol):
    """``plan.batch-apply.atomic``: submit one compiled molecule and resolve its keys."""

    def apply(self, compiled: CompiledMolecule, *, actor: str) -> FilingOutcome:
        """Raise :class:`MoleculeFilingFailed` on refusal or an indeterminate outcome."""
        ...


class GateCreateFailed(RuntimeError):
    """A named gate/kickoff/swarm compatibility route did not land."""


class PlanningGates(Protocol):
    """``plan.gate.create`` / ``plan.kickoff.update`` — cli-compatibility (no v1.3 HTTP route)."""

    def create_swarm(self, epic_id: str, *, actor: str) -> bool:
        """``bd swarm create``; True on success."""
        ...

    def create_kickoff_gate(self, root_id: str, epic_id: str, *, actor: str) -> None:
        """Open the ONE authoritative kickoff gate for one molecule root."""
        ...

    def set_kickoff_pending(self, epic_id: str, *, actor: str) -> None:
        """Stamp ``kickoff=pending`` — filed, awaiting approval."""
        ...

    def create_release_hold_gate(self, bead_id: str, epic_id: str, *, actor: str) -> None:
        """Open the release-hold gate for one ``release:breaking`` bead."""
        ...


# ---- the api-ready BatchApply route ---------------------------------------------------------


class SessionMoleculeFiler:
    """:class:`MoleculeFiler` over an open ``BeadsSession`` — the api-ready route."""

    route = "api-ready"

    def __init__(self, session: BeadsSession, *, observer: RoutingObserver | None = None) -> None:
        self._session = session
        self._observer = observer

    def apply(self, compiled: CompiledMolecule, *, actor: str) -> FilingOutcome:
        request = compiled.request(actor=actor)
        try:
            response = default_table().call_api(
                self._session, BATCH_APPLY_ROUTE, request, observer=self._observer
            )
        except IndeterminateWrite as exc:
            raise MoleculeFilingFailed(
                "molecule filing outcome unknown — the write may have committed; read the "
                "epic before retrying (never replayed automatically)"
            ) from exc
        except ServiceProblem as exc:
            raise MoleculeFilingFailed(f"molecule filing refused by Beads: {exc}") from exc
        except (SessionTimeout, IncompatibleService, RuntimeError) as exc:
            raise MoleculeFilingFailed(f"molecule filing failed: {exc}") from exc
        ids = {key: response.keys[key] for key in response.keys.additional_keys}
        return FilingOutcome(ids)


# ---- handlers ----------------------------------------------------------------------------


@dataclass(frozen=True)
class FileOutcome:
    epic_id: str
    issue_count: int
    root_count: int
    adopt_count: int = 0


class PlanningCommands:
    """Molecule filing: compile once, submit once, then open the gate/kickoff/swarm conventions
    the compiled request could not carry (bh-sy36q.2 narrows verify/repair out — see the bead's
    NOTES; they stay on the pre-existing ``bd``-read implementation, unchanged)."""

    def __init__(self, filer: MoleculeFiler, gates: PlanningGates) -> None:
        self._filer = filer
        self._gates = gates

    def file(
        self,
        spec: Mapping[str, Any],
        *,
        actor: str,
        dimension_fields: Sequence[str],
        identity_labels: Sequence[str] = (),
        epic_id: str | None = None,
        epic_external_ref: str = "",
        adopted_reports: Sequence[str] = (),
        release_breaking_handles: Sequence[str] = (),
        cap: int = BATCH_ITEM_CAP,
    ) -> FileOutcome:
        compiled = compile_molecule(
            spec,
            dimension_fields=dimension_fields,
            identity_labels=identity_labels,
            epic_id=epic_id,
            epic_external_ref=epic_external_ref,
            adopted_reports=adopted_reports,
            cap=cap,
        )
        outcome = self._filer.apply(compiled, actor=actor)
        filed_epic_id = epic_id or outcome.ids[compiled.epic_key]
        if not self._gates.create_swarm(filed_epic_id, actor=actor):
            raise GateCreateFailed(
                f"created epic {filed_epic_id} but `bd swarm create` failed — inspect the hive"
            )
        for root_handle in compiled.graph.roots:
            self._gates.create_kickoff_gate(
                outcome.issue_id(root_handle), filed_epic_id, actor=actor
            )
        self._gates.set_kickoff_pending(filed_epic_id, actor=actor)
        for handle in release_breaking_handles:
            self._gates.create_release_hold_gate(
                outcome.issue_id(handle), filed_epic_id, actor=actor
            )
        return FileOutcome(
            filed_epic_id,
            len(compiled.graph.order),
            len(compiled.graph.roots),
            len(adopted_reports),
        )


__all__ = [
    "BATCH_APPLY_ROUTE",
    "BATCH_ITEM_CAP",
    "BLOCKS_EDGE",
    "EPIC_KEY",
    "PARENT_CHILD_EDGE",
    "PLANNING_CAPABILITIES",
    "PLANNING_GATE_ROUTES",
    "PLANNING_ROUTES",
    "CompiledMolecule",
    "FileOutcome",
    "FilingOutcome",
    "GateCreateFailed",
    "Issue",
    "MoleculeFiler",
    "MoleculeFilingFailed",
    "MoleculeGraph",
    "MoleculeTooLarge",
    "PlanningCommands",
    "PlanningError",
    "PlanningGates",
    "SessionMoleculeFiler",
    "compile_molecule",
    "issue_key",
]
