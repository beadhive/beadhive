"""The neutral ``docs`` strategy: decision and design docs -> ``external_ref`` links to beads.

BeadHive-free (``docs/design/backfill-platform-adr.md`` section 2). It ports the docs mode of the
``bh:backfill`` skill's ``reconcile.sh`` and fixes its truncation class by construction: every
document is processed, the bead corpus comes in ONCE (:attr:`ExtractRequest.baseline`), git is read
once (one ``git log`` for every document's first-add commit) and no document costs a store call.

**Deterministic before fuzzy** (the skill's ``references/bridges.md``). Per document, in order:

0. *reverse ``external_ref``* — a baseline bead already points at the doc: ``PRESENT-in-sync``;
1. *frontmatter back-ref* — the first ``Beads:`` / ``- Bead:`` line of the doc names a bead id
   (the last id on the line wins);
2. *git add-trailer* — a bead id in the subject of the commit that first added the doc;
3. *lexical shortlist* — only for what bridges 0-2 left: the top-k baseline beads by token
   overlap with the doc, handed to the judge as one choice. The shortlist **narrows, it never
   decides**; with no judge every such doc is an abstain residual listed for a human.

Bridges 1 and 2 give the doc a bead and are classified against that bead's ``external_ref``:

================== ======================================= ==================================
status             meaning                                 engine class / event
================== ======================================= ==================================
PRESENT-in-sync    the bead already points at the doc      PRESENT; no event
PRESENT-needs-stamp the bead has no ``external_ref``       DRIFT; a ``set`` of ``external_ref``
DRIFTED-ref        the bead points at a different doc      DRIFT; reported, never overwritten
DANGLING           the named bead is not in the baseline   DRIFT; reported, no event
UNMATCHED          no bridge resolved it                   judged NEW / PRESENT / NOISE, or an
                                                           abstain residual for a human
================== ======================================= ==================================

The classes are those of :mod:`beadlog_sources.reconcile`; a ``set`` on an existing bead is a DRIFT
there and the neutral ``bh backfill apply`` applies only NEW, so stamps are applied through a
StoreLoader.

**Judging.** The residual is one :class:`ChoiceQuery` per doc (family :data:`FAMILY`): ``present:N``
(a shortlisted bead already represents the doc), ``noise:N`` (a shortlisted epic covers it),
``new`` and ``abstain``. Every verdict (outcome, probabilities, confidence, option count, engine,
model, fallback) is recorded in the report exactly as reconcile records one, and reused from a
``prior`` extract archive by its query hash, so a recorded verdict is never asked again. A judged
``present`` yields the same ``external_ref`` stamp (provenance ``inferred``, the judge's
confidence);
a judged ``new`` yields a ``create`` plus ``origin:backfill`` / ``source:docs`` labels. A doc with
no shortlisted candidate is NEW without a question (as in reconcile). The judge arrives as
:attr:`ExtractRequest.judge`; ``None`` is "judge off" and no doc is ever sent anywhere.

Options (``ExtractRequest.options``): ``docs`` (comma-separated directories; every ``*.md``
below each, recursively; default ``docs/decisions,docs/design``, whose ``*.md`` children only),
``top_k`` (shortlist size, default 3), ``id_prefix``
(prefix of minted ids; required when a doc would mint a bead, never derived from the baseline),
``new_type`` (bead type of a judged NEW, default ``task``), ``labels`` (extra labels
for judged NEW beads, e.g. the hive identity triplet), ``prior`` (an earlier extract ``.bdlg``
whose recorded verdicts are reused), ``allow_shallow`` and ``git``.
"""

from __future__ import annotations

import heapq
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from beadlog import (
    BeadEvent,
    BeadRef,
    BeadState,
    Evidence,
    fold,
    instant,
    mint_event,
    read_archive,
)

from ._sources import (
    AddRecord,
    SourceError,
    csv,
    doc_title,
    external_refs,
    first_adds,
    floors,
    follow_add,
    is_git_repo,
    live_beads,
    mint_bead_id,
    truthy,
)
from .commits import candidate_pattern, derive_namespaces, extract_candidates, require_full_history
from .contracts import ABSTAIN, ChoiceQuery, ExtractRequest, Judgment, JudgmentPort
from .judges import tokens
from .reconcile import ABSTAIN_TEXT, NEW_KEY, NEW_TEXT, Verdict, query_hash

STRATEGY_NAME = "docs"
STRATEGY_VERSION = "0.1.0"
SOURCE_SYSTEM = "docs"
ANNOTATION_KEY = "bdlg.docs"
FAMILY = "docs.match"
EXTERNAL_REF = "external_ref"
DEFAULT_DIRS = ("docs/decisions", "docs/design")
DEFAULT_TOP_K = 3
MIN_TOKEN = 4  # shortlist tokens are longer than three characters, as in reconcile.sh
NEW_LABELS = ("origin:backfill", "source:docs")
SKIP_TYPES = frozenset({"gate", "event", "molecule"})
QUESTION = (
    "A repository document is described in the context. Which option is true? 'present:N' means "
    "existing bead N already represents this document's work; 'noise:N' means the work is "
    "covered by the coarser existing epic N; 'new' means no existing bead represents it; "
    "'abstain' means you cannot tell."
)
_EXCERPT = 1500
_CONTEXT_LIMIT = 4000
_BEADS_LINE = re.compile(r"^\s*-?\s*beads?:", re.IGNORECASE)
_HYPHEN_TAIL = re.compile(r"-[0-9a-z]+$")
_STEM_PREFIX = re.compile(r"^[0-9]+[-_]")


class DocStatus(StrEnum):
    """The five statuses of ``reconcile.sh`` (docs mode)."""

    IN_SYNC = "PRESENT-in-sync"
    NEEDS_STAMP = "PRESENT-needs-stamp"
    DRIFTED_REF = "DRIFTED-ref"
    DANGLING = "DANGLING"
    UNMATCHED = "UNMATCHED"


# The engine class each status maps to (see the module table); a judged UNMATCHED carries its own.
ENGINE_CLASS: Mapping[DocStatus, str] = {
    DocStatus.IN_SYNC: "PRESENT",
    DocStatus.NEEDS_STAMP: "DRIFT",
    DocStatus.DRIFTED_REF: "DRIFT",
    DocStatus.DANGLING: "DRIFT",
    DocStatus.UNMATCHED: "NEW",
}


# -- the pure bridges ----------------------------------------------------------------------


def resolve_id(candidate: str, live_ids: frozenset[str] | set[str]) -> str | None:
    """Map an id-shaped token to a live bead id, or ``None``.

    Exact first, then trimmed back one ``-segment`` at a time (``bh-example-foo`` ->
    ``bh-example``). A ``.N`` child suffix is never trimmed: ``bh-example.5`` is a different
    bead from ``bh-example``.
    """
    probe = candidate
    while True:
        if probe in live_ids:
            return probe
        trimmed = _HYPHEN_TAIL.sub("", probe, count=1)
        if trimmed == probe or "-" not in trimmed:
            return None
        probe = trimmed


def frontmatter_candidate(text: str, pattern: re.Pattern[str]) -> str | None:
    """Bridge 1: the id-shaped token a doc's first ``Beads:`` line names (the last one on the
    line), or ``None``. Only the FIRST such line is read, as in ``reconcile.sh``."""
    for line in text.splitlines():
        if _BEADS_LINE.match(line):
            found = extract_candidates(line, pattern)
            return found[-1] if found else None
    return None


def commit_candidate(
    subject: str, pattern: re.Pattern[str], live_ids: frozenset[str] | set[str]
) -> str | None:
    """Bridge 2: the first token of a commit subject that resolves to a live bead. Unlike the
    script (which reports the first id-shaped token DANGLING), tokens that name no bead
    (``bh-infra``, a scope) are skipped so they cannot hide a doc from the shortlist."""
    for candidate in extract_candidates(subject, pattern):
        hit = resolve_id(candidate, live_ids)
        if hit is not None:
            return hit
    return None


def query_tokens(path: str, text: str) -> frozenset[str]:
    """What a doc is searched by: its file name (numeric prefix dropped) and its title."""
    stem = _STEM_PREFIX.sub("", Path(path).stem).replace("-", " ").replace("_", " ")
    title = doc_title(text, "")
    return frozenset(token for token in tokens(f"{stem} {title}") if len(token) >= MIN_TOKEN)


@dataclass(frozen=True)
class Candidate:
    """One shortlisted bead: id, title, type and overlap score."""

    bead: str
    title: str
    type: str | None
    score: int

    def to_json(self) -> dict[str, Any]:
        return {"bead": self.bead, "title": self.title, "type": self.type, "score": self.score}


class Shortlist:
    """Token overlap over the title and description of every baseline bead.

    Built once from the folded baseline; ``top`` costs the postings of the query tokens, not the
    corpus, and never touches a store. A title hit scores 2, a description-only hit 1; ties break
    on bead id so the shortlist is deterministic.
    """

    def __init__(self, live: Mapping[str, BeadState]) -> None:
        self._ids = sorted(i for i, state in live.items() if (state.type or "") not in SKIP_TYPES)
        self._states = [live[i] for i in self._ids]
        self._title: dict[str, list[int]] = {}
        self._desc: dict[str, list[int]] = {}
        for index, state in enumerate(self._states):
            title = tokens(_text(state, "title"))
            body = tokens(_text(state, "description"))
            for token in title:
                if len(token) >= MIN_TOKEN:
                    self._title.setdefault(token, []).append(index)
            for token in body - title:
                if len(token) >= MIN_TOKEN:
                    self._desc.setdefault(token, []).append(index)

    def __len__(self) -> int:
        return len(self._ids)

    def top(self, query: Iterable[str], k: int) -> list[Candidate]:
        scores: dict[int, int] = {}
        for token in query:
            for index in self._title.get(token, ()):
                scores[index] = scores.get(index, 0) + 2
            for index in self._desc.get(token, ()):
                scores[index] = scores.get(index, 0) + 1
        best = heapq.nsmallest(max(k, 0), scores.items(), key=lambda item: (-item[1], item[0]))
        return [
            Candidate(self._ids[i], _text(self._states[i], "title"), self._states[i].type, score)
            for i, score in best
        ]


def _text(state: BeadState, name: str) -> str:
    value = state.fields.get(name)
    return value if isinstance(value, str) else ""


# -- discovery -----------------------------------------------------------------------------


def discover_docs(root: Path | str, dirs: Sequence[str] | None = None) -> list[str]:
    """Doc paths (posix, relative to ``root``, sorted). ``dirs=None`` is the default pair, whose
    ``*.md`` children only; explicit directories are walked recursively."""
    base = Path(root)
    found: set[str] = set()
    if dirs is None:
        for name in DEFAULT_DIRS:
            folder = base / name
            if folder.is_dir():
                found.update(p.relative_to(base).as_posix() for p in folder.glob("*.md"))
    else:
        for name in dirs:
            folder = base / name
            if folder.is_dir():
                found.update(p.relative_to(base).as_posix() for p in folder.rglob("*.md"))
            elif folder.is_file():
                found.add(folder.relative_to(base).as_posix())
    return sorted(path for path in found if (base / path).is_file())


# -- the report ----------------------------------------------------------------------------


@dataclass
class DocEntry:
    """What happened to one doc."""

    doc: str
    status: DocStatus
    bead: str | None = None
    bridge: str | None = None
    note: str = ""
    outcome: str | None = None  # a judged UNMATCHED: new / present / noise / abstain
    match: str | None = None
    covered_by: str | None = None
    candidates: tuple[Candidate, ...] = ()
    verdict: Verdict | None = None
    events: int = 0

    @property
    def engine_class(self) -> str:
        if self.status is not DocStatus.UNMATCHED:
            return ENGINE_CLASS[self.status]
        return {"present": "PRESENT", "noise": "NOISE"}.get(self.outcome or "", "NEW")

    @property
    def residual(self) -> bool:
        """An abstain residual: unmatched and undecided, listed for a human to confirm."""
        return self.status is DocStatus.UNMATCHED and self.outcome in (None, "abstain")

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "doc": self.doc,
            "status": str(self.status),
            "class": self.engine_class,
            "events": self.events,
        }
        for name in ("bead", "bridge", "outcome", "match", "covered_by"):
            value = getattr(self, name)
            if value is not None:
                out[name] = value
        if self.note:
            out["note"] = self.note
        if self.candidates:
            out["candidates"] = [c.to_json() for c in self.candidates]
        if self.verdict is not None:
            out["verdict"] = self.verdict.to_json()
            out["review"] = "required"
        if self.residual:
            out["review"] = "required"
        return out


@dataclass
class DocsReport:
    """What one extraction saw, for the operator and the archive annotation."""

    dirs: tuple[str, ...]
    top_k: int
    judge: Mapping[str, Any]
    beads: int = 0
    calls: int = 0
    entries: list[DocEntry] = field(default_factory=list)

    @property
    def processed(self) -> int:
        return len(self.entries)

    def counts(self) -> dict[str, int]:
        out = {str(status): 0 for status in DocStatus}
        for entry in self.entries:
            out[str(entry.status)] += 1
        return out

    def drift(self) -> list[dict[str, Any]]:
        """DRIFTED-ref and DANGLING docs, in the shape the ``beadlog`` CLI prints."""
        return [
            {"bead": e.bead or e.doc, "reason": f"{e.doc}: {e.status} ({e.note})"}
            for e in self.entries
            if e.status in (DocStatus.DRIFTED_REF, DocStatus.DANGLING)
        ]

    def to_json(self) -> dict[str, Any]:
        residual = [e.doc for e in self.entries if e.residual]
        return {
            "strategy": STRATEGY_NAME,
            "version": STRATEGY_VERSION,
            "dirs": list(self.dirs),
            "top_k": self.top_k,
            "beads": self.beads,
            "docs": self.processed,
            "counts": self.counts(),
            "residual": residual,
            "judge": dict(self.judge),
            "judge_calls": self.calls,
            "proposed": sum(entry.events for entry in self.entries),
            "drift": self.drift(),
            "entries": [entry.to_json() for entry in self.entries],
        }


@dataclass(frozen=True)
class DocsProposal:
    events: tuple[BeadEvent, ...]
    report: DocsReport


# -- judging -------------------------------------------------------------------------------


def _identity(judge: JudgmentPort | None) -> dict[str, Any]:
    if judge is None:
        return {"engine": None, "version": None, "model": None}
    return {
        "engine": str(getattr(judge, "engine", type(judge).__name__)),
        "version": str(getattr(judge, "engine_version", "0")),
        "model": getattr(judge, "model", None),
    }


def build_query(
    doc: str, text: str, candidates: Sequence[Candidate]
) -> tuple[ChoiceQuery, dict[str, str]]:
    """The doc's choice and the bead each non-trivial option stands for."""
    options: dict[str, str] = {}
    targets: dict[str, str] = {}
    for number, candidate in enumerate(candidates, start=1):
        options[f"present:{number}"] = candidate.title
        targets[f"present:{number}"] = candidate.bead
    for number, candidate in enumerate((c for c in candidates if c.type == "epic"), start=1):
        options[f"noise:{number}"] = candidate.title
        targets[f"noise:{number}"] = candidate.bead
    options[NEW_KEY] = NEW_TEXT
    options[ABSTAIN] = ABSTAIN_TEXT
    title = doc_title(text, Path(doc).stem)
    context = f"{title}\npath: {doc}\n\n{text[:_EXCERPT]}"[:_CONTEXT_LIMIT]
    return ChoiceQuery(QUESTION, options, context, FAMILY), targets


def ask(judge: JudgmentPort, query: ChoiceQuery) -> Judgment:
    """One ``choose``; an outcome that is not an offered option is refused (becomes an abstain)."""
    verdict = judge.choose(query)
    if verdict.outcome is not None and verdict.outcome not in query.options:
        return Judgment(
            None,
            verdict.probabilities,
            0.0,
            verdict.option_count,
            f"refused outcome {verdict.outcome!r}: not an offered option",
            verdict.engine,
            verdict.engine_version,
            verdict.model,
            verdict.fallback,
        )
    return verdict


def prior_from_archive(path: str | Path) -> dict[str, Verdict]:
    """Verdicts recorded in an earlier ``docs`` extract archive, by query hash."""
    manifest, _ = read_archive(path)
    block = (manifest.annotations or {}).get(ANNOTATION_KEY)
    out: dict[str, Verdict] = {}
    if isinstance(block, Mapping):
        for entry in block.get("entries") or ():
            raw = entry.get("verdict") if isinstance(entry, Mapping) else None
            if isinstance(raw, Mapping):
                verdict = Verdict.from_json(raw)
                out[verdict.query_hash] = verdict
    return out


# -- the strategy --------------------------------------------------------------------------


def propose(
    request: ExtractRequest,
    docs: Sequence[str] | None = None,
    *,
    dirs: Sequence[str] | None = None,
    top_k: int = DEFAULT_TOP_K,
    prior: Mapping[str, Verdict] | None = None,
    git: str = "git",
) -> DocsProposal:
    """Classify every doc against ``request.baseline`` and emit the events (module doc).

    ``docs`` overrides discovery with explicit doc paths. The root need not be a git repository;
    then bridge 2 is skipped and events are undated.
    """
    root = Path(request.root)
    prior = dict(prior or {})
    judge = request.judge
    states = fold(request.baseline)
    live = live_beads(states, request.group)
    live_ids = frozenset(live)
    refs = external_refs(live)
    namespaces = derive_namespaces(live_ids)
    pattern = candidate_pattern(namespaces) if namespaces else None
    paths = list(docs) if docs is not None else discover_docs(root, dirs)
    repo = is_git_repo(root, git)
    adds = first_adds(root, (), git) if repo else {}
    stamp_floors = floors(request.baseline, request.group, EXTERNAL_REF)
    prefix = str(request.options.get("id_prefix") or "") or None
    new_type = str(request.options.get("new_type", "task"))
    extra_labels = csv(request.options.get("labels", ""))
    report = DocsReport(
        tuple(dirs) if dirs is not None else DEFAULT_DIRS, top_k, _identity(judge), beads=len(live)
    )
    events: list[BeadEvent] = []
    taken = set(live_ids)
    shortlist: Shortlist | None = None

    def add_record(path: str) -> AddRecord | None:
        record = adds.get(path)
        if record is None and repo:
            record = follow_add(root, path, git)  # a renamed doc: only these cost a git call
        return record

    def stamp(
        path: str, bead: str, record: AddRecord | None, provenance: str, confidence: float | None
    ) -> BeadEvent:
        when = record.when if record else None
        floor = stamp_floors.get(bead)
        if floor is not None and (when is None or instant(when) < instant(floor)):
            when = floor
        evidence = [Evidence("file", path)]
        if record is not None:
            evidence.append(Evidence("commit", record.sha))
        return mint_event(
            "set",
            BeadRef(request.group, bead),
            {"field": EXTERNAL_REF, "value": path},
            occurred_at=when,
            recorded_at=None,
            extracted_at=request.extracted_at,
            source=(SOURCE_SYSTEM, f"{path}#external_ref"),
            provenance=provenance,
            confidence=confidence,
            evidence=evidence,
        )

    for path in paths:
        text = (root / path).read_text(encoding="utf-8", errors="replace")
        linked = refs.get(path)
        if linked:  # bridge 0: the idempotency key, covers every prior match
            report.entries.append(DocEntry(path, DocStatus.IN_SYNC, linked[0], "ref"))
            continue
        record = add_record(path)
        bead: str | None = None
        bridge: str | None = None
        if pattern is not None:
            token = frontmatter_candidate(text, pattern)
            if token is not None:
                bead, bridge = resolve_id(token, live_ids), "frontmatter"
                if bead is None:
                    report.entries.append(
                        DocEntry(
                            path,
                            DocStatus.DANGLING,
                            token,
                            bridge,
                            note="ref names a bead not in corpus",
                        )
                    )
                    continue
            elif record is not None:
                bead = commit_candidate(record.subject, pattern, live_ids)
                bridge = "add-trailer" if bead else None
        if bead is not None:
            current = _text(live[bead], EXTERNAL_REF)
            if current:  # (a bead already pointing at the doc was caught by bridge 0)
                report.entries.append(
                    DocEntry(
                        path,
                        DocStatus.DRIFTED_REF,
                        bead,
                        bridge,
                        note=f"bead points at {current}; not overwritten",
                    )
                )
            else:
                event = stamp(path, bead, record, "derived", None)
                events.append(event)
                report.entries.append(DocEntry(path, DocStatus.NEEDS_STAMP, bead, bridge, events=1))
            continue

        # -- UNMATCHED: the lexical shortlist narrows, the judge (if any) decides ------------
        if shortlist is None:
            shortlist = Shortlist(live)
        candidates = tuple(shortlist.top(query_tokens(path, text), top_k))
        entry = DocEntry(path, DocStatus.UNMATCHED, candidates=candidates)
        report.entries.append(entry)
        if judge is None:
            entry.note = "judge disabled: abstain residual"
            continue
        verdict: Judgment | None = None
        targets: dict[str, str] = {}
        if candidates:
            query, targets = build_query(path, text, candidates)
            key = query_hash(query)
            if key in prior:
                verdict = prior[key].judgment
            else:
                verdict = ask(judge, query)
                report.calls += 1
            entry.verdict = Verdict(FAMILY, key, verdict)
            outcome = verdict.outcome or ABSTAIN
        else:
            outcome, entry.note = NEW_KEY, "no baseline candidate"
        confidence = verdict.confidence if verdict is not None else None
        kind, _, _ = outcome.partition(":")
        if outcome == NEW_KEY:
            entry.outcome = "new"
            if prefix is None:
                raise SourceError(
                    "cannot mint a bead id without an explicit id prefix (prefixes are never "
                    "derived or guessed); pass option id_prefix"
                )
            ident = mint_bead_id(prefix, request.group, f"doc:{path}", taken)
            taken.add(ident)
            made = _new_events(
                request,
                BeadRef(request.group, ident),
                path,
                text,
                record,
                new_type,
                [*NEW_LABELS, *extra_labels],
                confidence,
            )
            events.extend(made)
            entry.bead, entry.events = ident, len(made)
        elif kind == "present":
            target = targets[outcome]
            entry.outcome, entry.match = "present", target
            if _text(live[target], EXTERNAL_REF):
                entry.note = f"bead already points at {_text(live[target], EXTERNAL_REF)}"
            else:
                events.append(stamp(path, target, record, "inferred", confidence))
                entry.events = 1
        elif kind == "noise":
            entry.outcome, entry.covered_by = "noise", targets[outcome]
        else:
            entry.outcome = "abstain"
            entry.note = "judge abstained"
    return DocsProposal(tuple(events), report)


def _new_events(
    request: ExtractRequest,
    bead: BeadRef,
    path: str,
    text: str,
    record: AddRecord | None,
    bead_type: str,
    labels: Iterable[str],
    confidence: float | None,
) -> list[BeadEvent]:
    """A ``create`` for a judged-NEW doc plus its label adds, dated with the doc's first add."""
    when = record.when if record else None
    evidence = [Evidence("file", path)]
    if record is not None:
        evidence.append(Evidence("commit", record.sha))

    def mint(kind: str, data: Mapping[str, Any], ref: str) -> BeadEvent:
        return mint_event(
            kind,
            bead,
            data,
            occurred_at=when,
            recorded_at=None,
            extracted_at=request.extracted_at,
            source=(SOURCE_SYSTEM, f"{path}#{ref}"),
            provenance="inferred",
            confidence=confidence,
            evidence=evidence,
        )

    title = doc_title(text, Path(path).stem)
    made = [
        mint(
            "create",
            {
                "type": bead_type,
                "fields": {
                    "title": title,
                    EXTERNAL_REF: path,
                    "description": f"Reconstructed from {path}.",
                },
            },
            "create",
        )
    ]
    for label in dict.fromkeys(labels):
        made.append(mint("label_add", {"label": label}, f"label/{label}"))
    return made


class DocsStrategy:
    """Source strategy ``docs``: the decision and design docs of ``request.root`` as
    ``external_ref`` links to the beads of ``request.baseline`` (module doc). ``report()``
    describes the last extraction (the ``beadlog extract`` CLI stores it as the archive
    annotation)."""

    name = STRATEGY_NAME
    version = STRATEGY_VERSION

    def __init__(self) -> None:
        self._report: DocsReport | None = None

    def propose(self, request: ExtractRequest) -> DocsProposal:
        options = request.options
        binary = str(options.get("git", "git"))
        if is_git_repo(request.root, binary) and not truthy(options.get("allow_shallow", "false")):
            require_full_history(request.root, binary)
        dirs = csv(options["docs"]) if options.get("docs") else None
        try:
            top_k = int(options.get("top_k", DEFAULT_TOP_K))
        except ValueError as exc:
            raise SourceError(f"option top_k must be an integer: {exc}") from exc
        prior = prior_from_archive(str(options["prior"])) if options.get("prior") else None
        proposal = propose(request, dirs=dirs, top_k=top_k, prior=prior, git=binary)
        self._report = proposal.report
        return proposal

    def extract(self, request: ExtractRequest) -> tuple[BeadEvent, ...]:
        return self.propose(request).events

    def report(self) -> Mapping[str, Any] | None:
        """The last extraction's report as JSON (``None`` before the first extraction)."""
        return None if self._report is None else self._report.to_json()


__all__ = [
    "ANNOTATION_KEY",
    "DEFAULT_DIRS",
    "DEFAULT_TOP_K",
    "ENGINE_CLASS",
    "FAMILY",
    "STRATEGY_NAME",
    "STRATEGY_VERSION",
    "Candidate",
    "DocEntry",
    "DocStatus",
    "DocsProposal",
    "DocsReport",
    "DocsStrategy",
    "Shortlist",
    "build_query",
    "commit_candidate",
    "discover_docs",
    "frontmatter_candidate",
    "propose",
    "prior_from_archive",
    "query_tokens",
    "resolve_id",
]
