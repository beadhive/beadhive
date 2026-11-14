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

Opt-in extras (``docs/design/backfill-merge-heavy-repos-adr.md``, D8), all off by default so the
default output is byte-identical:

- ``own-add`` (a tier, outside :data:`ATTACH_TIERS`; place it after ``same-change`` to use it): a
  doc whose first-add commit exists but no unit claimed gets a doc-unit of its own, its evidence
  that add commit (and the anchor of the unit that implements it, when one is found).
- :attr:`AttachOptions.max_per_unit`: a unit takes at most that many attachments from the
  guessing tiers (``shared-path``, ``lexical``); ``explicit`` and ``same-change`` are exempt. The
  docs the unit would take beyond the cap, weakest evidence first, go to the next tier, then
  stand alone.
- :attr:`AttachOptions.rename_guard`: a doc moved by its first-add commit (the commit's
  ``renamed`` attr, read under the git reader option ``keep_renames``) is not attached through
  that commit.
- :attr:`AttachOptions.rank` ``proximity``: among ``shared-path`` candidates, the nearest unit in
  time to the doc's add commit wins (then subject overlap, then the number of paths touched, then
  key), and the runner-up is recorded on the attachment.

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
from datetime import datetime
from typing import Any

from .collate import (
    ARTIFACT,
    ATTACH,
    EPIC,
    ISSUE,
    REASON_EXPLICIT,
    REASON_LEXICAL,
    REASON_OWN_ADD,
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
# Every tier an option list may name; ``own-add`` is opt-in and so is not in ATTACH_TIERS.
KNOWN_TIERS = (
    REASON_EXPLICIT,
    REASON_SAME_CHANGE,
    REASON_OWN_ADD,
    REASON_SHARED_PATH,
    REASON_LEXICAL,
)
# The tiers that guess (``max_per_unit`` binds only these).
CAPPED_TIERS = (REASON_SHARED_PATH, REASON_LEXICAL)
RANK_EVIDENCE = "evidence"
RANK_PROXIMITY = "proximity"
RANKS = (RANK_EVIDENCE, RANK_PROXIMITY)
# Written to JSON only when set (D9): a collation without them is the 0.37 shape.
BASE_OPTION_MEMBERS = ("tiers", "lexical_percent", "lexical_min_tokens", "standalone")
NEW_OPTION_MEMBERS = ("max_per_unit", "rename_guard", "rank")
OPTION_MEMBERS = (*BASE_OPTION_MEMBERS, *NEW_OPTION_MEMBERS)
_FAR = 1 << 60  # a time distance nothing can beat for "unknown"
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
    - ``max_per_unit``: the most attachments a unit takes from ``shared-path`` and ``lexical``
      (default ``None``: no cap). ``explicit`` and ``same-change`` are exempt.
    - ``rename_guard``: do not attach a moved doc through the commit that moved it (default off).
    - ``rank``: ``evidence`` (default) picks a ``shared-path`` unit by evidence count;
      ``proximity`` by time distance to the add commit, subject overlap, edit size, then key, and
      records the runner-up.
    """

    tiers: tuple[str, ...] = ATTACH_TIERS
    lexical_percent: int = 60
    lexical_min_tokens: int = 2
    standalone: bool = True
    max_per_unit: int | None = None
    rename_guard: bool = False
    rank: str = RANK_EVIDENCE

    def __post_init__(self) -> None:
        tiers = self.tiers
        if isinstance(tiers, str) or not isinstance(tiers, Iterable):
            raise AttachOptionsError("tiers must be a list of strings")
        tiers = tuple(tiers)
        if (
            not all(isinstance(t, str) for t in tiers)
            or any(t not in KNOWN_TIERS for t in tiers)
            or len(set(tiers)) != len(tiers)
        ):
            raise AttachOptionsError(
                f"tiers must be distinct names from {KNOWN_TIERS}, got {list(tiers)}"
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
        cap = self.max_per_unit
        if cap is not None and (not isinstance(cap, int) or isinstance(cap, bool) or cap < 1):
            raise AttachOptionsError("max_per_unit must be a positive integer or unset")
        if not isinstance(self.rename_guard, bool):
            raise AttachOptionsError("rename_guard must be a boolean")
        if self.rank not in RANKS:
            raise AttachOptionsError(f"rank must be one of {RANKS}")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> AttachOptions:
        """Options from a ``backfill.collate.attach`` table; other members are ignored."""
        return cls(**{name: raw[name] for name in OPTION_MEMBERS if name in raw})

    def to_json(self) -> dict[str, Any]:
        """The effective options as a JSON object (the collation's ``options.attach``)."""
        out: dict[str, Any] = {
            "tiers": list(self.tiers),
            "lexical_percent": self.lexical_percent,
            "lexical_min_tokens": self.lexical_min_tokens,
            "standalone": self.standalone,
        }
        # The newer members are written only when set (D9): a run that sets none writes 0.37 bytes.
        if self.max_per_unit is not None:
            out["max_per_unit"] = self.max_per_unit
        if self.rename_guard:
            out["rename_guard"] = True
        if self.rank != RANK_EVIDENCE:
            out["rank"] = self.rank
        return out

    @classmethod
    def from_json(cls, raw: Any) -> AttachOptions:
        """Parse :meth:`to_json` output: the four base members plus any subset of the newer ones
        (an absent one reads as 0.37 behaved); missing or unknown members are refused."""
        if not isinstance(raw, dict) or not (
            set(BASE_OPTION_MEMBERS) <= set(raw) <= set(OPTION_MEMBERS)
        ):
            raise AttachOptionsError(
                f"attach options must have exactly {BASE_OPTION_MEMBERS} plus any of "
                f"{NEW_OPTION_MEMBERS}"
            )
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


def _seconds(stamp: str | None) -> int | None:
    if not stamp:
        return None
    try:
        return int(datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


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
        self.runner_up: dict[str, dict[str, Any]] = {}  # doc -> its shared-path runner-up
        self.lexical_score: dict[str, int] = {}  # doc -> the score of its lexical candidate

    # -- helpers ---------------------------------------------------------------------------

    def _usable(self, unit: str, tier: str) -> bool:
        """Only an explicit ref may reach an epic."""
        return tier == REASON_EXPLICIT or self.units[unit].kind != EPIC

    def _best(self, found: dict[str, set[str]]) -> tuple[str, tuple[str, ...]] | None:
        if not found:
            return None
        unit = min(found, key=lambda u: (-len(found[u]), u))
        return unit, tuple(sorted(found[unit]))

    def _renamed_by(self, doc: str, change: str | None) -> bool:
        """Whether ``change`` moved ``doc`` (rename_guard; needs the reader's ``renamed`` attr)."""
        if not self.options.rename_guard or change is None:
            return False
        art = self.ix.artifact(change)
        return art is not None and doc[len(DOC_PREFIX) :] in art.attrs.get("renamed", ())

    def _unit_seconds(self, unit: str) -> int | None:
        for key in self.units[unit].artifacts:
            art = self.ix.artifact(key)
            if art is not None and _seconds(art.occurred_at) is not None:
                return _seconds(art.occurred_at)
        return None

    def _unit_paths(self, unit: str) -> int:
        total = 0
        for key in self.units[unit].artifacts:
            art = self.ix.artifact(key)
            if art is not None and art.category is Category.CHANGE:
                total += len(art.attrs["paths"])
        return total

    def _rank_key(self, doc: str, unit: str) -> tuple[tuple[int, int, int, str], dict[str, Any]]:
        """Proximity rank of ``unit`` for ``doc``: time distance to the doc's add commit, then
        subject overlap (more first), then paths touched (more first), then key."""
        added = self.ix.artifact(self.ix.doc_add.get(doc, ""))
        then, now = _seconds(added.occurred_at) if added else None, self._unit_seconds(unit)
        distance = abs(then - now) if then is not None and now is not None else _FAR
        art = self.ix.artifact(doc)
        assert art is not None
        words = tokens(art.title) | tokens(_stem(doc[len(DOC_PREFIX) :]))
        overlap = len(words & tokens(self.units[unit].title))
        paths = self._unit_paths(unit)
        shown = {
            "unit": unit,
            "distance": None if distance == _FAR else distance,
            "overlap": overlap,
            "paths": paths,
        }
        return (distance, -overlap, -paths, unit), shown

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
        if added is None or self._renamed_by(doc, added):
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

    def _shared_candidates(self, doc: str) -> dict[str, set[str]]:
        changes: set[str] = set()
        for r in self.refs_of.get(doc, ()):
            if r.outcome == ARTIFACT and r.ref.kind == "path":
                changes.update(t for t in r.targets if self._is_change(t))
        added = self.ix.doc_add.get(doc)
        changes.update(
            c for c in self.ix.path_changes.get(doc[len(DOC_PREFIX) :], ()) if c != added
        )
        found: dict[str, set[str]] = defaultdict(set)
        for change in sorted(changes):
            if self._renamed_by(doc, change):
                continue
            unit = self.claimed.get(change)
            if unit is not None and self._usable(unit, REASON_SHARED_PATH):
                found[unit].add(change)
        return found

    def shared_path(self, doc: str) -> tuple[str, tuple[str, ...]] | None:
        found = self._shared_candidates(doc)
        if self.options.rank != RANK_PROXIMITY or not found:
            return self._best(found)
        ranked = sorted(self._rank_key(doc, unit) for unit in found)
        unit = ranked[0][0][3]
        if len(ranked) > 1:
            self.runner_up[doc] = ranked[1][1]
        return unit, tuple(sorted(found[unit]))

    def own_add(self, doc: str) -> tuple[str, tuple[str, ...]] | None:
        added = self.ix.doc_add.get(doc)
        if added is None or added in self.claimed or self._renamed_by(doc, added):
            return None
        evidence = [added]
        found = self._shared_candidates(doc)
        if found:  # name the implementing unit's anchor so a later bead can link to it
            unit = self.units[self._best(found)[0]]  # type: ignore[index]
            anchor = unit.artifacts[0] if unit.artifacts else unit.key
            if anchor not in evidence:
                evidence.append(anchor)
        return doc, tuple(evidence)

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
        self.lexical_score[doc] = best[0]
        unit = self.units[best[1]]
        return unit.key, (unit.artifacts[0] if unit.artifacts else unit.key,)

    # -- run -------------------------------------------------------------------------------

    def _capped(
        self, tier: str, hits: dict[str, tuple[str, tuple[str, ...]]], taken: dict[str, int]
    ) -> dict[str, tuple[str, tuple[str, ...]]]:
        """Keep, per unit, the strongest hits of a guessing tier that fit under ``max_per_unit``
        (weakest evidence first to overflow, then the larger doc key); the rest fall through."""
        cap = self.options.max_per_unit
        if cap is None or tier not in CAPPED_TIERS:
            return hits
        by_unit: dict[str, list[str]] = defaultdict(list)
        for doc, (unit, _) in hits.items():
            by_unit[unit].append(doc)

        def weight(doc: str) -> int:
            return self.lexical_score[doc] if tier == REASON_LEXICAL else len(hits[doc][1])

        kept: dict[str, tuple[str, tuple[str, ...]]] = {}
        for unit, docs in by_unit.items():
            room = max(cap - taken[unit], 0)
            for doc in sorted(docs, key=lambda d: (-weight(d), d))[:room]:
                kept[doc] = hits[doc]
        return kept

    def run(self) -> tuple[tuple[Unit, ...], tuple[Attachment, ...]]:
        tiers = {
            REASON_EXPLICIT: self.explicit,
            REASON_SAME_CHANGE: self.same_change,
            REASON_OWN_ADD: self.own_add,
            REASON_SHARED_PATH: self.shared_path,
            REASON_LEXICAL: self.lexical,
        }
        attachments: dict[str, Attachment] = {}
        doc_units: list[Unit] = []
        taken: dict[str, int] = defaultdict(int)  # guessed attachments per unit
        pending = [doc for doc in self.docs if doc not in self.claimed]
        # Tier-major: each tier sees every doc the earlier tiers left (a doc's own outcome never
        # depends on another's until a cap binds), so a cap can order a unit's candidates.
        for tier in self.options.tiers:
            hits = {}
            for doc in pending:
                hit = tiers[tier](doc)
                if hit is not None:
                    hits[doc] = hit
            for doc, (unit, evidence) in sorted(self._capped(tier, hits, taken).items()):
                runner = self.runner_up.get(doc) if tier == REASON_SHARED_PATH else None
                attachments[doc] = Attachment(doc, unit, tier, evidence, runner)
                if tier in CAPPED_TIERS:
                    taken[unit] += 1
                if tier == REASON_OWN_ADD:
                    art = self.ix.artifact(doc)
                    assert art is not None
                    doc_units.append(
                        Unit(key=doc, kind=ISSUE, rule=RULE_DOC, title=art.title, artifacts=(doc,))
                    )
            pending = [doc for doc in pending if doc not in attachments]
        if self.options.standalone:
            for doc in pending:
                art = self.ix.artifact(doc)
                assert art is not None
                doc_units.append(
                    Unit(key=doc, kind=ISSUE, rule=RULE_DOC, title=art.title, artifacts=(doc,))
                )
                attachments[doc] = Attachment(doc, doc, REASON_STANDALONE, ())
        units = sorted((*self.units.values(), *doc_units), key=lambda u: u.key)
        return tuple(units), tuple(attachments[doc] for doc in sorted(attachments))


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
    "BASE_OPTION_MEMBERS",
    "KNOWN_TIERS",
    "NEW_OPTION_MEMBERS",
    "OPTION_MEMBERS",
    "RANKS",
    "AttachOptions",
    "AttachOptionsError",
    "attach",
    "tokens",
]
