"""Phase B3 of the backfill pipeline: cluster artifacts into work units and area epics.

A **unit** (:class:`~beadlog_sources.collate.Unit`) is a candidate bead, or the evidence of an
existing local bead. :func:`cluster` runs ordered, configurable claim rules over a B2
:class:`~beadlog_sources.collate.Collation` (it needs the in-memory B1 indexes); the first rule
that claims an artifact wins, so an artifact belongs to at most one unit
(``docs/design/backfill-history-pipeline-adr.md``, Phase B3). The claim rules, in default order
(:data:`~beadlog_sources.collate.CLAIM_RULES`):

1. ``plan-item``: every plan item is a unit (the structure is stated, not inferred). Its
   ``parent`` and ``depends_on`` (a plan-item key, or the item id of ``plan:<id>``) become
   unit links; an unknown target is dropped. A plan item with children is an ``epic``.
2. ``local-bead``: a change or review whose first bead-token ref resolved ``local`` (B2) is
   attached to that existing bead (unit ``bead:<id>``, kind ``attach``). Then each attached
   review sweeps in its free commits (by number) and each attached merge its free
   second-parent commits (commit order): an artifact's own ref beats inherited membership.
3. ``review``: every free review is a unit keyed by its own key, sweeping in its free commits
   and the free merge commits that integrate it (a merge that refs the review, or whose
   second-parent commits all belong to it), in number order.
4. ``merge``: every free first-parent merge (a merge no other merge brings in) is a unit with
   its free second-parent commits (nested merges included), in commit order. A merge whose
   second-parent commits are all claimed by one unit joins that unit instead; one with none,
   or split across units, stays free.
5. ``commit``: every free non-merge change whose ``cc_type`` is in ``include_types`` (default
   ``feat``; ``*`` matches any type, untyped commits included) is a unit of its own.

Release markers, tracker items and design records are never claimed (releases give dates and
windows; trackers anchor identity in B5; docs attach in B4).

Then ``area`` (when :attr:`ClusterOptions.epics` is on) groups the ``review``, ``merge`` and
``commit`` units into area epics (key ``area:<name>``, kind ``epic``). A unit's area is, in
order: a path-prefix override (an ``areas`` key containing ``/``, longest prefix, majority over
the unit's paths); else the majority ``cc_scope`` of its changes; else the majority top-level
directory (``path_depth`` segments) of its paths; else its release window (``release/<version>``:
the earliest release whose commit range holds one of its changes, else the first release at or
after it by time, else ``release/unreleased``). A plain ``areas`` key then renames the derived
area (so several scopes or directories can share one epic). Merge commits vote only when the
unit has no other change. Ties break by name. An area with fewer than ``min_epic_units`` units
gets no epic (its units keep ``area`` and no ``parent``). Plan and attach units are never
grouped: they carry their own structure.

Everything is ordered by keys, commit order and numbers, so the same collation and options
give the same units, byte for byte.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any

from .collate import (
    ARTIFACT,
    ATTACH,
    CLAIM_RULES,
    EPIC,
    ISSUE,
    RULE_AREA,
    RULE_COMMIT,
    RULE_LOCAL_BEAD,
    RULE_MERGE,
    RULE_PLAN_ITEM,
    RULE_REVIEW,
    Collation,
    CollationFormatError,
    Indexes,
    Unit,
)
from .evidence import Artifact, Category
from .ledger import BEAD_TOKEN, LOCAL

ANY_TYPE = "*"
UNRELEASED = "unreleased"
# The rules whose units are grouped into area epics.
GROUPED_RULES = (RULE_REVIEW, RULE_MERGE, RULE_COMMIT)
# The ``backfill.collate`` config members :meth:`ClusterOptions.from_mapping` reads.
OPTION_MEMBERS = ("rules", "include_types", "epics", "areas", "path_depth", "min_epic_units")


class ClusterOptionsError(ValueError):
    """A B3 option outside its domain."""


def _strings(value: Any, what: str) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, Iterable):
        raise ClusterOptionsError(f"{what} must be a list of strings")
    out = tuple(value)
    if not all(isinstance(item, str) and item for item in out):
        raise ClusterOptionsError(f"{what} must be a list of non-empty strings")
    return out


@dataclass(frozen=True)
class ClusterOptions:
    """B3 options (the ``backfill.collate`` config table; defaults per the ADR).

    - ``rules``: the claim rules to run, in order (a subset of
      :data:`~beadlog_sources.collate.CLAIM_RULES`; drop one to turn it off).
    - ``include_types``: the ``cc_type`` values the ``commit`` rule makes units of (``*``: any).
    - ``epics``: group units into area epics (the ``area`` rule).
    - ``areas``: per-hive override map. A key with a ``/`` is a path prefix whose changes go to
      the named area; any other key renames a derived area (a scope or directory name).
    - ``path_depth``: directory segments of a path-derived area (``1``: top-level directory).
    - ``min_epic_units``: the fewest units an area needs to get an epic.
    """

    rules: tuple[str, ...] = CLAIM_RULES
    include_types: tuple[str, ...] = ("feat",)
    epics: bool = True
    areas: Mapping[str, str] = field(default_factory=dict, hash=False)
    path_depth: int = 1
    min_epic_units: int = 2

    def __post_init__(self) -> None:
        rules = _strings(self.rules, "rules")
        unknown = [r for r in rules if r not in CLAIM_RULES]
        if unknown or len(set(rules)) != len(rules):
            raise ClusterOptionsError(
                f"rules must be distinct names from {CLAIM_RULES}, got {list(rules)}"
            )
        object.__setattr__(self, "rules", rules)
        object.__setattr__(self, "include_types", _strings(self.include_types, "include_types"))
        if not isinstance(self.epics, bool):
            raise ClusterOptionsError("epics must be a boolean")
        if not isinstance(self.areas, Mapping) or not all(
            isinstance(k, str) and k and isinstance(v, str) and v for k, v in self.areas.items()
        ):
            raise ClusterOptionsError("areas must map non-empty strings to non-empty strings")
        object.__setattr__(self, "areas", MappingProxyType(dict(sorted(self.areas.items()))))
        for name in ("path_depth", "min_epic_units"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ClusterOptionsError(f"{name} must be a positive integer")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> ClusterOptions:
        """Options from a ``backfill.collate`` table; members other B steps own are ignored."""
        return cls(**{name: raw[name] for name in OPTION_MEMBERS if name in raw})


@dataclass
class _Draft:
    key: str
    kind: str
    rule: str
    title: str
    bead: str | None = None
    artifacts: list[str] = field(default_factory=list)
    area: str | None = None
    parent: str | None = None
    children: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)

    def freeze(self) -> Unit:
        return Unit(
            key=self.key,
            kind=self.kind,
            rule=self.rule,
            title=self.title,
            bead=self.bead,
            artifacts=tuple(self.artifacts),
            area=self.area,
            parent=self.parent,
            children=tuple(sorted(self.children)),
            depends_on=tuple(sorted(self.depends_on)),
        )


def _is_merge(artifact: Artifact) -> bool:
    return len(artifact.attrs["parents"]) >= 2


class _Clusterer:
    def __init__(self, collation: Collation, indexes: Indexes, options: ClusterOptions) -> None:
        self.options = options
        self.ix = indexes
        self.arts = {key: found[0] for key, found in sorted(indexes.by_key.items())}
        self.units: dict[str, _Draft] = {}
        self.claimed: dict[str, str] = {}
        changes = self.of(Category.CHANGE)
        self.changes = sorted(
            changes, key=lambda a: (str(a.attrs["committed_at"]), str(a.attrs["sha"]).lower())
        )
        self.rank = {a.key: i for i, a in enumerate(self.changes)}
        self.reviews = sorted(self.of(Category.REVIEW), key=lambda a: (a.attrs["number"], a.key))
        inner = {k for kids in indexes.merge_children.values() for k in kids}
        self.mainline_merges = [a for a in self.changes if _is_merge(a) and a.key not in inner]
        self.local_bead: dict[str, str] = {}
        # The merges that integrate a review: they ref it, or it holds all their commits.
        self.merges_of_review: dict[str, set[str]] = {}
        for r in collation.refs:
            if r.outcome == LOCAL and r.ref.kind == BEAD_TOKEN:
                self.local_bead.setdefault(r.source, r.ref.raw.strip())
            elif r.outcome == ARTIFACT and r.source in self.rank and _is_merge(self.arts[r.source]):
                for target in r.targets:
                    if self._category(target) is Category.REVIEW:
                        self.merges_of_review.setdefault(target, set()).add(r.source)
        for merge in self.mainline_merges:
            kids = indexes.merge_children.get(merge.key, ())
            if not kids:
                continue
            common = set(indexes.pr_of_commit.get(kids[0], ()))
            for kid in kids[1:]:
                common &= set(indexes.pr_of_commit.get(kid, ()))
            for review in common:
                self.merges_of_review.setdefault(review, set()).add(merge.key)

    def of(self, category: Category) -> list[Artifact]:
        return [a for a in self.arts.values() if a.category is category]

    def _category(self, key: str) -> Category | None:
        art = self.arts.get(key)
        return art.category if art else None

    # -- claiming --------------------------------------------------------------------------

    def free(self, key: str) -> bool:
        return key not in self.claimed and key in self.arts

    def claim(self, key: str, unit: str) -> bool:
        if not self.free(key):
            return False
        self.claimed[key] = unit
        self.units[unit].artifacts.append(key)
        return True

    def new_unit(self, anchor: Artifact, rule: str, kind: str = ISSUE) -> _Draft:
        draft = _Draft(key=anchor.key, kind=kind, rule=rule, title=anchor.title)
        self.units[draft.key] = draft
        self.claim(anchor.key, draft.key)
        return draft

    def sweep(self, keys: Iterable[str], unit: str) -> None:
        for key in sorted(keys, key=lambda k: (self.rank.get(k, -1), k)):
            self.claim(key, unit)

    # -- rules -----------------------------------------------------------------------------

    def rule_plan_item(self) -> None:
        items = sorted(self.of(Category.PLAN_ITEM), key=lambda a: (a.attrs["order"], a.key))
        plan_keys = {a.key for a in items}

        def target(raw: Any) -> str | None:
            if not isinstance(raw, str) or not raw:
                return None
            for key in (raw, f"plan:{raw}"):
                unit = self.units.get(key)
                if key in plan_keys and unit is not None and unit.rule == RULE_PLAN_ITEM:
                    return key
            return None

        made = [self.new_unit(a, RULE_PLAN_ITEM) for a in items if self.free(a.key)]
        for draft in made:
            attrs = self.arts[draft.key].attrs
            parent = target(attrs["parent"])
            if parent is not None and parent != draft.key:
                draft.parent = parent
                self.units[parent].children.append(draft.key)
            draft.depends_on = sorted(
                {t for t in (target(d) for d in attrs["depends_on"]) if t and t != draft.key}
            )
        for draft in made:
            if draft.children:
                draft.kind = EPIC

    def rule_local_bead(self) -> None:
        attached: list[str] = []
        for key, bead in sorted(self.local_bead.items()):
            if self._category(key) not in (Category.CHANGE, Category.REVIEW) or not self.free(key):
                continue
            unit = f"bead:{bead}"
            if unit not in self.units:
                self.units[unit] = _Draft(key=unit, kind=ATTACH, rule=RULE_LOCAL_BEAD, title=bead)
                self.units[unit].bead = bead
            self.claim(key, unit)
            attached.append(key)
        for review in self.reviews:
            if review.key in attached:
                self.sweep(self.ix.pr_commits.get(review.key, ()), self.claimed[review.key])
        for change in self.changes:
            if change.key in attached and _is_merge(change):
                self.sweep(self.ix.merge_children.get(change.key, ()), self.claimed[change.key])

    def rule_review(self) -> None:
        for review in self.reviews:
            if not self.free(review.key):
                continue
            draft = self.new_unit(review, RULE_REVIEW)
            self.sweep(self.ix.pr_commits.get(review.key, ()), draft.key)
            self.sweep(self.merges_of_review.get(review.key, ()), draft.key)

    def rule_merge(self) -> None:
        for merge in self.mainline_merges:
            if not self.free(merge.key):
                continue
            kids = self.ix.merge_children.get(merge.key, ())
            if any(self.free(k) for k in kids):
                draft = self.new_unit(merge, RULE_MERGE)
                self.sweep(kids, draft.key)
                continue
            owners = {self.claimed[k] for k in kids if k in self.claimed}
            if kids and len(owners) == 1:
                self.claim(merge.key, owners.pop())

    def rule_commit(self) -> None:
        types = set(self.options.include_types)
        for change in self.changes:
            if not self.free(change.key) or _is_merge(change):
                continue
            if ANY_TYPE in types or change.attrs["cc_type"] in types:
                self.new_unit(change, RULE_COMMIT)

    # -- areas -----------------------------------------------------------------------------

    def _vote(self, names: Iterable[str]) -> str | None:
        counts = Counter(names)
        if not counts:
            return None
        return min(counts, key=lambda n: (-counts[n], n))

    def _path_override(self, path: str) -> str | None:
        best: tuple[int, str] | None = None
        for prefix, area in self.options.areas.items():
            if "/" not in prefix:
                continue
            stem = prefix.rstrip("/")
            if (path == stem or path.startswith(stem + "/")) and (
                best is None or len(stem) > best[0]
            ):
                best = (len(stem), area)
        return best[1] if best else None

    def _top_dir(self, path: str) -> str | None:
        dirs = path.split("/")[:-1]
        return "/".join(dirs[: self.options.path_depth]) if dirs else None

    def _releases(self) -> list[Artifact]:
        return sorted(
            self.of(Category.RELEASE_MARKER),
            key=lambda a: (a.attrs["released_at"] is None, str(a.attrs["released_at"]), a.key),
        )

    def _release_window(self, draft: _Draft, releases: list[Artifact]) -> str | None:
        if not releases:
            return None
        members = set(draft.artifacts)
        for release in releases:
            if members.intersection(self.ix.tag_range.get(release.key, ())):
                return release.key
        times = []
        for key in draft.artifacts:
            attrs = self.arts[key].attrs
            when = attrs.get("committed_at") or attrs.get("merged_at")
            if isinstance(when, str):
                times.append(when)
        if not times:
            return None
        latest = max(times)
        for release in releases:
            when = release.attrs["released_at"]
            if isinstance(when, str) and when >= latest:
                return release.key
        return UNRELEASED

    def area_of(self, draft: _Draft, releases: list[Artifact]) -> tuple[str | None, str | None]:
        """The unit's area name and the release marker key it came from (if any)."""
        changes = [self.arts[k] for k in draft.artifacts if self._category(k) is Category.CHANGE]
        voters = [c for c in changes if not _is_merge(c)] or changes
        paths = [str(p) for c in voters for p in c.attrs["paths"]]
        override = self._vote(o for o in map(self._path_override, paths) if o)
        if override is not None:
            return override, None
        name = self._vote(str(c.attrs["cc_scope"]) for c in voters if c.attrs["cc_scope"])
        if name is None:
            name = self._vote(d for d in map(self._top_dir, paths) if d)
        release = None
        if name is None:
            release = self._release_window(draft, releases)
            if release is None:
                return None, None
            if release == UNRELEASED:
                name, release = f"release/{UNRELEASED}", None
            else:
                name = f"release/{self.arts[release].attrs['version']}"
        return self.options.areas.get(name, name), release

    def rule_area(self) -> None:
        releases = self._releases()
        members: dict[str, list[_Draft]] = {}
        cited: dict[str, set[str]] = {}
        for key in sorted(self.units):
            draft = self.units[key]
            if draft.rule not in GROUPED_RULES:
                continue
            area, release = self.area_of(draft, releases)
            if area is None:
                continue
            draft.area = area
            members.setdefault(area, []).append(draft)
            if release is not None:
                cited.setdefault(area, set()).add(release)
        for area, drafts in sorted(members.items()):
            if len(drafts) < self.options.min_epic_units:
                continue
            epic = _Draft(key=f"area:{area}", kind=EPIC, rule=RULE_AREA, title=area, area=area)
            epic.artifacts = sorted(cited.get(area, ()))
            epic.children = [d.key for d in drafts]
            for draft in drafts:
                draft.parent = epic.key
            self.units[epic.key] = epic

    def run(self) -> tuple[Unit, ...]:
        rules = {
            RULE_PLAN_ITEM: self.rule_plan_item,
            RULE_LOCAL_BEAD: self.rule_local_bead,
            RULE_REVIEW: self.rule_review,
            RULE_MERGE: self.rule_merge,
            RULE_COMMIT: self.rule_commit,
        }
        for rule in self.options.rules:
            rules[rule]()
        if self.options.epics:
            self.rule_area()
        return tuple(self.units[key].freeze() for key in sorted(self.units))


def cluster(collation: Collation, options: ClusterOptions | None = None) -> Collation:
    """B3: ``collation`` with its ``units`` filled (see the module doc).

    Needs the in-memory B1 indexes (a collation from :func:`~beadlog_sources.collate.collate`,
    not one loaded from disk).
    """
    if collation.indexes is None:
        raise CollationFormatError("cluster needs the B1 indexes (a loaded collation has none)")
    units = _Clusterer(collation, collation.indexes, options or ClusterOptions()).run()
    return replace(collation, units=units)


__all__ = [
    "ANY_TYPE",
    "GROUPED_RULES",
    "OPTION_MEMBERS",
    "UNRELEASED",
    "ClusterOptions",
    "ClusterOptionsError",
    "cluster",
]
