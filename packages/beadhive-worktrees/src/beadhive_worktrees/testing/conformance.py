"""Reusable conformance kit for ``worktree.manager`` and ``workspace.binding`` providers.

Any provider of the two worktree slots runs the same semantic cases: the native Git manager
passes the manager cases today, the Herdr ``WorkspaceBinding`` (ADR bh-mr9tk.2 Option A)
passes the binding cases, and a future Herdr manager (Option B) runs the manager cases
unchanged. A provider proves itself by supplying a *harness* — the subject plus the read-only
oracles the cases need to observe what the provider actually did — never by reimplementing a
case.

Cases are plain callables over a freshly built harness, not pytest fixtures, so the kit runs
under any runner and ships with the package without a test-framework dependency. Each case
raises :class:`ConformanceFailure` (an ``AssertionError``) naming its stable ``case_id``.

Manager cases (the contract in ``beadhive_plugins.worktree_slots.WorktreeManager``):

* ``create.exact-path`` / ``create.exact-branch`` — the manager executes the spec's exact path
  and branch; it never chooses either.
* ``create.new-branch-only`` — ``create`` refuses a branch that already exists and leaves its
  tip where it was.
* ``attach.existing-branch`` — ``attach`` checks out an existing branch at the exact path and
  never moves its tip, even when the spec names a diverged ``base`` (E37).
* ``remove.keeps-branch`` — ``remove`` deletes the linked worktree, never the branch.
* ``remove.dirty-refusal`` — a dirty worktree is refused without ``force`` and removed with it.
* ``handle.round-trip`` — the returned handle mirrors the spec, and a handle re-derived from
  ``main`` + ``path`` alone (``WorktreeHandle.for_removal``) is enough to remove.

Binding cases (``WorkspaceBinding``, E28-E35):

* ``bind.records-reference`` / ``bind.idempotent`` — bind caches the presenter's reference on
  the handle; a repeated bind returns the same reference and one presentation (E34).
* ``bind.reference-rederivable`` — the cached reference equals the presenter's own inventory
  answer, and a lost reference is re-derived at release (E35).
* ``release.closes`` / ``release.unbound-noop`` / ``release.never-closes-foreign`` — release
  closes exactly this worktree's presentation, is a no-op for an unbound handle, and never
  closes another checkout's presentation through a stale reference.
* ``release.before-remove`` — through :class:`WorktreeLifecycleService`, the presentation is
  already released at the moment the manager's ``remove`` runs (E30, E31).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, Protocol, TypeVar

from beadhive_plugins.worktree_slots import WorkspaceBinding, WorktreeManager

from ..application import WorktreeLifecycleService
from ..contracts.ports import WorkspaceBindingPort, WorktreeManagerPort
from ..domain import WorktreeHandle, WorktreeManagerError, WorktreeRemoved, WorktreeSpec

H = TypeVar("H")
_CASE_ID = re.compile(r"^[a-z0-9]+(?:[.-][a-z0-9]+)*$")


class ConformanceFailure(AssertionError):
    """One named semantic case rejected a provider."""

    def __init__(self, *, suite: str, case_id: str, subject: str, detail: str) -> None:
        self.suite = suite
        self.case_id = case_id
        self.subject = subject
        self.detail = detail
        super().__init__(f"{suite} conformance failed for {subject!r} [{case_id}]: {detail}")


class Expectation:
    """One case's assertion helper: every failure names the suite, case, and subject."""

    def __init__(self, suite: str, case_id: str, subject: str) -> None:
        self._suite = suite
        self._case_id = case_id
        self._subject = subject

    def that(self, condition: object, detail: str) -> None:
        if not condition:
            raise ConformanceFailure(
                suite=self._suite, case_id=self._case_id, subject=self._subject, detail=detail
            )


@dataclass(frozen=True)
class ConformanceCase(Generic[H]):
    """A stable semantic scenario applied to one freshly built harness."""

    case_id: str
    description: str
    check: Callable[[H, Expectation], None]

    def __post_init__(self) -> None:
        if not _CASE_ID.fullmatch(self.case_id):
            raise ValueError(f"conformance case_id must be a stable identifier: {self.case_id!r}")


# ---- manager kit ---------------------------------------------------------------------------


class ManagerHarness(Protocol):
    """A ``worktree.manager`` under test plus the read-only oracles its cases observe.

    ``main`` is a repository main checkout whose default branch has at least one commit, and
    ``root`` an empty scratch directory in which the cases choose exact worktree paths. The
    oracles read the repository directly, never through the manager under test.
    """

    @property
    def manager(self) -> WorktreeManagerPort: ...

    @property
    def main(self) -> Path: ...

    @property
    def root(self) -> Path: ...

    @property
    def base(self) -> str:
        """The main checkout's default branch — a valid ``base`` for ``create``."""
        ...

    def seed_branch(self, branch: str) -> str:
        """Create ``branch`` with one commit diverged from ``base``, not checked out anywhere;
        return its tip."""
        ...

    def branch_tip(self, branch: str) -> str:
        """The branch's commit, or ``""`` if it does not exist."""
        ...

    def checked_out(self, path: Path) -> str:
        """The branch checked out in the worktree at ``path``, or ``""`` if none is there."""
        ...

    def registered(self, path: Path) -> bool:
        """Whether the repository still lists a linked worktree at ``path``."""
        ...

    def make_dirty(self, path: Path) -> None:
        """Leave an uncommitted change in the worktree at ``path``."""
        ...


def _spec(harness: ManagerHarness, leaf: str, branch: str, base: str = "") -> WorktreeSpec:
    return WorktreeSpec(harness.main, branch, harness.root / leaf, base)


def _create_exact_path(harness: ManagerHarness, expect: Expectation) -> None:
    before = set(harness.root.iterdir()) if harness.root.exists() else set()
    spec = _spec(harness, "exact-leaf", "wt/conformance/exact-path", harness.base)
    handle = harness.manager.create(spec)
    expect.that(handle.path == spec.path, f"handle path {handle.path} != spec path {spec.path}")
    expect.that(spec.path.is_dir(), f"no worktree directory at the exact path {spec.path}")
    expect.that(harness.registered(spec.path), f"{spec.path} is not a registered worktree")
    created = set(harness.root.iterdir()) - before
    expect.that(created == {spec.path}, f"manager created unexpected paths: {sorted(created)}")


def _create_exact_branch(harness: ManagerHarness, expect: Expectation) -> None:
    spec = _spec(harness, "branch-leaf", "wt/conformance/exact/branch", harness.base)
    handle = harness.manager.create(spec)
    expect.that(handle.branch == spec.branch, f"handle branch {handle.branch!r} != spec")
    expect.that(
        harness.checked_out(spec.path) == spec.branch,
        f"{spec.path} has {harness.checked_out(spec.path)!r} checked out, not {spec.branch!r}",
    )
    expect.that(
        harness.branch_tip(spec.branch) == harness.branch_tip(harness.base),
        "a created branch must fork from the spec's base",
    )


def _create_new_branch_only(harness: ManagerHarness, expect: Expectation) -> None:
    branch = "wt/conformance/already-there"
    tip = harness.seed_branch(branch)
    spec = _spec(harness, "exists-leaf", branch, harness.base)
    try:
        harness.manager.create(spec)
    except WorktreeManagerError:
        pass
    else:
        expect.that(False, "create accepted a branch that already exists")
    expect.that(harness.branch_tip(branch) == tip, "a refused create moved the existing branch")
    expect.that(not harness.registered(spec.path), "a refused create left a worktree behind")


def _attach_existing_branch(harness: ManagerHarness, expect: Expectation) -> None:
    branch = "wt/conformance/attach"
    tip = harness.seed_branch(branch)
    # The seeded branch has diverged from base, so honoring base would move its tip.
    spec = _spec(harness, "attach-leaf", branch, harness.base)
    handle = harness.manager.attach(spec)
    expect.that(handle.path == spec.path, f"attach used {handle.path}, not {spec.path}")
    expect.that(harness.checked_out(spec.path) == branch, "attach did not check out the branch")
    expect.that(harness.branch_tip(branch) == tip, "attach moved the existing branch tip")
    expect.that(handle.base == spec.base, "attach must record base on the handle as intent")


def _remove_keeps_branch(harness: ManagerHarness, expect: Expectation) -> None:
    spec = _spec(harness, "remove-leaf", "wt/conformance/remove", harness.base)
    handle = harness.manager.create(spec)
    tip = harness.branch_tip(spec.branch)
    removed = harness.manager.remove(handle, False)
    expect.that(isinstance(removed, WorktreeRemoved), "remove must return a WorktreeRemoved")
    expect.that(removed.handle.path == spec.path, "the receipt names a different worktree")
    expect.that(not spec.path.exists(), f"{spec.path} still exists after remove")
    expect.that(not harness.registered(spec.path), f"{spec.path} is still registered")
    expect.that(harness.branch_tip(spec.branch) == tip, "remove deleted or moved the branch")


def _remove_dirty_refusal(harness: ManagerHarness, expect: Expectation) -> None:
    spec = _spec(harness, "dirty-leaf", "wt/conformance/dirty", harness.base)
    handle = harness.manager.create(spec)
    harness.make_dirty(spec.path)
    try:
        harness.manager.remove(handle, False)
    except WorktreeManagerError as exc:
        expect.that(exc.returncode != 0, "a refusal must carry a non-zero exit code")
    else:
        expect.that(False, "remove without force discarded uncommitted work")
    expect.that(spec.path.is_dir() and harness.registered(spec.path), "refusal lost the worktree")
    harness.manager.remove(handle, True)
    expect.that(not harness.registered(spec.path), "force did not remove the dirty worktree")
    expect.that(harness.branch_tip(spec.branch) != "", "a forced remove deleted the branch")


def _handle_round_trip(harness: ManagerHarness, expect: Expectation) -> None:
    spec = WorktreeSpec(
        harness.main,
        "wt/conformance/round-trip",
        harness.root / "round-leaf",
        harness.base,
        identity="bead-7",
    )
    handle = harness.manager.create(spec)
    mirrored = WorktreeHandle.of(spec)
    for name in ("identity", "main", "path", "branch", "base"):
        expect.that(
            getattr(handle, name) == getattr(mirrored, name),
            f"handle.{name}={getattr(handle, name)!r} does not mirror the spec",
        )
    expect.that(handle.bindings == {}, "a manager that binds nothing must record no bindings")
    rederived = WorktreeHandle.for_removal(spec.main, spec.path)
    harness.manager.remove(rederived, False)
    expect.that(not harness.registered(spec.path), "a re-derived handle could not remove")
    reattached = harness.manager.attach(WorktreeSpec(spec.main, spec.branch, spec.path))
    expect.that(reattached.path == spec.path, "re-attach after remove used a different path")
    expect.that(harness.checked_out(spec.path) == spec.branch, "re-attach lost the branch")


def _declares_capabilities(harness: ManagerHarness, expect: Expectation) -> None:
    manager = harness.manager
    expect.that(isinstance(manager, WorktreeManager), "provider does not match WorktreeManager")
    expect.that(isinstance(manager.binds, tuple), "binds must be a tuple of presenters")
    expect.that(
        isinstance(manager.remove_releases_bindings, bool),
        "remove_releases_bindings must be a bool",
    )


MANAGER_CASES: tuple[ConformanceCase[ManagerHarness], ...] = tuple(
    ConformanceCase(case_id, description, check)
    for case_id, description, check in (
        ("manager.declares-capabilities", "matches the slot", _declares_capabilities),
        ("create.exact-path", "create executes the spec's exact path", _create_exact_path),
        ("create.exact-branch", "create executes the spec's exact branch", _create_exact_branch),
        ("create.new-branch-only", "create refuses an existing branch", _create_new_branch_only),
        ("attach.existing-branch", "attach never moves the tip", _attach_existing_branch),
        ("remove.keeps-branch", "remove never deletes the branch", _remove_keeps_branch),
        ("remove.dirty-refusal", "remove refuses dirty work without force", _remove_dirty_refusal),
        ("handle.round-trip", "handles mirror specs and re-derive", _handle_round_trip),
    )
)


# ---- binding kit ---------------------------------------------------------------------------


class BindingHarness(Protocol):
    """A ``workspace.binding`` under test plus the presenter-side oracles its cases observe.

    ``worktree(leaf)`` returns a handle for an existing worktree (created by whatever manager
    the harness uses) that is not yet bound. ``derive``/``presentations``/``is_live`` read the
    presenter's own inventory, never the handle's cached reference.
    """

    @property
    def binding(self) -> WorkspaceBindingPort: ...

    def worktree(self, leaf: str) -> WorktreeHandle: ...

    def derive(self, path: Path) -> str | None:
        """The presenter's reference for ``path`` from its own inventory, if it holds one."""
        ...

    def presentations(self, path: Path) -> int:
        """How many live presentations the presenter holds for ``path``."""
        ...

    def is_live(self, reference: str) -> bool:
        """Whether the presenter still holds a live presentation under ``reference``."""
        ...


def _reference(harness: BindingHarness, handle: WorktreeHandle) -> str:
    return handle.bindings.get(harness.binding.presenter, "")


def _bind_records_reference(harness: BindingHarness, expect: Expectation) -> None:
    handle = harness.worktree("records")
    bound = harness.binding.bind(handle)
    reference = _reference(harness, bound)
    expect.that(reference, "bind did not record a reference under the presenter's name")
    expect.that(_reference(harness, handle) == "", "bind mutated the caller's handle")
    for name in ("identity", "main", "path", "branch", "base"):
        expect.that(getattr(bound, name) == getattr(handle, name), f"bind changed handle.{name}")
    expect.that(harness.is_live(reference), "the recorded reference is not live")


def _bind_idempotent(harness: BindingHarness, expect: Expectation) -> None:
    handle = harness.worktree("idempotent")
    first = _reference(harness, harness.binding.bind(handle))
    again = _reference(harness, harness.binding.bind(handle))
    rebound = _reference(harness, harness.binding.bind(harness.binding.bind(handle)))
    expect.that(first == again == rebound, f"repeated binds disagree: {first}, {again}, {rebound}")
    expect.that(harness.presentations(handle.path) == 1, "repeated binds duplicated the workspace")


def _bind_reference_rederivable(harness: BindingHarness, expect: Expectation) -> None:
    handle = harness.worktree("rederive")
    bound = harness.binding.bind(handle)
    reference = _reference(harness, bound)
    expect.that(
        harness.derive(handle.path) == reference,
        "the cached reference differs from the presenter's own inventory",
    )
    # A bind intent recorded with no reference (a crash between bind and record) still releases.
    harness.binding.release(handle.with_binding(harness.binding.presenter, ""))
    expect.that(not harness.is_live(reference), "release did not re-derive a lost reference")
    expect.that(harness.presentations(handle.path) == 0, "release left a presentation behind")


def _release_closes(harness: BindingHarness, expect: Expectation) -> None:
    handle = harness.worktree("release")
    bound = harness.binding.bind(handle)
    reference = _reference(harness, bound)
    harness.binding.release(bound)
    expect.that(not harness.is_live(reference), "release left the presentation live")
    expect.that(harness.presentations(handle.path) == 0, "release left a presentation behind")
    rebound = harness.binding.bind(handle)
    expect.that(_reference(harness, rebound), "a released worktree could not be bound again")


def _release_unbound_noop(harness: BindingHarness, expect: Expectation) -> None:
    other = harness.binding.bind(harness.worktree("bystander"))
    handle = harness.worktree("unbound")
    harness.binding.release(handle)
    expect.that(harness.is_live(_reference(harness, other)), "an unbound release closed another")
    expect.that(harness.presentations(handle.path) == 0, "an unbound release bound something")


def _release_never_closes_foreign(harness: BindingHarness, expect: Expectation) -> None:
    mine = harness.binding.bind(harness.worktree("mine"))
    theirs = harness.binding.bind(harness.worktree("theirs"))
    foreign = _reference(harness, theirs)
    stale = mine.with_binding(harness.binding.presenter, foreign)
    harness.binding.release(stale)
    expect.that(harness.is_live(foreign), "a stale reference closed another checkout's workspace")
    expect.that(harness.presentations(mine.path) == 0, "release missed this worktree's workspace")


class _ReleaseCheckingManager:
    """A manager whose ``remove`` proves the presentation was released before it ran."""

    binds: tuple[str, ...] = ()
    remove_releases_bindings = False

    def __init__(self, harness: BindingHarness, expect: Expectation) -> None:
        self._harness = harness
        self._expect = expect
        self.removed: list[Path] = []

    def create(self, spec: WorktreeSpec) -> WorktreeHandle:
        return WorktreeHandle.of(spec)

    def attach(self, spec: WorktreeSpec) -> WorktreeHandle:
        return WorktreeHandle.of(spec)

    def remove(self, handle: WorktreeHandle, force: bool) -> WorktreeRemoved:
        del force
        self._expect.that(
            self._harness.presentations(handle.path) == 0,
            "the manager's remove ran while the presentation was still bound",
        )
        self.removed.append(handle.path)
        return WorktreeRemoved(handle)


def _release_before_remove(harness: BindingHarness, expect: Expectation) -> None:
    manager = _ReleaseCheckingManager(harness, expect)
    service = WorktreeLifecycleService(manager=manager, bindings=(harness.binding,))
    expect.that(service.bindings == (harness.binding,), "a native-style manager must compose it")
    bound = service.bind(harness.worktree("teardown"))
    expect.that(not bound.gaps, f"bind reported gaps: {bound.gaps}")
    removed = service.remove(bound.handle)
    expect.that(manager.removed == [bound.handle.path], "the manager's remove never ran")
    expect.that(not removed.gaps, f"release reported gaps: {removed.gaps}")


BINDING_CASES: tuple[ConformanceCase[BindingHarness], ...] = tuple(
    ConformanceCase(case_id, description, check)
    for case_id, description, check in (
        ("bind.records-reference", "bind caches the presenter reference", _bind_records_reference),
        ("bind.idempotent", "a repeated bind returns the same reference", _bind_idempotent),
        ("bind.reference-rederivable", "references re-derive", _bind_reference_rederivable),
        ("release.closes", "release closes this worktree's presentation", _release_closes),
        ("release.unbound-noop", "releasing an unbound handle is a no-op", _release_unbound_noop),
        ("release.never-closes-foreign", "stale refs stay local", _release_never_closes_foreign),
        ("release.before-remove", "release precedes the manager's remove", _release_before_remove),
    )
)


# ---- runners -------------------------------------------------------------------------------


def run_manager_case(case: ConformanceCase[ManagerHarness], harness: ManagerHarness) -> None:
    """Run one manager case against a freshly built harness."""
    subject = type(harness.manager).__name__
    case.check(harness, Expectation("worktree.manager", case.case_id, subject))


def run_binding_case(case: ConformanceCase[BindingHarness], harness: BindingHarness) -> None:
    """Run one binding case against a freshly built harness."""
    binding = harness.binding
    subject = f"{type(binding).__name__}({getattr(binding, 'presenter', '?')})"
    if not isinstance(binding, WorkspaceBinding):
        raise ConformanceFailure(
            suite="workspace.binding",
            case_id=case.case_id,
            subject=subject,
            detail="provider does not match the WorkspaceBinding slot",
        )
    case.check(harness, Expectation("workspace.binding", case.case_id, subject))


def assert_manager_conforms(
    factory: Callable[[], ManagerHarness],
    cases: Iterable[ConformanceCase[ManagerHarness]] = MANAGER_CASES,
) -> None:
    """Run every manager case, each on a fresh harness from ``factory``."""
    for case in cases:
        run_manager_case(case, factory())


def assert_binding_conforms(
    factory: Callable[[], BindingHarness],
    cases: Iterable[ConformanceCase[BindingHarness]] = BINDING_CASES,
) -> None:
    """Run every binding case, each on a fresh harness from ``factory``."""
    for case in cases:
        run_binding_case(case, factory())


__all__ = [
    "BINDING_CASES",
    "MANAGER_CASES",
    "BindingHarness",
    "ConformanceCase",
    "ConformanceFailure",
    "Expectation",
    "ManagerHarness",
    "assert_binding_conforms",
    "assert_manager_conforms",
    "run_binding_case",
    "run_manager_case",
]
