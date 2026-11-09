"""The label projection: a registry-derived catalog of owned tracker labels and the desired state.

Pure rules (standard library only) for projecting bead state onto an external issue's labels
under ONE owned prefix (``bh:`` by default), per the operator decisions recorded on ``bh-8jfwu``
for epic ``bh-hrx2r``. Nothing here reads config, a tracker, a store or a clock: the composition
root hands in the dimension registry (:class:`RegistryDimension`) and the projection settings
(:class:`LabelSettings`), and the application layer hands in the facts and the live labels.

**The catalog** (:class:`LabelCatalog`) is every label definition the projection may produce, one
:class:`LabelSpec` per ``(dimension, value)``:

- names are ``<prefix><alias-or-dimension>/<value>`` (``bh:intake/accepted``, or
  ``bh:triage/accepted`` with the alias ``intake -> triage``), unless a per-value override names
  the label (still under the prefix);
- the description comes from the registry's dimension description (a built-in one for the
  built-in dimensions), followed by the value; an override may replace it;
- the color is per dimension (``colors``), or a built-in default.

Closed sets are complete up front: the built-in intake and delivery vocabulary
(:data:`~.vocabulary.INTAKE_DIMENSIONS`) unioned with any configured ``dimensions.*.values``, the
fixed ``type`` and ``priority`` sets, the derived ``progress`` set, and any other configured
dimension with ``values``. Open sets (``component``, ``tag``, and any configured dimension without
``values``) hold the values observed on projected beads (:meth:`LabelCatalog.observe`).

**The desired state** (:func:`desired_labels`) is a pure function of the report's own facts, the
tracked work's facts and the progress facts. Type, priority and component come from the tracked
work, falling back to the report; progress is derived (:func:`derive_progress`). Every closed
dimension is an exclusive group: at most one of its values is desired.

**The diff** (:func:`diff_labels`) compares the desired set with the issue's live OWNED labels
only: labels outside the prefix are never added, removed or reported. An owned live label the
catalog does not produce is *drift*, reported and never removed here (pruning is the caller's
explicit choice). :meth:`LabelCatalog.unmapped` classifies owned label definitions the catalog no
longer produces (a value removed from the registry, a dimension no longer projected) for prune.

**Leaks** (:meth:`LabelCatalog.findings`): every name and description is run through the leak
check (:func:`~.leaks.check`) with the hive's :class:`~.leaks.LeakPolicy`, so no bead id, branch,
seat, path or private ``<dimension>:<value>`` label can become a public label.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType

from .leaks import LeakFinding, LeakPolicy, Scanner, check, unresolved
from .link import work_progress_of
from .models import WorkProgress
from .vocabulary import DELIVERY_DIM, DISPOSITION_STATE, INTAKE_DIM, INTAKE_DIMENSIONS

#: The owned prefix every projected label carries by default.
DEFAULT_PREFIX = "bh:"
PROGRESS_DIM = "progress"
TYPE_DIM = "type"
PRIORITY_DIM = "priority"
COMPONENT_DIM = "component"
TAG_DIM = "tag"

#: The fixed closed sets (not extensible by config).
TYPE_VALUES = frozenset({"bug", "feature", "chore", "task"})
PRIORITY_VALUES = frozenset({"p0", "p1", "p2", "p3", "p4"})
#: The derived progress set: started, blocked, deprioritised (operator decision 5, bh-8jfwu).
PROGRESS_STARTED = "started"
PROGRESS_BLOCKED = "blocked"
PROGRESS_DEPRIORITISED = "deprioritised"
PROGRESS_VALUES = frozenset({PROGRESS_STARTED, PROGRESS_BLOCKED, PROGRESS_DEPRIORITISED})
FIXED_DIMENSIONS: Mapping[str, frozenset[str]] = MappingProxyType(
    {TYPE_DIM: TYPE_VALUES, PRIORITY_DIM: PRIORITY_VALUES, PROGRESS_DIM: PROGRESS_VALUES}
)
#: The built-in open sets: filled from the values observed on projected beads.
OPEN_DIMENSIONS = frozenset({COMPONENT_DIM, TAG_DIM})
#: The dimensions projected unless the settings say otherwise.
DEFAULT_PROJECTED = (INTAKE_DIM, DELIVERY_DIM, PROGRESS_DIM, TYPE_DIM, PRIORITY_DIM, COMPONENT_DIM)

#: GitHub's limits on a label's name and description.
MAX_NAME_CHARS = 50
MAX_DESCRIPTION_CHARS = 100
#: Used for a dimension with no configured and no built-in color.
NEUTRAL_COLOR = "ededed"
#: The built-in color of each built-in dimension (6-hex, no ``#``).
DEFAULT_COLORS: Mapping[str, str] = MappingProxyType(
    {
        INTAKE_DIM: "fbca04",
        DELIVERY_DIM: "0e8a16",
        PROGRESS_DIM: "1d76db",
        TYPE_DIM: "5319e7",
        PRIORITY_DIM: "d93f0b",
        COMPONENT_DIM: "c5def5",
        TAG_DIM: "bfdadc",
    }
)
#: The built-in description of each built-in dimension (the registry's description wins).
DEFAULT_DESCRIPTIONS: Mapping[str, str] = MappingProxyType(
    {
        INTAKE_DIM: "Triage state",
        DELIVERY_DIM: "Delivery of the fix",
        PROGRESS_DIM: "Progress of the work",
        TYPE_DIM: "Type",
        PRIORITY_DIM: "Priority",
        COMPONENT_DIM: "Component",
        TAG_DIM: "Tag",
    }
)

_HEX = re.compile(r"^[0-9a-fA-F]{6}$")
_GROUP = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")


class Openness(StrEnum):
    """Whether a dimension's values are defined up front or as they are observed."""

    CLOSED = "closed"
    OPEN = "open"


class UnmappedReason(StrEnum):
    """Why an owned label definition is no longer produced by the catalog."""

    VALUE_REMOVED = "value_removed"  # a closed dimension is projected, the value left its set
    NOT_OBSERVED = "not_observed"  # an open dimension is projected, no projected bead has it
    NOT_PROJECTED = "not_projected"  # the dimension is not (or no longer) projected
    UNPARSEABLE = "unparseable"  # not ``<prefix><group>/<value>`` at all


# -- inputs ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class RegistryDimension:
    """One dimension as the registry declares it (``dimensions.<name>``): ``values=None`` is an
    open set, an empty set a locked one (closed, nothing to project)."""

    name: str
    description: str = ""
    values: frozenset[str] | None = None


@dataclass(frozen=True)
class LabelOverride:
    """A per-value override (``overrides["<dimension>/<value>"]``). An empty field keeps the
    derived one; an overriding ``name`` must still be under the prefix."""

    name: str = ""
    description: str = ""
    color: str = ""


def _frozen(mapping: Mapping | None) -> Mapping:
    return MappingProxyType(dict(mapping or {}))


@dataclass(frozen=True)
class LabelSettings:
    """The ``intake.external.labels`` projection semantics: the owned prefix, which dimensions to
    project, the per-dimension display alias and color, and per-value overrides. Raises
    :class:`ValueError` on a shape the config layer should already have refused."""

    prefix: str = DEFAULT_PREFIX
    project: tuple[str, ...] = DEFAULT_PROJECTED
    aliases: Mapping[str, str] = field(default_factory=dict)
    colors: Mapping[str, str] = field(default_factory=dict)
    overrides: Mapping[str, LabelOverride] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.prefix:
            raise ValueError("a label projection needs an owned prefix")
        object.__setattr__(self, "project", tuple(dict.fromkeys(self.project)))
        object.__setattr__(self, "aliases", _frozen(self.aliases))
        object.__setattr__(self, "colors", _frozen(self.colors))
        object.__setattr__(self, "overrides", _frozen(self.overrides))
        for dim, alias in self.aliases.items():
            if not _GROUP.match(alias):
                raise ValueError(f"aliases.{dim}: {alias!r} is not a label group name")
        groups = [self.group(dim) for dim in self.project]
        clashes = sorted({g for g in groups if groups.count(g) > 1})
        if clashes:
            raise ValueError(f"two projected dimensions share the label group {clashes[0]!r}")
        for dim, color in self.colors.items():
            if not _HEX.match(color):
                raise ValueError(f"colors.{dim}: {color!r} is not a 6-hex color")
        for key, o in self.overrides.items():
            if key.count("/") != 1 or not all(key.split("/")):
                raise ValueError(f"overrides: {key!r} is not <dimension>/<value>")
            if o.name and not o.name.startswith(self.prefix):
                raise ValueError(f"overrides.{key}: {o.name!r} is not under {self.prefix!r}")
            if o.color and not _HEX.match(o.color):
                raise ValueError(f"overrides.{key}: {o.color!r} is not a 6-hex color")

    def group(self, dimension: str) -> str:
        """The label group a dimension is shown as: its alias, or its own name."""
        return self.aliases.get(dimension) or dimension

    def owns(self, label: str) -> bool:
        return label.startswith(self.prefix)


@dataclass(frozen=True)
class ReportFacts:
    """The report bead's own projected values (the fallback for type, priority and component)."""

    intake: str | None = None
    delivery: str | None = None
    type: str | None = None
    priority: int | str | None = None
    components: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()

    @property
    def rejected(self) -> bool:
        """Fielded by ``reject`` (a ``duplicate-of`` ends in one): its delivery and progress are
        not its own, so none is projected (bh-5kpwc)."""
        return self.intake == DISPOSITION_STATE["reject"]

    @classmethod
    def from_bead(
        cls,
        labels: Iterable[str] | None,
        issue_type: str | None = None,
        priority: int | str | None = None,
    ) -> ReportFacts:
        """The facts on a report bead's ``<dimension>:<value>`` label cache."""
        cache = _dimension_values(labels)
        return cls(
            intake=_single(cache.get(INTAKE_DIM)),
            delivery=_single(cache.get(DELIVERY_DIM)),
            type=issue_type,
            priority=priority,
            components=tuple(cache.get(COMPONENT_DIM, ())),
            tags=tuple(cache.get(TAG_DIM, ())),
        )


@dataclass(frozen=True)
class WorkFacts:
    """The tracked work bead: its status (``blocked`` counts as blocked progress), its labels
    (the ``review:pending`` label means in review) and its own type, priority and components."""

    status: str
    labels: tuple[str, ...] = ()
    type: str | None = None
    priority: int | str | None = None
    components: tuple[str, ...] = ()

    @classmethod
    def from_bead(
        cls,
        status: str,
        labels: Iterable[str] | None,
        issue_type: str | None = None,
        priority: int | str | None = None,
    ) -> WorkFacts:
        labels = tuple(labels or ())
        cache = _dimension_values(labels)
        return cls(status, labels, issue_type, priority, tuple(cache.get(COMPONENT_DIM, ())))

    @property
    def progress(self) -> WorkProgress:
        return work_progress_of(self.status, self.labels)


@dataclass(frozen=True)
class ProgressFacts:
    """The operator notices that exist for the report (``bh work delivery blocked|deprioritised``
    records in its outbox)."""

    blocked_notice: bool = False
    deprioritised_notice: bool = False


def _dimension_values(labels: Iterable[str] | None) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for label in labels or ():
        dim, sep, value = label.partition(":")
        if sep and dim and value:
            out.setdefault(dim, []).append(value)
    return out


def _single(values: list[str] | None) -> str | None:
    return values[0] if values else None


# -- the catalog -----------------------------------------------------------------------------


@dataclass(frozen=True)
class LabelSpec:
    """One label definition the projection produces."""

    dimension: str
    value: str
    name: str
    description: str
    color: str
    openness: Openness = Openness.CLOSED

    @property
    def exclusive(self) -> bool:
        """A closed dimension is an exclusive group (one value at a time); an open set (several
        components or tags) is not."""
        return self.openness is Openness.CLOSED


@dataclass(frozen=True)
class Unmapped:
    """An owned label the catalog does not produce, and why (reported for ``--prune``)."""

    name: str
    reason: UnmappedReason
    dimension: str = ""
    value: str = ""


@dataclass(frozen=True)
class LabelProblem:
    """A catalog label that cannot be defined as it is (a name longer than the tracker allows)."""

    name: str
    detail: str


def normalize_priority(priority: int | str | None) -> str | None:
    """``2``, ``"2"``, ``"P2"`` or ``"p2"`` as ``p2``; ``None`` for anything else."""
    if priority is None or isinstance(priority, bool):
        return None
    text = str(priority).strip().lower().removeprefix("p")
    value = f"p{text}"
    return value if value in PRIORITY_VALUES else None


class LabelCatalog:
    """The label definitions derived from the registry and the settings. Never mutated:
    :meth:`observe` returns a new catalog with open-set values added."""

    def __init__(
        self,
        settings: LabelSettings,
        closed: Mapping[str, frozenset[str]],
        open_values: Mapping[str, frozenset[str]],
        descriptions: Mapping[str, str],
    ) -> None:
        self.settings = settings
        self._closed = MappingProxyType(dict(closed))
        self._open = MappingProxyType(dict(open_values))
        self._descriptions = MappingProxyType(dict(descriptions))
        specs: dict[str, LabelSpec] = {}
        for dim in settings.project:
            if dim in self._closed:
                values, openness = self._closed[dim], Openness.CLOSED
            else:
                values, openness = self._open.get(dim, frozenset()), Openness.OPEN
            for value in sorted(values):
                spec = self._spec(dim, value, openness)
                clash = specs.get(spec.name)
                if clash is not None:
                    raise ValueError(
                        f"{clash.dimension}/{clash.value} and {dim}/{value} are both {spec.name!r}"
                    )
                specs[spec.name] = spec
        self._specs = MappingProxyType(specs)
        self._by_value = MappingProxyType({(s.dimension, s.value): s for s in specs.values()})

    # -- construction --------------------------------------------------------------------

    @classmethod
    def build(
        cls,
        registry: Mapping[str, RegistryDimension] | Iterable[RegistryDimension],
        settings: LabelSettings | None = None,
        observed: Mapping[str, Iterable[str]] | None = None,
    ) -> LabelCatalog:
        """The catalog for ``registry`` (the configured ``dimensions.*``, passed in) under
        ``settings``, with the open-set values in ``observed``."""
        settings = settings if settings is not None else LabelSettings()
        dims = dict(registry) if isinstance(registry, Mapping) else {d.name: d for d in registry}
        closed: dict[str, frozenset[str]] = {}
        descriptions: dict[str, str] = {}
        for dim in settings.project:
            spec = dims.get(dim)
            descriptions[dim] = (spec.description if spec else "") or DEFAULT_DESCRIPTIONS.get(
                dim, dim
            )
            if dim in FIXED_DIMENSIONS:
                closed[dim] = FIXED_DIMENSIONS[dim]
            elif dim in INTAKE_DIMENSIONS:
                extra = spec.values if spec and spec.values else frozenset()
                closed[dim] = frozenset(INTAKE_DIMENSIONS[dim]) | frozenset(extra)
            elif spec is not None and spec.values is not None:  # configured ``values``: closed
                closed[dim] = frozenset(spec.values)
        catalog = cls(settings, closed, {}, descriptions)
        return catalog.observe(observed or {})

    def observe(self, observed: Mapping[str, Iterable[str]]) -> LabelCatalog:
        """A catalog with the open-set values in ``observed`` added. Values of a closed or an
        unprojected dimension are ignored (a closed set is complete up front)."""
        merged = {dim: set(values) for dim, values in self._open.items()}
        for dim, values in observed.items():
            if dim in self.settings.project and dim not in self._closed:
                merged.setdefault(dim, set()).update(v for v in values if v)
        return LabelCatalog(
            self.settings,
            self._closed,
            {dim: frozenset(values) for dim, values in merged.items()},
            self._descriptions,
        )

    def _spec(self, dim: str, value: str, openness: Openness) -> LabelSpec:
        o = self.settings.overrides.get(f"{dim}/{value}", LabelOverride())
        name = o.name or f"{self.settings.prefix}{self.settings.group(dim)}/{value}"
        description = o.description or f"{self._descriptions.get(dim, dim)}: {value}"
        color = o.color or self.settings.colors.get(dim) or DEFAULT_COLORS.get(dim) or NEUTRAL_COLOR
        return LabelSpec(
            dim, value, name, description[:MAX_DESCRIPTION_CHARS], color.lower(), openness
        )

    # -- reads ---------------------------------------------------------------------------

    @property
    def prefix(self) -> str:
        return self.settings.prefix

    @property
    def specs(self) -> tuple[LabelSpec, ...]:
        """Every definition, ordered by projected dimension then value."""
        order = {dim: n for n, dim in enumerate(self.settings.project)}
        return tuple(sorted(self._specs.values(), key=lambda s: (order[s.dimension], s.value)))

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self._specs)

    def openness(self, dimension: str) -> Openness | None:
        """Whether a projected dimension is closed or open; ``None`` when it is not projected."""
        if dimension not in self.settings.project:
            return None
        return Openness.CLOSED if dimension in self._closed else Openness.OPEN

    def values(self, dimension: str) -> frozenset[str]:
        """The values of a projected dimension: the whole closed set, or the observed values."""
        if dimension in self._closed:
            return self._closed[dimension] if dimension in self.settings.project else frozenset()
        return self._open.get(dimension, frozenset())

    def spec(self, dimension: str, value: str) -> LabelSpec | None:
        return self._by_value.get((dimension, value))

    def by_name(self, name: str) -> LabelSpec | None:
        return self._specs.get(name)

    def owns(self, label: str) -> bool:
        return self.settings.owns(label)

    def name_for(self, dimension: str, value: str) -> str | None:
        """The label of ``dimension=value``: a defined one, or (for an open set) the name the
        value would be defined as once observed. ``None`` when nothing would project it."""
        spec = self.spec(dimension, value)
        if spec is not None:
            return spec.name
        if self.openness(dimension) is Openness.OPEN and value:
            return self._spec(dimension, value, Openness.OPEN).name
        return None

    def parse(self, name: str) -> tuple[str, str] | None:
        """``(dimension, value)`` of an owned label name: a defined one, else by the naming
        rule (the alias reversed). ``None`` when it is not ``<prefix><group>/<value>``."""
        spec = self.by_name(name)
        if spec is not None:
            return spec.dimension, spec.value
        if not self.owns(name):
            return None
        group, sep, value = name[len(self.prefix) :].partition("/")
        if not sep or not group or not value:
            return None
        aliased = {alias: dim for dim, alias in self.settings.aliases.items()}
        return aliased.get(group, group), value

    def unmapped(self, labels: Iterable[str]) -> tuple[Unmapped, ...]:
        """The owned labels in ``labels`` (the live definitions) the catalog does not define,
        each with why: the candidates ``--prune`` may delete."""
        out: list[Unmapped] = []
        for name in sorted(set(labels)):
            if not self.owns(name) or name in self._specs:
                continue
            parsed = self.parse(name)
            if parsed is None:
                out.append(Unmapped(name, UnmappedReason.UNPARSEABLE))
                continue
            dim, value = parsed
            reason = {
                Openness.CLOSED: UnmappedReason.VALUE_REMOVED,
                Openness.OPEN: UnmappedReason.NOT_OBSERVED,
                None: UnmappedReason.NOT_PROJECTED,
            }[self.openness(dim)]
            out.append(Unmapped(name, reason, dim, value))
        return tuple(out)

    def produces(self, name: str) -> bool:
        """Whether ``name`` is a label of this projection: a definition, or any value of a
        projected open dimension (observed or not), so a component the work no longer carries
        is removed like any other value, not left behind as drift."""
        if name in self._specs:
            return True
        parsed = self.parse(name)
        return parsed is not None and self.openness(parsed[0]) is Openness.OPEN

    def problems(self) -> tuple[LabelProblem, ...]:
        """Definitions the tracker would refuse as they are (a name over its length limit)."""
        return tuple(
            LabelProblem(s.name, f"longer than {MAX_NAME_CHARS} characters")
            for s in self.specs
            if len(s.name) > MAX_NAME_CHARS
        )

    def findings(
        self, policy: LeakPolicy, *, sanitizer: Scanner | None
    ) -> Mapping[str, tuple[LeakFinding, ...]]:
        """The unresolved leak findings of every definition's name and description, by name;
        a clean catalog maps to nothing. ``sanitizer=None`` holds every label (fail closed)."""
        return find_leaks(self.specs, policy, sanitizer=sanitizer)


def find_leaks(
    specs: Iterable[LabelSpec], policy: LeakPolicy, *, sanitizer: Scanner | None
) -> Mapping[str, tuple[LeakFinding, ...]]:
    """The unresolved leak findings of each spec's name and description (bead ids, branches,
    seats, paths, private ``<dimension>:<value>`` labels, secrets, a name outside the owned
    prefixes), keyed by label name."""
    out: dict[str, tuple[LeakFinding, ...]] = {}
    for spec in specs:
        found = unresolved(
            check(
                f"{spec.name}\n{spec.description}", policy, sanitizer=sanitizer, labels=[spec.name]
            )
        )
        if found:
            out[spec.name] = found
    return MappingProxyType(out)


# -- the desired state -----------------------------------------------------------------------


def derive_progress(
    work: WorkFacts | None, progress: ProgressFacts, delivery: str | None = None
) -> str | None:
    """The one ``progress`` value, derived on each sync (operator decision 5, bh-8jfwu):

    - ``deprioritised`` when an operator deprioritised notice exists;
    - else ``blocked`` when the work is blocked in bd or an operator blocked notice exists;
    - else ``started`` when the work is in progress or in review;
    - else none.

    Nothing once the delivery is ``merged`` or ``released``: the delivery label carries the news
    and a stale notice must not outlive the work."""
    if delivery in {"merged", "released"}:
        return None
    if progress.deprioritised_notice:
        return PROGRESS_DEPRIORITISED
    if progress.blocked_notice or (work is not None and work.status == "blocked"):
        return PROGRESS_BLOCKED
    if work is not None and work.progress in {WorkProgress.IN_PROGRESS, WorkProgress.IN_REVIEW}:
        return PROGRESS_STARTED
    return None


@dataclass(frozen=True)
class DesiredLabels:
    """The labels a report's issue should carry under the prefix, and the values that would be
    projected but have no label (a closed value outside its set, e.g. an ``epic`` type)."""

    labels: frozenset[str]
    by_dimension: Mapping[str, tuple[str, ...]]
    skipped: tuple[tuple[str, str], ...] = ()

    def undefined(self, catalog: LabelCatalog) -> frozenset[str]:
        """The desired labels the catalog has no definition for (an open value not yet
        observed): ``define`` must create them before a sync may add them."""
        return frozenset(name for name in self.labels if catalog.by_name(name) is None)


def desired_labels(
    catalog: LabelCatalog,
    report: ReportFacts,
    work: WorkFacts | None = None,
    progress: ProgressFacts | None = None,
) -> DesiredLabels:
    """The owned labels the report's issue should carry: one per closed (exclusive) dimension,
    every value of an open one. Type, priority and component come from the tracked work, falling
    back to the report."""

    progress = progress if progress is not None else ProgressFacts()
    w = work or WorkFacts("")
    # per dimension, the alternatives in order: the first that projects anything wins
    # a rejected report (a duplicate-of included) shares no delivery or progress with the work it
    # tracks: the canonical report carries both (bh-5kpwc)
    own = not report.rejected
    candidates: dict[str, tuple[tuple[str, ...], ...]] = {
        INTAKE_DIM: (_opt(report.intake),),
        DELIVERY_DIM: (_opt(report.delivery if own else None),),
        PROGRESS_DIM: (_opt(derive_progress(work, progress, report.delivery) if own else None),),
        TYPE_DIM: (_opt(w.type), _opt(report.type)),
        PRIORITY_DIM: (
            _opt(normalize_priority(w.priority)),
            _opt(normalize_priority(report.priority)),
        ),
        COMPONENT_DIM: (tuple(w.components), tuple(report.components)),
        TAG_DIM: (tuple(report.tags),),
    }
    labels: set[str] = set()
    by_dimension: dict[str, tuple[str, ...]] = {}
    skipped: list[tuple[str, str]] = []
    for dim in catalog.settings.project:
        names: list[str] = []
        unnamed: list[tuple[str, str]] = []
        for alternative in candidates.get(dim, ()):
            values = tuple(dict.fromkeys(v for v in alternative if v))
            names = [n for n in (catalog.name_for(dim, v) for v in values) if n is not None]
            unnamed += [(dim, v) for v in values if catalog.name_for(dim, v) is None]
            if names:
                break
        if catalog.openness(dim) is Openness.CLOSED:
            names = names[:1]  # an exclusive group: one value per dimension
        skipped += [item for item in unnamed if not names]
        if names:
            by_dimension[dim] = tuple(names)
            labels.update(names)
    return DesiredLabels(frozenset(labels), MappingProxyType(by_dimension), tuple(skipped))


def _opt(value: str | None) -> tuple[str, ...]:
    return (value,) if value else ()


# -- the diff --------------------------------------------------------------------------------


@dataclass(frozen=True)
class LabelsDiff:
    """What converges one issue's owned labels on the desired state. ``drift`` is every owned
    live label the projection does not produce (:meth:`LabelCatalog.produces`): reported, never
    in ``remove``."""

    add: frozenset[str]
    remove: frozenset[str]
    drift: frozenset[str] = frozenset()

    @property
    def changed(self) -> bool:
        return bool(self.add or self.remove)

    def swaps(self, catalog: LabelCatalog) -> Mapping[str, tuple[str, str]]:
        """The exclusive-group swaps in this diff: ``{dimension: (removed, added)}``."""
        removed = {parsed[0]: n for n in self.remove if (parsed := catalog.parse(n))}
        out: dict[str, tuple[str, str]] = {}
        for name in self.add:
            parsed = catalog.parse(name)
            if parsed and parsed[0] in removed and catalog.openness(parsed[0]) is Openness.CLOSED:
                out[parsed[0]] = (removed[parsed[0]], name)
        return MappingProxyType(out)


def diff_labels(
    catalog: LabelCatalog, desired: DesiredLabels | Iterable[str], live: Iterable[str]
) -> LabelsDiff:
    """Desired versus the live OWNED labels: add what is missing, remove the catalog's labels
    that are not desired (so an exclusive group swaps), and report owned labels the catalog
    does not produce as drift. A label outside the prefix is never touched."""
    want = desired.labels if isinstance(desired, DesiredLabels) else frozenset(desired)
    stray = sorted(name for name in want if not catalog.owns(name))
    if stray:
        raise ValueError(f"desired labels outside the owned prefix: {', '.join(stray)}")
    owned = frozenset(label for label in live if catalog.owns(label))
    produced = frozenset(label for label in owned if catalog.produces(label))
    return LabelsDiff(
        add=frozenset(want - owned),
        remove=frozenset(produced - want),
        drift=frozenset(owned - produced),
    )


__all__ = [
    "COMPONENT_DIM",
    "DEFAULT_COLORS",
    "DEFAULT_DESCRIPTIONS",
    "DEFAULT_PREFIX",
    "DEFAULT_PROJECTED",
    "FIXED_DIMENSIONS",
    "MAX_DESCRIPTION_CHARS",
    "MAX_NAME_CHARS",
    "NEUTRAL_COLOR",
    "OPEN_DIMENSIONS",
    "PRIORITY_DIM",
    "PRIORITY_VALUES",
    "PROGRESS_BLOCKED",
    "PROGRESS_DEPRIORITISED",
    "PROGRESS_DIM",
    "PROGRESS_STARTED",
    "PROGRESS_VALUES",
    "TAG_DIM",
    "TYPE_DIM",
    "TYPE_VALUES",
    "DesiredLabels",
    "LabelCatalog",
    "LabelOverride",
    "LabelProblem",
    "LabelSettings",
    "LabelSpec",
    "LabelsDiff",
    "Openness",
    "ProgressFacts",
    "RegistryDimension",
    "ReportFacts",
    "Unmapped",
    "UnmappedReason",
    "WorkFacts",
    "derive_progress",
    "desired_labels",
    "diff_labels",
    "find_leaks",
    "normalize_priority",
]
