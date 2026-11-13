"""Phase B6 of the backfill pipeline: unit status, lifecycle dates and status conflicts.

:func:`derive_lifecycle` runs over a B4 :class:`~beadlog_sources.collate.Collation` (it needs the
in-memory B1 indexes) and fills two members, per ``docs/design/backfill-history-pipeline-adr.md``
(B6): ``lifecycle`` (one :class:`Lifecycle` per unit, sorted by unit) and ``conflicts``
(:class:`Conflict`, sorted). Both are stored as JSON objects in the collation (read them back
with :func:`lifecycles` / :func:`conflicts_of` / :func:`lifecycle_of`); the units are untouched.

**Status** comes from history only: a plan item per its adapter (``done_marker``), a merged
review, a committed change, a released tag, an add commit or a closed tracker item is ``closed``;
an unmerged review, an open tracker item or a plan item not done is ``open``; a unit that claims
nothing evidential (an area epic) is ``closed`` when it has children and all are. A design
record's own claim (``declared_status``) never sets a status **against evidence**: only a unit
with no evidential closure (``basis`` ``doc`` or ``none``) consults it, through
:attr:`StatusOptions.doc_default` (``from-doc`` by default): backfill writes a timeline, so a
draft or proposed doc's add commit is the unit's creation, never a recorded close.

**In progress** (bh-1vkff.3): a unit with an opened, not abandoned, review has a ``started``
moment (the review's ``opened_at``, see :data:`IN_PROGRESS_SIGNALS`); on a closed unit only a start
strictly between ``created`` and ``closed`` is kept, so a unit reads created -> in_progress ->
closed. Status stays ``open``/``closed``; the projector turns ``started`` into the transition.

**Dates**: ``created`` is the earliest of the first change, the review's open, the doc's add
commit and the tracker item's open (a doc with no add commit falls back to its declared date); a
closed unit's ``closed`` is the merge (review ``merged_at``, else a merge commit), else the
release, else its last change or add. A :class:`Moment` carries its ``precision``: ``second`` for
a timestamp, ``day`` for a ``YYYY-MM-DD`` declared date. Moments compare at day precision as
soon as either side is a day (a timestamp contributes its UTC day); a day is never read as
midnight.

**Conflicts** (:data:`CONFLICT_RULES`) are reported with both sources (artifact key, field, raw
and normalized value) and the unit's derived status, which a conflict never changes:

- ``doc-draft-code-landed``: an attached doc declares ``draft``/``proposed`` but the change or PR
  that added it also landed code;
- ``doc-design-only-feat``: an attached doc's raw status says "design only" (see
  :attr:`StatusOptions.design_only`) but its unit holds a ``feat`` change;
- ``doc-accepted-no-code``: a doc declares ``accepted`` after the last release (day precision)
  and its unit has no code (a doc's date falls back to its add commit);
- ``plan-open-summary``: a plan item is not done but its summary doc is attached;
- ``clock-doc-date`` (opt-in): a doc's declared date and its add commit's day differ by more than
  :attr:`StatusOptions.date_tolerance_days`;
- ``clock-review-merge`` (opt-in): a review merged more than
  :attr:`StatusOptions.clock_tolerance_seconds` before the first change of its unit was committed.

The two ``clock-*`` rules are **off by default** (:data:`DEFAULT_RULES` holds the four others):
declared doc dates routinely differ from add-commit days by days or weeks, and on by default they
would bury the four rules above. Enable them through :attr:`StatusOptions.rules`.

Pure and deterministic: the same collation, the same bytes.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from typing import Any

from .collate import Collation, CollationFormatError, Indexes, Unit
from .evidence import DECLARED_STATUSES, Artifact, Category

STATUS_OPEN = "open"
STATUS_CLOSED = "closed"
STATUSES = (STATUS_OPEN, STATUS_CLOSED)

# What a unit's status rests on.
BASES = (
    "plan-item",
    "merged",
    "committed",
    "released",
    "tracker",
    "review",
    "doc",
    "children",
    "none",
)
PRECISIONS = ("second", "day")

RULE_DOC_DRAFT_LANDED = "doc-draft-code-landed"
RULE_DOC_DESIGN_ONLY = "doc-design-only-feat"
RULE_DOC_ACCEPTED_NO_CODE = "doc-accepted-no-code"
RULE_PLAN_OPEN_SUMMARY = "plan-open-summary"
RULE_CLOCK_DOC_DATE = "clock-doc-date"
RULE_CLOCK_REVIEW_MERGE = "clock-review-merge"
CONFLICT_RULES = (
    RULE_DOC_DRAFT_LANDED,
    RULE_DOC_DESIGN_ONLY,
    RULE_DOC_ACCEPTED_NO_CODE,
    RULE_PLAN_OPEN_SUMMARY,
    RULE_CLOCK_DOC_DATE,
    RULE_CLOCK_REVIEW_MERGE,
)

# The rules on by default; the clock rules are opt-in (``StatusOptions(rules=CONFLICT_RULES)``).
DEFAULT_RULES = tuple(r for r in CONFLICT_RULES if not r.startswith("clock-"))

DOC_DEFAULTS = ("closed", "open", "from-doc")
DEFAULT_DOC_OPEN = ("draft", "proposed")

# The options recorded before ``doc_default`` existed; a record that lacks the last two was
# derived with today's closed-by-add-commit rule, so it reads back as ``doc_default = "closed"``.
_LEGACY_OPTION_MEMBERS = (
    "rules",
    "design_only",
    "summary_suffix",
    "date_tolerance_days",
    "clock_tolerance_seconds",
)
_DOC_OPTION_MEMBERS = (*_LEGACY_OPTION_MEMBERS, "doc_default", "doc_open")
# ``close_on_evidence`` / ``in_progress_on_evidence`` (bh-1vkff.3) steer the projector's writes
# on a re-run; a record written before them reads back with both on, the default.
OPTION_MEMBERS = (*_DOC_OPTION_MEMBERS, "close_on_evidence", "in_progress_on_evidence")
DOC_PREFIX = "doc:"


class StatusOptionsError(ValueError):
    """A B6 option outside its domain."""


def _is_int(value: Any, minimum: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _words(text: str) -> str:
    return " ".join(text.lower().replace("-", " ").replace("_", " ").split())


@dataclass(frozen=True)
class StatusOptions:
    """B6 options (``backfill.collate.status``).

    - ``rules``: the conflict rules to run (distinct names from :data:`CONFLICT_RULES`;
      default :data:`DEFAULT_RULES`, i.e. all but the opt-in ``clock-*`` rules). Status and
      dates are always derived.
    - ``design_only``: phrases (case-insensitive, ``-``/``_`` read as a space) that make a doc's
      raw declared status "design only" (default ``("design only",)``).
    - ``summary_suffix``: a doc whose file name ends with this (case-insensitive) is the summary
      of the plan item it is attached to (default ``-summary.md``, the gsd convention).
    - ``date_tolerance_days``: how many days a doc's declared date may differ from the day its
      add commit landed before the clocks disagree (default 1: declared dates are written by
      hand, in any zone).
    - ``clock_tolerance_seconds``: how long before its first change a review may be merged before
      the clocks disagree (default 86400).
    - ``doc_default``: what a unit with no evidential closure (``basis`` ``doc`` or ``none``)
      becomes: ``from-doc`` (default) is open when a doc of the unit declares a status in
      ``doc_open`` or a raw status matching ``design_only``, else closed; ``closed`` is the 0.36
      behaviour (the add commit closes the unit, else its declared date); ``open`` leaves every
      such unit open. Evidence
      (a merged or committed change, a release, a closed tracker item, a plan done marker) always
      wins, and every conflict rule still fires.
    - ``doc_open``: the declared statuses (:data:`~beadlog_sources.evidence.DECLARED_STATUSES`)
      that keep a doc unit open under ``from-doc`` (default ``("draft", "proposed")``).
    - ``close_on_evidence`` (default ``True``): a re-run appends the ``closed`` transition to an
      open (or in-progress) ``origin:backfill`` bead whose unit now has closing evidence, at the
      evidence's own moment. ``False`` leaves such a bead in its current phase.
    - ``in_progress_on_evidence`` (default ``True``): a unit with in-progress evidence (an opened
      review, see :data:`IN_PROGRESS_SIGNALS`) gets an ``in_progress`` transition at the
      evidence's own moment, on creation and on a re-run. ``False`` writes none.
    """

    rules: tuple[str, ...] = DEFAULT_RULES
    design_only: tuple[str, ...] = ("design only",)
    summary_suffix: str = "-summary.md"
    date_tolerance_days: int = 1
    clock_tolerance_seconds: int = 86400
    doc_default: str = "from-doc"
    doc_open: tuple[str, ...] = DEFAULT_DOC_OPEN
    close_on_evidence: bool = True
    in_progress_on_evidence: bool = True

    def __post_init__(self) -> None:
        rules = self.rules
        if isinstance(rules, str) or not isinstance(rules, Iterable):
            raise StatusOptionsError("rules must be a list of strings")
        rules = tuple(rules)
        if (
            not all(isinstance(r, str) for r in rules)
            or any(r not in CONFLICT_RULES for r in rules)
            or len(set(rules)) != len(rules)
        ):
            raise StatusOptionsError(
                f"rules must be distinct names from {CONFLICT_RULES}, got {list(rules)}"
            )
        object.__setattr__(self, "rules", rules)
        phrases = self.design_only
        if isinstance(phrases, str) or not isinstance(phrases, Iterable):
            raise StatusOptionsError("design_only must be a list of strings")
        phrases = tuple(phrases)
        if not all(isinstance(p, str) and p.strip() for p in phrases):
            raise StatusOptionsError("design_only must be a list of non-empty strings")
        object.__setattr__(self, "design_only", tuple(_words(p) for p in phrases))
        if not isinstance(self.summary_suffix, str) or not self.summary_suffix:
            raise StatusOptionsError("summary_suffix must be a non-empty string")
        if not _is_int(self.date_tolerance_days, 0):
            raise StatusOptionsError("date_tolerance_days must be a non-negative integer")
        if not _is_int(self.clock_tolerance_seconds, 0):
            raise StatusOptionsError("clock_tolerance_seconds must be a non-negative integer")
        if self.doc_default not in DOC_DEFAULTS:
            raise StatusOptionsError(f"doc_default must be one of {DOC_DEFAULTS}")
        opens = self.doc_open
        if isinstance(opens, str) or not isinstance(opens, Iterable):
            raise StatusOptionsError("doc_open must be a list of strings")
        opens = tuple(opens)
        if (
            not all(isinstance(o, str) for o in opens)
            or any(o not in DECLARED_STATUSES for o in opens)
            or len(set(opens)) != len(opens)
        ):
            raise StatusOptionsError(
                f"doc_open must be distinct names from {DECLARED_STATUSES}, got {list(opens)}"
            )
        object.__setattr__(self, "doc_open", opens)
        for name in ("close_on_evidence", "in_progress_on_evidence"):
            if not isinstance(getattr(self, name), bool):
                raise StatusOptionsError(f"{name} must be a boolean")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> StatusOptions:
        """Options from a ``backfill.collate.status`` table; other members are ignored."""
        return cls(**{name: raw[name] for name in OPTION_MEMBERS if name in raw})

    def to_json(self) -> dict[str, Any]:
        """The effective options as a JSON object (the collation's ``options.status``)."""
        return {
            "rules": list(self.rules),
            "design_only": list(self.design_only),
            "summary_suffix": self.summary_suffix,
            "date_tolerance_days": self.date_tolerance_days,
            "clock_tolerance_seconds": self.clock_tolerance_seconds,
            "doc_default": self.doc_default,
            "doc_open": list(self.doc_open),
            "close_on_evidence": self.close_on_evidence,
            "in_progress_on_evidence": self.in_progress_on_evidence,
        }

    @classmethod
    def from_json(cls, raw: Any) -> StatusOptions:
        """Parse :meth:`to_json` output; unknown members are refused. A record written before
        ``doc_default`` existed (exactly the legacy members) reads back as ``closed``, the rule
        it was derived under."""
        if isinstance(raw, dict) and sorted(raw) == sorted(_LEGACY_OPTION_MEMBERS):
            return cls(**raw, doc_default="closed")
        if isinstance(raw, dict) and sorted(raw) == sorted(_DOC_OPTION_MEMBERS):
            return cls(**raw)
        if not isinstance(raw, dict) or sorted(raw) != sorted(OPTION_MEMBERS):
            raise StatusOptionsError(f"status options must have exactly {OPTION_MEMBERS}")
        return cls(**raw)


# -- records ------------------------------------------------------------------------------------


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


@dataclass(frozen=True)
class Moment:
    """A point in a unit's life: ``at`` (a canonical timestamp, or ``YYYY-MM-DD`` when
    ``precision`` is ``day``) and the artifact (or unit) ``source`` whose clock gave it."""

    at: str
    precision: str
    source: str

    def __post_init__(self) -> None:
        if self.precision not in PRECISIONS:
            raise CollationFormatError(f"moment: unknown precision {self.precision!r}")
        if not isinstance(self.at, str) or not isinstance(self.source, str) or not self.source:
            raise CollationFormatError("moment at and source must be strings")

    @classmethod
    def of(cls, at: str, source: str) -> Moment:
        """A moment of day precision for a ``YYYY-MM-DD``, else of second precision."""
        return cls(at, "day" if len(at) == 10 else "second", source)

    @property
    def day(self) -> date:
        """The calendar day (the UTC day of a timestamp)."""
        if self.precision == "day":
            return date.fromisoformat(self.at)
        return _timestamp(self.at).date()

    @property
    def sort_key(self) -> tuple[date, datetime]:
        """A total order over mixed precisions: the day, then the instant (a day's midnight
        only breaks ties between moments of one day)."""
        if self.precision == "second":
            return (self.day, _timestamp(self.at))
        return (self.day, datetime(self.day.year, self.day.month, self.day.day, tzinfo=UTC))

    def after(self, other: Moment) -> bool:
        """Strictly later; at day precision when either side is a day."""
        if self.precision == "day" or other.precision == "day":
            return self.day > other.day
        return _timestamp(self.at) > _timestamp(other.at)

    def to_json(self) -> dict[str, Any]:
        """The moment as a JSON object."""
        return {"at": self.at, "precision": self.precision, "source": self.source}

    @classmethod
    def from_json(cls, raw: Any) -> Moment:
        """Parse a moment object; unknown or missing members are refused."""
        if not isinstance(raw, dict) or sorted(raw) != ["at", "precision", "source"]:
            raise CollationFormatError("moment must have exactly at, precision, source")
        return cls(raw["at"], raw["precision"], raw["source"])


def has_no_moment(life: Lifecycle) -> bool:
    """Whether a unit has no evidence moment at all (no add commit, no declared date, no change):
    ``create = propose`` does not create it (bh-uysl1), since an open bead minted from nothing
    would pass for live work."""
    return life.created is None and life.closed is None and life.basis in ("none", "doc")


_LIFECYCLE_MEMBERS = ("unit", "status", "basis", "created", "closed")
# The signals that put a unit in progress (bh-1vkff.3), by what the evidence's own moment is. A
# commit that references the unit is not one: a committed change closes the unit (basis
# ``committed``). A tracker item carries no dated in-progress event (its labels and assignee
# have no time), so it is not one either; a plan item has only a done marker.
IN_PROGRESS_SIGNALS = ("review-opened",)
# The bases a re-run may close an open or in-progress backfilled bead on: a merged review or
# change, a committed change, a release, a closed tracker item, a plan done marker, or an epic
# whose children all closed. A doc's add commit (``doc``) is never closing evidence.
CLOSING_BASES = ("merged", "committed", "released", "tracker", "plan-item", "children")


@dataclass(frozen=True)
class Lifecycle:
    """One unit's derived status and dates. ``basis`` (:data:`BASES`) says what the status
    rests on; ``closed`` is ``None`` unless the unit is closed (or has no clock for it).
    ``started`` is the moment of the unit's in-progress evidence (:data:`IN_PROGRESS_SIGNALS`),
    or ``None``; on a closed unit it lies strictly between ``created`` and ``closed``, else it is
    dropped (the unit then reads created -> closed). It is written to the record only when set,
    so a record without one is unchanged."""

    unit: str
    status: str
    basis: str
    created: Moment | None = None
    closed: Moment | None = None
    started: Moment | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.unit, str) or not self.unit:
            raise CollationFormatError("lifecycle unit must be a non-empty string")
        if self.status not in STATUSES:
            raise CollationFormatError(f"lifecycle {self.unit!r}: unknown status {self.status!r}")
        if self.basis not in BASES:
            raise CollationFormatError(f"lifecycle {self.unit!r}: unknown basis {self.basis!r}")
        if self.closed is not None and self.status != STATUS_CLOSED:
            raise CollationFormatError(f"lifecycle {self.unit!r}: only a closed unit has closed")

    def to_json(self) -> dict[str, Any]:
        """The record as a JSON object."""
        out: dict[str, Any] = {
            "unit": self.unit,
            "status": self.status,
            "basis": self.basis,
            "created": None if self.created is None else self.created.to_json(),
            "closed": None if self.closed is None else self.closed.to_json(),
        }
        if self.started is not None:
            out["started"] = self.started.to_json()
        return out

    @classmethod
    def from_json(cls, raw: Any) -> Lifecycle:
        """Parse a record object; unknown or missing members are refused."""
        if not isinstance(raw, dict) or sorted(raw) not in (
            sorted(_LIFECYCLE_MEMBERS),
            sorted((*_LIFECYCLE_MEMBERS, "started")),
        ):
            raise CollationFormatError(
                f"lifecycle must have exactly {_LIFECYCLE_MEMBERS}, plus an optional started"
            )
        return cls(
            unit=raw["unit"],
            status=raw["status"],
            basis=raw["basis"],
            created=None if raw["created"] is None else Moment.from_json(raw["created"]),
            closed=None if raw["closed"] is None else Moment.from_json(raw["closed"]),
            started=None if raw.get("started") is None else Moment.from_json(raw["started"]),
        )


_SOURCE_MEMBERS = ("artifact", "field", "raw", "value", "at")


@dataclass(frozen=True)
class Source:
    """One side of a conflict: ``artifact`` (a key) claims ``value`` (normalized) for ``field``,
    written ``raw``; ``at`` is the moment compared, for the date rules."""

    artifact: str
    field: str
    raw: Any
    value: Any
    at: Moment | None = None

    def to_json(self) -> dict[str, Any]:
        """The side as a JSON object."""
        return {
            "artifact": self.artifact,
            "field": self.field,
            "raw": self.raw,
            "value": self.value,
            "at": None if self.at is None else self.at.to_json(),
        }

    @classmethod
    def from_json(cls, raw: Any) -> Source:
        """Parse a side object; unknown or missing members are refused."""
        if not isinstance(raw, dict) or sorted(raw) != sorted(_SOURCE_MEMBERS):
            raise CollationFormatError(f"conflict source must have exactly {_SOURCE_MEMBERS}")
        at = None if raw["at"] is None else Moment.from_json(raw["at"])
        return cls(raw["artifact"], raw["field"], raw["raw"], raw["value"], at)


_CONFLICT_MEMBERS = ("rule", "unit", "status", "sources", "detail")


@dataclass(frozen=True)
class Conflict:
    """Two artifacts that disagree about ``unit``. ``sources`` is exactly two :class:`Source`
    (the claim, then the evidence against it); ``status`` is the unit's status as derived from
    history, which the conflict leaves as it is."""

    rule: str
    unit: str
    status: str
    sources: tuple[Source, Source]
    detail: str = ""

    def __post_init__(self) -> None:
        if self.rule not in CONFLICT_RULES:
            raise CollationFormatError(f"conflict: unknown rule {self.rule!r}")
        if self.status not in STATUSES:
            raise CollationFormatError(f"conflict: unknown status {self.status!r}")
        if not isinstance(self.unit, str) or not self.unit or len(self.sources) != 2:
            raise CollationFormatError("conflict needs a unit and exactly two sources")

    def to_json(self) -> dict[str, Any]:
        """The conflict as a JSON object."""
        return {
            "rule": self.rule,
            "unit": self.unit,
            "status": self.status,
            "sources": [s.to_json() for s in self.sources],
            "detail": self.detail,
        }

    @classmethod
    def from_json(cls, raw: Any) -> Conflict:
        """Parse a conflict object; unknown or missing members are refused."""
        if not isinstance(raw, dict) or sorted(raw) != sorted(_CONFLICT_MEMBERS):
            raise CollationFormatError(f"conflict must have exactly {_CONFLICT_MEMBERS}")
        sources = tuple(Source.from_json(s) for s in raw["sources"])
        if len(sources) != 2:
            raise CollationFormatError("conflict needs exactly two sources")
        return cls(raw["rule"], raw["unit"], raw["status"], (sources[0], sources[1]), raw["detail"])


# -- derivation ---------------------------------------------------------------------------------

_DOC_EXTENSIONS = (".md", ".markdown", ".rst", ".txt", ".html", ".htm", ".adoc")


@dataclass
class _Evidence:
    """What a unit's own artifacts say (children are not consulted)."""

    status: str | None = None
    basis: str = "none"
    created: list[Moment] | None = None
    closed: list[tuple[int, Moment]] | None = None
    started: list[Moment] | None = None


def _is_doc(art: Artifact) -> bool:
    return art.category is Category.DESIGN_RECORD and art.key.startswith(DOC_PREFIX)


class _Deriver:
    def __init__(self, collation: Collation, indexes: Indexes, options: StatusOptions) -> None:
        self.options = options
        self.ix = indexes
        self.units = {u.key: u for u in collation.units}
        self.attached: dict[str, list[str]] = defaultdict(list)
        for a in collation.attachments:
            self.attached[a.unit].append(a.doc)
        self.merge_of: dict[str, set[str]] = defaultdict(set)
        for merge, kids in indexes.merge_children.items():
            for kid in kids:
                self.merge_of[kid].add(merge)
        self.doc_paths = {
            key[len(DOC_PREFIX) :] for key, found in indexes.by_key.items() if _is_doc(found[0])
        }
        self.releases = sorted(
            (
                (_timestamp(a.attrs["released_at"]), a)
                for found in indexes.by_key.values()
                for a in found
                if a.category is Category.RELEASE_MARKER and a.attrs["released_at"] is not None
            ),
            key=lambda pair: (pair[0], pair[1].key),
        )
        self._life: dict[str, Lifecycle] = {}
        self._visiting: set[str] = set()

    # -- helpers ---------------------------------------------------------------------------

    def _artifacts(self, unit: Unit) -> list[Artifact]:
        found = (self.ix.artifact(k) for k in unit.artifacts)
        return [a for a in found if a is not None]

    def _docs(self, unit: Unit) -> list[Artifact]:
        keys = {a.key for a in self._artifacts(unit) if _is_doc(a)}
        keys.update(self.attached.get(unit.key, ()))
        found = (self.ix.artifact(k) for k in sorted(keys))
        return [a for a in found if a is not None and _is_doc(a)]

    def _is_code(self, change: Artifact, doc: str | None = None) -> bool:
        """Whether a change touches a path that is not a doc (nor ``doc`` itself)."""
        own = doc[len(DOC_PREFIX) :] if doc else None
        return any(
            str(p) != own
            and str(p) not in self.doc_paths
            and not str(p).lower().endswith(_DOC_EXTENSIONS)
            for p in change.attrs["paths"]
        )

    def _code_changes(self, unit: Unit) -> list[Artifact]:
        return [
            a for a in self._artifacts(unit) if a.category is Category.CHANGE and self._is_code(a)
        ]

    @staticmethod
    def _moment(artifact: Artifact, name: str) -> Moment | None:
        value = artifact.attrs.get(name)
        return None if value is None else Moment.of(value, artifact.key)

    def _add_moment(self, doc: Artifact) -> Moment | None:
        added = self.ix.doc_add.get(doc.key)
        change = None if added is None else self.ix.artifact(added)
        return None if change is None else self._moment(change, "committed_at")

    # -- status and dates ------------------------------------------------------------------

    def _doc_open(self, unit: Unit) -> bool:
        """Whether ``doc_default`` keeps the unit's doc open (when no evidence closes the unit)."""
        mode = self.options.doc_default
        if mode == "closed":
            return False
        if mode == "open":
            return True
        for doc in self._docs(unit):
            if doc.attrs["declared_status"] in self.options.doc_open:
                return True
            raw = self._raw(doc, "declared_status")
            if isinstance(raw, str) and _words(raw) in self.options.design_only:
                return True
        return False

    def own(self, unit: Unit) -> _Evidence:
        """What the unit's own artifacts say, in the priority the module doc gives."""
        created: list[Moment] = []
        closing: list[tuple[int, Moment]] = []
        changes: list[Artifact] = []
        plan: Artifact | None = None
        merged = released = tracker_closed = doc_added = False
        open_review = open_tracker = False
        doc_open = self._doc_open(unit)
        doc_kept_open = False
        deferred: list[tuple[int, Moment]] = []
        started: list[Moment] = []
        for art in self._artifacts(unit):
            cat = art.category
            if cat is Category.CHANGE:
                changes.append(art)
                if (m := self._moment(art, "committed_at")) is not None:
                    created.append(m)
                    if art.kind == "merge":
                        closing.append((1, m))
            elif cat is Category.REVIEW:
                opened = self._moment(art, "opened_at") or (
                    Moment.of(art.occurred_at, art.key) if art.occurred_at else None
                )
                if opened is not None:
                    created.append(opened)
                if (m := self._moment(art, "merged_at")) is not None:
                    merged = True
                    closing.append((0, m))
                elif art.attrs["state"] == "open":
                    open_review = True
                if (began := self._moment(art, "opened_at")) is not None and art.attrs[
                    "state"
                ] != "closed":  # review-opened: an opened, not abandoned, review is work under way
                    started.append(began)
            elif cat is Category.TRACKER_ITEM:
                if (m := self._moment(art, "opened_at")) is not None:
                    created.append(m)
                if art.attrs["state"] == "closed":
                    tracker_closed = True
                    if (m := self._moment(art, "closed_at")) is not None:
                        closing.append((3, m))
                else:
                    open_tracker = True
            elif cat is Category.PLAN_ITEM:
                plan = plan or art
                if art.occurred_at:
                    m = Moment.of(art.occurred_at, art.key)
                    created.append(m)
                    if art.attrs["done_marker"]:
                        closing.append((4, m))
            elif cat is Category.DESIGN_RECORD:
                add = self._add_moment(art)
                declared = self._moment(art, "declared_date")
                if add is not None:
                    created.append(add)
                    if doc_open:  # a timeline: the add commit creates it, it does not close it
                        doc_kept_open = True
                        deferred.append((6, add))
                    else:
                        doc_added = True
                        closing.append((6, add))
                elif declared is not None:
                    created.append(declared)
                    if doc_open:
                        doc_kept_open = True
                        deferred.append((6, declared))
                    else:
                        doc_added = True  # no add commit: the declared date is the moment
                        closing.append((6, declared))
            elif (
                cat is Category.RELEASE_MARKER
                and (m := self._moment(art, "released_at")) is not None
            ):
                released = True
                created.append(m)
                closing.append((2, m))
        lasts = [m for a in changes if (m := self._moment(a, "committed_at")) is not None]
        if lasts:
            closing.append((5, max(lasts, key=lambda m: m.sort_key)))
        ev = _Evidence(created=created, closed=closing, started=started)
        if plan is not None:
            ev.status = STATUS_CLOSED if plan.attrs["done_marker"] else STATUS_OPEN
            ev.basis = "plan-item"
        elif merged or changes:
            ev.status, ev.basis = STATUS_CLOSED, "merged" if merged else "committed"
        elif released:
            ev.status, ev.basis = STATUS_CLOSED, "released"
        elif tracker_closed:
            ev.status, ev.basis = STATUS_CLOSED, "tracker"
        elif doc_added:
            ev.status, ev.basis = STATUS_CLOSED, "doc"
        elif open_review:
            ev.status, ev.basis = STATUS_OPEN, "review"
        elif open_tracker:
            ev.status, ev.basis = STATUS_OPEN, "tracker"
        elif doc_kept_open:
            ev.status, ev.basis = STATUS_OPEN, "doc"
        if ev.status == STATUS_CLOSED:  # evidence closed it: the doc's moment may still close it
            closing.extend(deferred)
        return ev

    def lifecycle(self, key: str) -> Lifecycle:
        """The derived lifecycle of unit ``key`` (a unit with no evidence of its own is derived
        from its children)."""
        if key in self._life:
            return self._life[key]
        unit = self.units[key]
        ev = self.own(unit)
        created = min((ev.created or []), key=lambda m: m.sort_key, default=None)
        if ev.status is None:
            self._visiting.add(key)
            kids = [
                self.lifecycle(c)
                for c in unit.children
                if c in self.units and c not in self._visiting
            ]
            self._visiting.discard(key)
            if kids:
                done = all(k.status == STATUS_CLOSED for k in kids)
                firsts = [k.created for k in kids if k.created is not None]
                lasts = [k.closed for k in kids if k.closed is not None]
                life = Lifecycle(
                    key,
                    STATUS_CLOSED if done else STATUS_OPEN,
                    "children",
                    min(firsts, key=lambda m: m.sort_key, default=None),
                    max(lasts, key=lambda m: m.sort_key, default=None) if done else None,
                )
            else:
                life = Lifecycle(key, STATUS_OPEN, "none", created)
        else:
            closed = None
            if ev.status == STATUS_CLOSED and ev.closed:
                best = min(p for p, _ in ev.closed)
                closed = max((m for p, m in ev.closed if p == best), key=lambda m: m.sort_key)
            if created is None and closed is not None:
                created = closed
            began = min(ev.started or [], key=lambda m: m.sort_key, default=None)
            if began is not None and closed is not None:
                # created -> in_progress -> closed: only a start strictly between the two counts
                inside = created is not None and began.after(created) and closed.after(began)
                began = began if inside else None
            life = Lifecycle(key, ev.status, ev.basis, created, closed, began)
        self._life[key] = life
        return life

    # -- conflicts -------------------------------------------------------------------------

    @staticmethod
    def _raw(art: Artifact, name: str) -> Any:
        return art.raw_attrs.get(name, art.attrs.get(name))

    def _landed_with(self, unit: Unit, doc: Artifact) -> Artifact | None:
        """The code change that landed in the same commit or PR as ``doc``, else ``None``."""
        added = self.ix.doc_add.get(doc.key)
        if added is None:
            return None
        vias = {added, *self.ix.pr_of_commit.get(added, ()), *self.merge_of.get(added, ())}
        if not vias & set(unit.artifacts):
            return None
        change = self.ix.artifact(added)
        if change is not None and self._is_code(change, doc.key):
            return change
        for key in unit.artifacts:
            other = self.ix.artifact(key)
            if other is not None and key != added and other.category is Category.CHANGE:
                return other
        return None

    def conflicts(self, unit: Unit, status: str) -> list[Conflict]:
        """The conflicts of ``unit`` (derived ``status``) under the enabled rules."""
        enabled = set(self.options.rules)
        found: list[Conflict] = []

        def add(rule: str, a: Source, b: Source, detail: str) -> None:
            if rule in enabled:
                found.append(Conflict(rule, unit.key, status, (a, b), detail))

        arts = self._artifacts(unit)
        changes = [a for a in arts if a.category is Category.CHANGE]
        docs = self._docs(unit)
        for doc in docs:
            declared = doc.attrs["declared_status"]
            raw_status = self._raw(doc, "declared_status")
            claim = Source(doc.key, "declared_status", raw_status, declared)
            if declared in ("draft", "proposed"):
                code = self._landed_with(unit, doc)
                if code is not None:
                    add(
                        RULE_DOC_DRAFT_LANDED,
                        claim,
                        Source(code.key, "landed", code.attrs["committed_at"], "landed"),
                        f"doc says {declared} but code landed in the same change or PR",
                    )
            if isinstance(raw_status, str) and _words(raw_status) in self.options.design_only:
                feat = next((c for c in changes if c.attrs["cc_type"] == "feat"), None)
                if feat is not None:
                    add(
                        RULE_DOC_DESIGN_ONLY,
                        claim,
                        Source(feat.key, "cc_type", self._raw(feat, "cc_type"), "feat"),
                        "doc says design only but a feat change is attached",
                    )
            if declared == "accepted" and self.releases and not self._code_changes(unit):
                when = self._moment(doc, "declared_date") or self._add_moment(doc)
                last = self.releases[-1][1]
                released = Moment.of(last.attrs["released_at"], last.key)
                if when is not None and when.after(released):
                    add(
                        RULE_DOC_ACCEPTED_NO_CODE,
                        replace(claim, at=when),
                        Source(
                            last.key,
                            "released_at",
                            self._raw(last, "released_at"),
                            last.attrs["released_at"],
                            released,
                        ),
                        "doc accepted after the last release with no code attached",
                    )
            declared_day = self._moment(doc, "declared_date")
            added = self._add_moment(doc)
            if declared_day is not None and added is not None:
                gap = abs((declared_day.day - added.day).days)
                if gap > self.options.date_tolerance_days:
                    add(
                        RULE_CLOCK_DOC_DATE,
                        Source(
                            doc.key,
                            "declared_date",
                            self._raw(doc, "declared_date"),
                            doc.attrs["declared_date"],
                            declared_day,
                        ),
                        Source(added.source, "committed_at", added.at, added.at, added),
                        f"declared date and add commit are {gap} days apart",
                    )
        plans = [a for a in arts if a.category is Category.PLAN_ITEM]
        if plans and not plans[0].attrs["done_marker"]:
            suffix = self.options.summary_suffix.lower()
            for doc in docs:
                if doc.key[len(DOC_PREFIX) :].rsplit("/", 1)[-1].lower().endswith(suffix):
                    add(
                        RULE_PLAN_OPEN_SUMMARY,
                        Source(plans[0].key, "done_marker", False, STATUS_OPEN),
                        Source(doc.key, "summary", doc.key[len(DOC_PREFIX) :], "exists"),
                        "plan item is not done but its summary exists",
                    )
        firsts = [m for c in changes if (m := self._moment(c, "committed_at")) is not None]
        if firsts:
            first = min(firsts, key=lambda m: m.sort_key)
            for review in (a for a in arts if a.category is Category.REVIEW):
                merged = self._moment(review, "merged_at")
                if merged is None:
                    continue
                gap = (_timestamp(first.at) - _timestamp(merged.at)).total_seconds()
                if gap > self.options.clock_tolerance_seconds:
                    add(
                        RULE_CLOCK_REVIEW_MERGE,
                        Source(
                            review.key,
                            "merged_at",
                            self._raw(review, "merged_at"),
                            merged.at,
                            merged,
                        ),
                        Source(first.source, "committed_at", first.at, first.at, first),
                        "review merged before its first change was committed",
                    )
        return found


def derive_lifecycle(collation: Collation, options: StatusOptions | None = None) -> Collation:
    """B6: ``collation`` with ``lifecycle`` and ``conflicts`` filled and ``options.status`` set
    (see the module doc).

    Needs the in-memory B1 indexes and the B3/B4 units (a collation from
    :func:`~beadlog_sources.collate.collate`, not one loaded from disk).
    """
    if collation.indexes is None:
        raise CollationFormatError("status needs the B1 indexes (a loaded collation has none)")
    options = options or StatusOptions()
    deriver = _Deriver(collation, collation.indexes, options)
    lives = [deriver.lifecycle(u.key) for u in collation.units]
    conflicts: list[Conflict] = []
    for unit, life in zip(collation.units, lives, strict=True):
        conflicts.extend(deriver.conflicts(unit, life.status))
    conflicts.sort(key=lambda c: (c.unit, c.rule, c.sources[0].artifact, c.sources[1].artifact))
    return replace(
        collation,
        lifecycle=tuple(life.to_json() for life in sorted(lives, key=lambda x: x.unit)),
        conflicts=tuple(c.to_json() for c in conflicts),
        options={**collation.options, "status": options.to_json()},
    )


def lifecycles(collation: Collation) -> tuple[Lifecycle, ...]:
    """The collation's lifecycle records, typed."""
    return tuple(Lifecycle.from_json(raw) for raw in collation.lifecycle)


def lifecycle_of(collation: Collation, unit: str) -> Lifecycle | None:
    """The lifecycle of ``unit``, or ``None``."""
    return next((life for life in lifecycles(collation) if life.unit == unit), None)


def conflicts_of(collation: Collation, unit: str | None = None) -> tuple[Conflict, ...]:
    """The collation's conflicts, typed; only those of ``unit`` when given."""
    found = tuple(Conflict.from_json(raw) for raw in collation.conflicts)
    return found if unit is None else tuple(c for c in found if c.unit == unit)


def validate_lifecycle(collation: Collation) -> None:
    """Refuse a collation whose ``lifecycle`` or ``conflicts`` are not B6 records."""
    lifecycles(collation)
    conflicts_of(collation)


__all__ = [
    "BASES",
    "CLOSING_BASES",
    "CONFLICT_RULES",
    "DEFAULT_RULES",
    "IN_PROGRESS_SIGNALS",
    "OPTION_MEMBERS",
    "STATUSES",
    "Conflict",
    "Lifecycle",
    "Moment",
    "Source",
    "StatusOptions",
    "StatusOptionsError",
    "conflicts_of",
    "derive_lifecycle",
    "lifecycle_of",
    "lifecycles",
    "validate_lifecycle",
]
