"""Phase B4 of the backfill pipeline: attach design records (docs) to work units.

:func:`attach` runs over a B3 :class:`~beadlog_sources.collate.Collation` (it needs the in-memory
B1 indexes). Every ``design-record`` artifact (key ``doc:<path>``) is attached to **at most one**
unit, by the first tier that finds one (``docs/design/backfill-history-pipeline-adr.md``, B4).
The tiers, in default order (:data:`ATTACH_TIERS`), each recording the artifact keys behind the
decision as evidence:

1. ``explicit``: the doc refs a unit's anchor or member (a resolved B2 ref whose target a unit
   claimed, or a ``bead-token`` that resolved ``local`` to an ``attach`` unit), or a unit's
   artifact (a commit, a PR) refs the doc. A ``path`` ref in the doc is not explicit (it is a
   shared path, tier 3). Evidence: the claimed artifact keys (``bead:<id>`` for
   a local bead token). This is the only tier that may attach to an ``epic`` unit.
2. ``same-change``: the commit that first added the doc (B1 ``doc_add``) is a member of a unit,
   or its PR / the merge that integrates it is, and that unit also holds code: the adding commit
   touched a non-doc path, or the unit has another change. Evidence: the adding commit and the
   claiming artifact.
3. ``shared-path``: a unit's change touched a path the doc names (a ``path`` ref that resolved to
   changes), or touched the doc's own path after it was added. Evidence: those change keys.
4. ``lexical``: the doc's title and file stem share enough words with a unit's title: at least
   :attr:`AttachOptions.lexical_min_tokens` shared tokens and an overlap coefficient
   (shared / the smaller token set, in percent) of at least :attr:`AttachOptions.lexical_percent`.
   Evidence: the unit's anchor artifact.

Within a tier the unit with the most evidence wins, then the smaller key (lexical: highest score,
then the smaller key). A doc no tier attaches becomes a **doc-unit** (``issue`` unit, rule ``doc``,
keyed by the doc's key and claiming it), matching the pre-pipeline docs behaviour; set
:attr:`AttachOptions.standalone` false to leave it unplaced. One
:class:`~beadlog_sources.collate.Attachment` per doc records the unit, the reason and the evidence.
An attached doc is not added to the unit's ``artifacts``.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from .collate import (
    ARTIFACT,
    ATTACH,
    EPIC,
    ISSUE,
    REASON_EXPLICIT,
    REASON_LEXICAL,
    REASON_SAME_CHANGE,
    REASON_SHARED_PATH,
    REASON_STANDALONE,
    RULE_DOC,
    Attachment,
    Collation,
    CollationFormatError,
    Indexes,
    Unit,
    claimed_artifacts,
)
from .evidence import Category
from .ledger import BEAD_TOKEN, LOCAL

ATTACH_TIERS = (REASON_EXPLICIT, REASON_SAME_CHANGE, REASON_SHARED_PATH, REASON_LEXICAL)
OPTION_MEMBERS = ("tiers", "lexical_percent", "lexical_min_tokens", "standalone")
DOC_PREFIX = "doc:"

_TOKEN = re.compile(r"[a-z0-9]+")
# Words that carry no topic: conventional-commit types and common glue.
_STOPWORDS = frozenset(
    "feat fix docs chore refactor test tests perf build style revert merge the and for with from "
    "into this that its not are was how why what when use add adds added new via per all any "
    "design record plan adr doc readme index notes note draft".split()
)


class AttachOptionsError(ValueError):
    """A B4 option outside its domain."""


@dataclass(frozen=True)
class AttachOptions:
    """B4 options (``backfill.collate.attach``; defaults per the ADR).

    - ``tiers``: the attach tiers to run, in order (distinct names from :data:`ATTACH_TIERS`;
      drop one to turn it off, e.g. drop ``lexical`` for evidence-only attachment).
    - ``lexical_percent``: the minimum overlap coefficient, an integer percent in ``1..100``
      (default 60; an integer because the collation's canonical JSON admits no floats).
    - ``lexical_min_tokens``: the fewest shared tokens the lexical tier accepts (default 2).
    - ``standalone``: make a doc-unit of a doc no tier attached (default on).
    """

    tiers: tuple[str, ...] = ATTACH_TIERS
    lexical_percent: int = 60
    lexical_min_tokens: int = 2
    standalone: bool = True

    def __post_init__(self) -> None:
        tiers = self.tiers
        if isinstance(tiers, str) or not isinstance(tiers, Iterable):
            raise AttachOptionsError("tiers must be a list of strings")
        tiers = tuple(tiers)
        if (
            not all(isinstance(t, str) for t in tiers)
            or any(t not in ATTACH_TIERS for t in tiers)
            or len(set(tiers)) != len(tiers)
        ):
            raise AttachOptionsError(
                f"tiers must be distinct names from {ATTACH_TIERS}, got {list(tiers)}"
            )
        object.__setattr__(self, "tiers", tiers)
        percent = self.lexical_percent
        if not isinstance(percent, int) or isinstance(percent, bool) or not 1 <= percent <= 100:
            raise AttachOptionsError("lexical_percent must be an integer in 1..100")
        count = self.lexical_min_tokens
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            raise AttachOptionsError("lexical_min_tokens must be a positive integer")
        if not isinstance(self.standalone, bool):
            raise AttachOptionsError("standalone must be a boolean")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> AttachOptions:
        """Options from a ``backfill.collate.attach`` table; other members are ignored."""
        return cls(**{name: raw[name] for name in OPTION_MEMBERS if name in raw})

    def to_json(self) -> dict[str, Any]:
        """The effective options as a JSON object (the collation's ``options.attach``)."""
        return {
            "tiers": list(self.tiers),
            "lexical_percent": self.lexical_percent,
            "lexical_min_tokens": self.lexical_min_tokens,
            "standalone": self.standalone,
        }

    @classmethod
    def from_json(cls, raw: Any) -> AttachOptions:
        """Parse :meth:`to_json` output; missing or unknown members are refused."""
        if not isinstance(raw, dict) or sorted(raw) != sorted(OPTION_MEMBERS):
            raise AttachOptionsError(f"attach options must have exactly {OPTION_MEMBERS}")
        return cls(**raw)


def tokens(text: str) -> frozenset[str]:
    """The topic words of ``text``: lowercase alphanumerics of 3+ characters, no stopwords."""
    return (
        frozenset(t for t in _TOKEN.findall(text.lower()) if len(t) >= 3 and not t.isdigit())
        - _STOPWORDS
    )


def _stem(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[0] if "." in name else name


class _Attacher:
    def __init__(self, collation: Collation, indexes: Indexes, options: AttachOptions) -> None:
        self.options = options
        self.ix = indexes
        self.units = {u.key: u for u in collation.units}
        self.claimed = claimed_artifacts(collation.units)
        self.bead_unit = {u.bead: u.key for u in collation.units if u.kind == ATTACH and u.bead}
        self.docs = sorted(
            key
            for key, found in indexes.by_key.items()
            if found[0].category is Category.DESIGN_RECORD
        )
        doc_set = set(self.docs)
        self.doc_paths = {k[len(DOC_PREFIX) :] for k in doc_set if k.startswith(DOC_PREFIX)}
        self.refs_of: dict[str, list[Any]] = defaultdict(list)
        self.cited_by: dict[str, set[str]] = defaultdict(set)
        for r in collation.refs:
            self.refs_of[r.source].append(r)
            if r.outcome == ARTIFACT:
                for target in r.targets:
                    if target in doc_set and target != r.source:
                        self.cited_by[target].add(r.source)
        self.merge_of: dict[str, set[str]] = defaultdict(set)
        for merge, kids in indexes.merge_children.items():
            for kid in kids:
                self.merge_of[kid].add(merge)
        self.unit_tokens = {u.key: tokens(u.title) for u in collation.units if u.kind != EPIC}

    # -- helpers ---------------------------------------------------------------------------

    def _usable(self, unit: str, tier: str) -> bool:
        """Only an explicit ref may reach an epic."""
        return tier == REASON_EXPLICIT or self.units[unit].kind != EPIC

    def _best(self, found: dict[str, set[str]]) -> tuple[str, tuple[str, ...]] | None:
        if not found:
            return None
        unit = min(found, key=lambda u: (-len(found[u]), u))
        return unit, tuple(sorted(found[unit]))

    def _is_change(self, key: str) -> bool:
        art = self.ix.artifact(key)
        return art is not None and art.category is Category.CHANGE

    # -- tiers -----------------------------------------------------------------------------

    def explicit(self, doc: str) -> tuple[str, tuple[str, ...]] | None:
        found: dict[str, set[str]] = defaultdict(set)
        for r in self.refs_of.get(doc, ()):
            if r.outcome == ARTIFACT and r.ref.kind != "path":
                for target in r.targets:
                    if target != doc and target in self.claimed:
                        found[self.claimed[target]].add(target)
            elif r.outcome == LOCAL and r.ref.kind == BEAD_TOKEN:
                bead = r.ref.raw.strip()
                if bead in self.bead_unit:
                    found[self.bead_unit[bead]].add(f"bead:{bead}")
        for source in sorted(self.cited_by.get(doc, ())):
            if source in self.claimed:
                found[self.claimed[source]].add(source)
        return self._best(found)

    def same_change(self, doc: str) -> tuple[str, tuple[str, ...]] | None:
        added = self.ix.doc_add.get(doc)
        if added is None:
            return None
        vias = {added, *self.ix.pr_of_commit.get(added, ()), *self.merge_of.get(added, ())}
        found: dict[str, set[str]] = defaultdict(set)
        for via in vias:
            unit = self.claimed.get(via)
            if unit is None or not self._usable(unit, REASON_SAME_CHANGE):
                continue
            found[unit].update({added, via})
        art = self.ix.artifact(added)
        adds_code = art is not None and any(
            str(p) != doc[len(DOC_PREFIX) :] and str(p) not in self.doc_paths
            for p in art.attrs["paths"]
        )
        for unit in list(found):
            has_other_change = any(
                a != added and self._is_change(a) for a in self.units[unit].artifacts
            )
            if not (adds_code or has_other_change):
                del found[unit]
        return self._best(found)

    def shared_path(self, doc: str) -> tuple[str, tuple[str, ...]] | None:
        changes: set[str] = set()
        for r in self.refs_of.get(doc, ()):
            if r.outcome == ARTIFACT and r.ref.kind == "path":
                changes.update(t for t in r.targets if self._is_change(t))
        added = self.ix.doc_add.get(doc)
        changes.update(
            c for c in self.ix.path_changes.get(doc[len(DOC_PREFIX) :], ()) if c != added
        )
        found: dict[str, set[str]] = defaultdict(set)
        for change in changes:
            unit = self.claimed.get(change)
            if unit is not None and self._usable(unit, REASON_SHARED_PATH):
                found[unit].add(change)
        return self._best(found)

    def lexical(self, doc: str) -> tuple[str, tuple[str, ...]] | None:
        art = self.ix.artifact(doc)
        assert art is not None
        words = tokens(art.title) | tokens(_stem(doc[len(DOC_PREFIX) :]))
        best: tuple[int, str] | None = None
        for key in sorted(self.unit_tokens):
            shared = len(words & self.unit_tokens[key])
            size = min(len(words), len(self.unit_tokens[key]))
            if shared < self.options.lexical_min_tokens or not size:
                continue
            score = shared * 100 // size
            if score >= self.options.lexical_percent and (best is None or score > best[0]):
                best = (score, key)
        if best is None:
            return None
        unit = self.units[best[1]]
        return unit.key, (unit.artifacts[0] if unit.artifacts else unit.key,)

    # -- run -------------------------------------------------------------------------------

    def run(self) -> tuple[tuple[Unit, ...], tuple[Attachment, ...]]:
        tiers = {
            REASON_EXPLICIT: self.explicit,
            REASON_SAME_CHANGE: self.same_change,
            REASON_SHARED_PATH: self.shared_path,
            REASON_LEXICAL: self.lexical,
        }
        attachments: list[Attachment] = []
        doc_units: list[Unit] = []
        for doc in self.docs:
            if doc in self.claimed:
                continue
            for tier in self.options.tiers:
                hit = tiers[tier](doc)
                if hit is not None:
                    attachments.append(Attachment(doc, hit[0], tier, hit[1]))
                    break
            else:
                if not self.options.standalone:
                    continue
                art = self.ix.artifact(doc)
                assert art is not None
                doc_units.append(
                    Unit(key=doc, kind=ISSUE, rule=RULE_DOC, title=art.title, artifacts=(doc,))
                )
                attachments.append(Attachment(doc, doc, REASON_STANDALONE, ()))
        units = sorted((*self.units.values(), *doc_units), key=lambda u: u.key)
        return tuple(units), tuple(attachments)


def attach(collation: Collation, options: AttachOptions | None = None) -> Collation:
    """B4: ``collation`` with docs attached (``attachments``, doc-units added to ``units``) and
    ``options.attach`` set (see the module doc).

    Needs the in-memory B1 indexes and the B3 units (a collation from
    :func:`~beadlog_sources.collate.collate`, not one loaded from disk).
    """
    if collation.indexes is None:
        raise CollationFormatError("attach needs the B1 indexes (a loaded collation has none)")
    options = options or AttachOptions()
    units, attachments = _Attacher(collation, collation.indexes, options).run()
    return replace(
        collation,
        units=units,
        attachments=attachments,
        options={**collation.options, "attach": options.to_json()},
    )


__all__ = [
    "ATTACH_TIERS",
    "OPTION_MEMBERS",
    "AttachOptions",
    "AttachOptionsError",
    "attach",
    "tokens",
]
