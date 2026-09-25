"""A backend-neutral fixture repository for impact-backend conformance.

The fixture is data, not a checkout: a handful of graph units (owners of paths), the dependency
edges between them, the attest keys that select them, and named change scenarios. It carries its
own ground truth, so a case can say which keys a change *must* invalidate without asking any
build system. A backend harness translates the same fixture into its tool's native answers (for
Pants, ``peek`` rows; for Turborepo, a ``--dry=json`` graph) and hands them to its backend through
the tool-query injection point, so the kit runs without the real tool.

The fixture contains, deliberately and nothing more:

- **owners**: every path the head tree contains is owned by exactly one unit, except one;
- **a dependent chain**: ``core`` <- ``service`` <- ``cli`` <- ``cli-tests`` (selected by ``unit``);
- **a global input**: a build-configuration path owned by ``build-config`` (rule 2);
- **an unowned path**: ``stray/notes.txt``, owned by nothing (rule 1);
- **a deletion**: ``app/helpers.py``, owned by ``service`` in the base tree only (rule 1);
- **a rename**: ``app/util_old.py`` -> ``app/util.py``, both owned by ``core`` (split by git into a
  deletion and an addition);
- **an unproven unit**: ``legacy-tests`` (rule 4), a unit whose inputs are not enforced;
- **a git-metadata key**: ``release-pin`` has no selector for any backend (rule 5), even though a
  unit carries the tag a name-based convention would guess for it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from ...modules.work.domain.impact import AttestKey, ChangedPath, KeyPolicy

#: Revisions the kit asks the resolver about; :class:`FixtureTreeDiff` maps them to trees.
BASE_REV = "fixture-base"
HEAD_REV = "fixture-head"
#: Tree ids of the fixture's two sides. A harness that checks its checkout (Pants compares
#: ``HEAD^{tree}``) reports :data:`HEAD_TREE` as the current tree.
BASE_TREE = "fixture-base-tree"
HEAD_TREE = "fixture-head-tree"

DEFAULT_GLOBAL_INPUT = "build/global.toml"

#: Unit kinds. A harness maps them onto its own vocabulary (Pants category tags, for example).
UNIT_KINDS = ("code", "test", "docs", "build")

# Key names.
KEY_UNIT = "unit"
KEY_DOCS = "docs"
KEY_LEGACY = "legacy"
KEY_ORPHAN = "orphan"
KEY_GIT_METADATA = "release-pin"

# Paths with a role in a scenario.
CHAIN_ROOT = "app/core.py"
LEAF_DOC = "docs/guide.md"
UNOWNED_PATH = "stray/notes.txt"
DELETED_PATH = "app/helpers.py"
RENAMED_FROM = "app/util_old.py"
RENAMED_TO = "app/util.py"
UNPROVEN_SOURCE = "tests/test_legacy.py"

# Scenario names.
UNCHANGED = "unchanged"
LEAF = "leaf"
CHAIN = "chain"
UNOWNED = "unowned"
GLOBAL = "global"
DELETION = "deletion"
RENAME = "rename"
UNPROVEN_EDIT = "unproven-edit"


def default_selector(key: str) -> str:
    """The selector value a key uses unless the harness names its own (Pants' tag shape)."""
    return f"attest:{key}"


@dataclass(frozen=True)
class FixtureUnit:
    """One graph unit. ``sources`` are the paths it owns in the head tree; ``base_sources`` the
    paths it owned only in the base tree (deleted or renamed away). ``keys`` names the keys whose
    selector tag the unit carries; ``proven`` is whether its declared inputs are enforced."""

    id: str
    kind: str
    sources: tuple[str, ...]
    base_sources: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    keys: tuple[str, ...] = ()
    proven: bool = True

    def __post_init__(self) -> None:
        if self.kind not in UNIT_KINDS:
            raise ValueError(f"fixture unit {self.id!r}: kind must be one of {UNIT_KINDS}")


@dataclass(frozen=True)
class ImpactScenario:
    """One named change between the fixture's base and head trees."""

    name: str
    changes: tuple[ChangedPath, ...]
    base_tree: str = BASE_TREE
    head_tree: str = HEAD_TREE

    @property
    def changed_paths(self) -> tuple[str, ...]:
        return tuple(sorted({change.path for change in self.changes}))


class FixtureTreeDiff:
    """The kit's :class:`TreeDiffPort`: the scenario's trees and changed paths, no git."""

    def __init__(self, scenario: ImpactScenario) -> None:
        self._scenario = scenario

    def tree_of(self, repo: str, rev: str) -> str:
        trees = {BASE_REV: self._scenario.base_tree, HEAD_REV: self._scenario.head_tree}
        if rev not in trees:
            raise KeyError(f"fixture has no revision {rev!r}")
        return trees[rev]

    def changed_paths(self, repo: str, base_tree: str, head_tree: str) -> tuple[ChangedPath, ...]:
        return () if base_tree == head_tree else self._scenario.changes


@dataclass(frozen=True)
class ImpactFixture:
    """The fixture repository plus its ground truth, built for one backend name."""

    backend: str
    units: tuple[FixtureUnit, ...]
    keys: tuple[AttestKey, ...]
    scenarios: Mapping[str, ImpactScenario]
    global_input: str
    selector: Callable[[str], str] = field(default=default_selector, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "scenarios", MappingProxyType(dict(self.scenarios)))
        ids = [unit.id for unit in self.units]
        if len(ids) != len(set(ids)):
            raise ValueError(f"duplicate fixture unit ids: {ids}")
        for unit in self.units:
            missing = set(unit.depends_on) - set(ids)
            if missing:
                raise ValueError(f"fixture unit {unit.id!r} depends on unknown {sorted(missing)}")

    @property
    def key_names(self) -> tuple[str, ...]:
        return tuple(key.name for key in self.keys)

    def key(self, name: str) -> AttestKey:
        return next(key for key in self.keys if key.name == name)

    def unit(self, unit_id: str) -> FixtureUnit:
        return next(unit for unit in self.units if unit.id == unit_id)

    def scenario(self, name: str) -> ImpactScenario:
        return self.scenarios[name]

    def tree_paths(self, side: str) -> tuple[str, ...]:
        """Every path of one side (``"base"`` or ``"head"``), for a harness that materializes the
        fixture on disk for a real-tool lane."""
        if side not in {"base", "head"}:
            raise ValueError("side must be 'base' or 'head'")
        head = {path for unit in self.units for path in unit.sources} | {UNOWNED_PATH}
        if side == "head":
            return tuple(sorted(head))
        added = {RENAMED_TO, UNOWNED_PATH}
        base = (head - added) | {path for unit in self.units for path in unit.base_sources}
        return tuple(sorted(base))

    def tags(self, unit: FixtureUnit) -> tuple[str, ...]:
        """The selector values ``unit`` carries: one per key it names."""
        return tuple(sorted({self.selector(key) for key in unit.keys}))

    def owners(self, change: ChangedPath) -> tuple[str, ...]:
        """Ground-truth owner unit ids; a deleted path is attributed through the base tree."""
        if change.deleted:
            return tuple(
                unit.id
                for unit in self.units
                if change.path in unit.base_sources or change.path in unit.sources
            )
        return tuple(unit.id for unit in self.units if change.path in unit.sources)

    def affected_units(self, changes: Iterable[ChangedPath]) -> frozenset[str]:
        """Owners of the changes plus every unit transitively depending on them."""
        affected = {owner for change in changes for owner in self.owners(change)}
        grew = True
        while grew:
            grew = False
            for unit in self.units:
                if unit.id not in affected and affected.intersection(unit.depends_on):
                    affected.add(unit.id)
                    grew = True
        return frozenset(affected)

    def selected_units(self, key_name: str) -> tuple[str, ...]:
        """Units this backend's selector for ``key_name`` selects; none without a selector."""
        value = self.key(key_name).selector(self.backend)
        if value is None:
            return ()
        return tuple(unit.id for unit in self.units if value in self.tags(unit))

    def graph_invalidated(self, scenario: ImpactScenario) -> frozenset[str]:
        """Keys whose selected units a scenario's change reaches through the graph alone."""
        affected = self.affected_units(scenario.changes)
        return frozenset(
            name for name in self.key_names if affected.intersection(self.selected_units(name))
        )


def build_impact_fixture(
    backend: str,
    *,
    selector: Callable[[str], str] = default_selector,
    global_input: str = DEFAULT_GLOBAL_INPUT,
) -> ImpactFixture:
    """Build the conformance fixture for ``backend``.

    ``selector`` maps a key name to the backend's selector value (a Pants tag, a Turborepo task
    name, a Bazel tag). ``global_input`` is the path the backend treats as a global build input
    (``pants.toml``, ``turbo.json``, ``MODULE.bazel``); the fixture makes it owned, so rule 1
    cannot mask rule 2.
    """
    if not backend:
        raise ValueError("fixture backend name must be non-empty")
    units = (
        FixtureUnit("core", "code", (CHAIN_ROOT, RENAMED_TO), base_sources=(RENAMED_FROM,)),
        FixtureUnit(
            "service",
            "code",
            ("app/service.py",),
            base_sources=(DELETED_PATH,),
            depends_on=("core",),
        ),
        FixtureUnit("cli", "code", ("app/cli.py",), depends_on=("service",)),
        FixtureUnit(
            "cli-tests", "test", ("tests/test_cli.py",), depends_on=("cli",), keys=(KEY_UNIT,)
        ),
        FixtureUnit("docs", "docs", (LEAF_DOC,), keys=(KEY_DOCS,)),
        FixtureUnit("legacy-tests", "test", (UNPROVEN_SOURCE,), keys=(KEY_LEGACY,), proven=False),
        FixtureUnit("release-tests", "test", ("tests/test_release.py",), keys=(KEY_GIT_METADATA,)),
        FixtureUnit("build-config", "build", (global_input,)),
    )
    owned = [path for unit in units for path in (*unit.sources, *unit.base_sources)]
    if len(owned) != len(set(owned)) or UNOWNED_PATH in owned:
        raise ValueError(f"global_input {global_input!r} collides with a fixture path")

    def key(name: str, cmd: str, policy: KeyPolicy = "required") -> AttestKey:
        return AttestKey(name, cmd, policy=policy, selectors={backend: selector(name)})

    keys = (
        key(KEY_UNIT, "just test-unit"),
        key(KEY_DOCS, "just lint-md", "optional"),
        key(KEY_LEGACY, "just test-legacy"),
        key(KEY_ORPHAN, "just test-orphan"),
        # Reads commit history: no selector for any backend, so it can never carry (rule 5).
        AttestKey(KEY_GIT_METADATA, "pytest -m always_run"),
    )
    modified = "M"
    scenarios = (
        ImpactScenario(UNCHANGED, (), head_tree=BASE_TREE),
        ImpactScenario(LEAF, (ChangedPath(LEAF_DOC, modified),)),
        ImpactScenario(CHAIN, (ChangedPath(CHAIN_ROOT, modified),)),
        ImpactScenario(UNOWNED, (ChangedPath(LEAF_DOC, modified), ChangedPath(UNOWNED_PATH, "A"))),
        ImpactScenario(GLOBAL, (ChangedPath(global_input, modified),)),
        ImpactScenario(DELETION, (ChangedPath(DELETED_PATH, "D"),)),
        ImpactScenario(RENAME, (ChangedPath(RENAMED_TO, "A"), ChangedPath(RENAMED_FROM, "D"))),
        ImpactScenario(UNPROVEN_EDIT, (ChangedPath(UNPROVEN_SOURCE, modified),)),
    )
    return ImpactFixture(
        backend=backend,
        units=units,
        keys=keys,
        scenarios={scenario.name: scenario for scenario in scenarios},
        global_input=global_input,
        selector=selector,
    )


__all__ = [
    "BASE_REV",
    "BASE_TREE",
    "CHAIN",
    "CHAIN_ROOT",
    "DEFAULT_GLOBAL_INPUT",
    "DELETED_PATH",
    "DELETION",
    "GLOBAL",
    "HEAD_REV",
    "HEAD_TREE",
    "KEY_DOCS",
    "KEY_GIT_METADATA",
    "KEY_LEGACY",
    "KEY_ORPHAN",
    "KEY_UNIT",
    "LEAF",
    "LEAF_DOC",
    "RENAME",
    "RENAMED_FROM",
    "RENAMED_TO",
    "UNCHANGED",
    "UNIT_KINDS",
    "UNOWNED",
    "UNOWNED_PATH",
    "UNPROVEN_EDIT",
    "UNPROVEN_SOURCE",
    "FixtureTreeDiff",
    "FixtureUnit",
    "ImpactFixture",
    "ImpactScenario",
    "build_impact_fixture",
    "default_selector",
]
