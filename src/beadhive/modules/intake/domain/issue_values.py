"""The issue type and field-value projection's pure rules (``bh-poqy0``), beside the labels.

The label projection also projects the bead's type onto the issue's **type** and its priority
onto a single-select issue **field** (GitHub's ``Priority``), through configurable maps
(:class:`ValueSettings`, from ``intake.external.labels.issue_type`` and ``priority_field``). The
values come from the tracked work, falling back to the report, exactly as the ``type`` and
``priority`` labels do: the first candidate the map names wins.

**Never guessed.** A bead type or priority the map does not name is skipped with a note, and so
is a value the issue's type or fields were not read for (the tracker plugin's switch is off, or
the tracker reports none). A value is only ever SET: nothing here clears an issue's type or a
field. :func:`value_changes` diffs the desired values against the issue's live ones (an issue
type compares case-insensitively, as GitHub names it; an option exactly) and is what a sync
reports as drift and drafts. Pure: no store, no tracker, no clock.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from .labels import ReportFacts, WorkFacts, normalize_priority
from .outbox import TYPE_VALUE

#: The default maps (GitHub's organization defaults: ``Bug``, ``Feature`` and ``Task`` types, and
#: a ``Priority`` field offering ``Urgent``, ``High``, ``Medium`` and ``Low``).
DEFAULT_ISSUE_TYPES: Mapping[str, str] = MappingProxyType(
    {"bug": "Bug", "feature": "Feature", "task": "Task", "chore": "Task"}
)
DEFAULT_PRIORITY_FIELD = "Priority"
DEFAULT_PRIORITIES: Mapping[str, str] = MappingProxyType(
    {"p0": "Urgent", "p1": "High", "p2": "Medium", "p3": "Low", "p4": "Low"}
)


@dataclass(frozen=True)
class ValueSettings:
    """The value maps: bead type to issue type (keys case-insensitive), and bead priority
    (``P0`` to ``P4``, any spelling :func:`~.labels.normalize_priority` reads) to the option of
    ``priority_field``. An empty map projects nothing for that value."""

    issue_types: Mapping[str, str] = field(default_factory=lambda: dict(DEFAULT_ISSUE_TYPES))
    priority_field: str = DEFAULT_PRIORITY_FIELD
    priorities: Mapping[str, str] = field(default_factory=lambda: dict(DEFAULT_PRIORITIES))

    def __post_init__(self) -> None:
        types = {key.strip().lower(): value for key, value in self.issue_types.items()}
        priorities: dict[str, str] = {}
        for key, value in self.priorities.items():
            normal = normalize_priority(key)
            if normal is None:
                raise ValueError(f"priority_field.map: {key!r} is not a priority (P0 to P4)")
            priorities[normal] = value
        if not self.priority_field.strip():
            raise ValueError("priority_field.name must not be blank")
        object.__setattr__(self, "issue_types", MappingProxyType(types))
        object.__setattr__(self, "priorities", MappingProxyType(priorities))


@dataclass(frozen=True)
class ValueChange:
    """One value a sync sets: ``name`` is ``type`` (the issue type) or a field's name, ``live``
    what the issue holds now (``""``: unset) and ``desired`` what the bead projects."""

    name: str
    live: str
    desired: str


@dataclass(frozen=True)
class DesiredValues:
    """The issue type and field values a report's issue should carry (``None`` / empty: nothing
    projected), and the notes of what was skipped and why."""

    issue_type: str | None = None
    fields: tuple[tuple[str, str], ...] = ()
    notes: tuple[str, ...] = ()


def _first_mapped(
    candidates: tuple[str | None, ...], table: Mapping[str, str]
) -> tuple[str | None, str | None]:
    """``(mapped value, first unmapped key)``: the first candidate the table names wins."""
    unmapped = None
    for key in candidates:
        if not key:
            continue
        if key in table:
            return table[key], None
        unmapped = unmapped or key
    return None, unmapped


def desired_values(
    settings: ValueSettings,
    report: ReportFacts,
    work: WorkFacts | None = None,
    *,
    issue_type: bool = True,
    fields: bool = True,
) -> DesiredValues:
    """The type (with ``issue_type``) and the priority field value (with ``fields``) the report's
    issue should carry, from the tracked work falling back to the report. An unmapped value is
    skipped with a note naming the key to set."""
    w = work or WorkFacts("")
    notes: list[str] = []
    wanted_type = None
    if issue_type:
        kinds = tuple((t or "").strip().lower() or None for t in (w.type, report.type))
        wanted_type, unmapped = _first_mapped(kinds, settings.issue_types)
        if wanted_type is None and unmapped:
            notes.append(
                f"type {unmapped!r} is not mapped (intake.external.labels.issue_type.map); "
                "the issue type is skipped"
            )
    wanted_fields: tuple[tuple[str, str], ...] = ()
    if fields:
        ranks = (normalize_priority(w.priority), normalize_priority(report.priority))
        option, unmapped = _first_mapped(ranks, settings.priorities)
        if option is not None:
            wanted_fields = ((settings.priority_field, option),)
        elif unmapped:
            notes.append(
                f"priority {unmapped.upper()} is not mapped "
                f"(intake.external.labels.priority_field.map); {settings.priority_field} is skipped"
            )
    return DesiredValues(wanted_type, wanted_fields, tuple(notes))


def type_change(desired: DesiredValues, live: str | None) -> tuple[ValueChange | None, str]:
    """The issue type to set (``None``: none), or the note of why it is not compared: an issue
    whose type was not read is never assumed untyped."""
    if desired.issue_type is None:
        return None, ""
    if live is None:
        return None, "the issue's type was not read (the tracker reports none); skipped"
    if live.casefold() == desired.issue_type.casefold():
        return None, ""
    return ValueChange(TYPE_VALUE, live, desired.issue_type), ""


def field_changes(
    desired: DesiredValues, live: tuple[tuple[str, str], ...] | None
) -> tuple[tuple[ValueChange, ...], str]:
    """The field values to set, or the note of why they are not compared (not read)."""
    if not desired.fields:
        return (), ""
    if live is None:
        return (), "the issue's field values were not read (the tracker reports none); skipped"
    held = dict(live)
    return (
        tuple(
            ValueChange(name, held.get(name, ""), value)
            for name, value in desired.fields
            if held.get(name, "") != value
        ),
        "",
    )


__all__ = [
    "DEFAULT_ISSUE_TYPES",
    "DEFAULT_PRIORITIES",
    "DEFAULT_PRIORITY_FIELD",
    "DesiredValues",
    "ValueChange",
    "ValueSettings",
    "desired_values",
    "field_changes",
    "type_change",
]
