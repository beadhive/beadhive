"""The ``commits``, ``docs`` and ``planning`` hive passes through the engine and a fold store.

The git history is a throwaway ``git init`` repository per test; the store is a list of events
folded on every export, so what is asserted is the pass and the envelope (DRIFT and NEW applied,
the second dry run empty, a pinned ``rev``, judge off by default), not the bd mapping (the root
suite proves that against the bd StoreLoader). No clock is read: the extractor clock is a
constant and the judge is a scripted fake.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

import beadlog
from beadhive_backfill import (
    SOURCE_PASS_NAMES,
    BackfillEngine,
    BackfillError,
    JsonFileAuditSink,
    Plan,
    source_pass,
    source_pass_for_plan,
)
from beadhive_backfill.source_passes import parse_options
from beadlog_sources import ChoiceQuery, Judgment, ScoreQuery, baseline_events

GROUP = beadlog.Group("urn:uuid:00000000-0000-4000-8000-0000000000dd", "wd")
STAMP = "2026-11-01T00:00:00Z"


def _clock() -> str:
    return STAMP


def _row(iid: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": iid,
        "issue_type": "task",
        "status": "open",
        "title": f"Bead {iid}",
        "priority": 2,
        "labels": [],
        "created_at": "2026-08-10T09:00:00Z",
        "updated_at": "2026-08-20T00:00:00Z",
        **extra,
    }


class FoldStore:
    """A store that is a list of events: the reader, loader and pre-state port of the engine."""

    name = "fake"
    version = "0"
    capabilities = frozenset({"valid-time"})

    def __init__(self, rows: Iterable[Mapping[str, Any]]) -> None:
        self.events = list(baseline_events(rows, group=GROUP.id, extracted_at=STAMP))
        self.exported = False

    def export(self) -> tuple[beadlog.BeadEvent, ...]:
        return tuple(self.events)

    def export_pre_state(self, baseline: Sequence[beadlog.BeadEvent]) -> str:
        self.exported = True
        return "pre-state"

    def load(self, events: Sequence[beadlog.BeadEvent], *, dry_run: bool) -> Mapping[str, Any]:
        if not dry_run:
            assert self.exported, "the pre-state must exist before the first write"
            self.events.extend(events)
        return {"summary": {"by_code": {}}, "losses": []}

    def field(self, iid: str, name: str) -> Any:
        return beadlog.fold(self.events)[beadlog.BeadRef(GROUP.id, iid)].fields.get(name)


class ListSink:
    def __init__(self) -> None:
        self.entries: list[Any] = []

    def record(self, entry: Any) -> None:
        self.entries.append(entry)


class NewJudge:
    """Judges every doc a new bead and counts its calls."""

    engine = "scripted"
    engine_version = "1"
    model = None

    def __init__(self) -> None:
        self.calls = 0

    def choose(self, query: ChoiceQuery) -> Judgment:
        self.calls += 1
        return Judgment("new", {"new": 1.0}, 0.9, len(query.options), "scripted", "scripted", "1")

    def score(self, query: ScoreQuery) -> Judgment:  # pragma: no cover - never asked
        raise AssertionError("docs asks choices only")


# -- a throwaway repository -----------------------------------------------------------------


def _git(repo: Path, *args: str, when: str = "2026-09-01T10:00:00+00:00") -> str:
    env = {**os.environ, "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
    done = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env
    )
    return done.stdout.strip()


def _commit(repo: Path, message: str, files: Mapping[str, str] | None = None) -> str:
    for name, text in (files or {}).items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    for command in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@example.com"],
        ["config", "user.name", "T"],
        ["config", "commit.gpgsign", "false"],
    ):
        _git(root, *command)
    return root


def _nonzero(plan: Plan) -> dict[str, int]:
    return {key: value for key, value in plan.totals["by_class"].items() if value}


def _apply(engine: BackfillEngine, plan: Plan, store: FoldStore, sink: ListSink | None = None):
    return engine.apply(plan, store, store, sink or ListSink(), pre_state=store)


# -- the registry and the options -------------------------------------------------------------


def test_the_three_passes_are_registered_drift_applicable_and_off_by_default(
    tmp_path: Path,
) -> None:
    assert SOURCE_PASS_NAMES == ("commits", "docs", "planning")
    for name in ("docs", "planning"):
        pass_ = source_pass(name, tmp_path, GROUP.id)
        assert pass_.name == name and pass_.drift_applicable is True
        assert pass_.judging is False and pass_.options == {}
    assert source_pass("docs", tmp_path, GROUP.id, judging=True).invalidating_fields > (
        source_pass("docs", tmp_path, GROUP.id).invalidating_fields
    )


def test_unknown_passes_and_options_are_refused(tmp_path: Path) -> None:
    with pytest.raises(BackfillError, match="unknown source pass"):
        source_pass("nope", tmp_path, GROUP.id)
    with pytest.raises(BackfillError, match=r"unknown option\(s\) rev"):
        source_pass("docs", tmp_path, GROUP.id, options={"rev": "HEAD"})
    with pytest.raises(BackfillError, match="not key=value"):
        parse_options(["labels"])
    assert parse_options(["labels=complexity:MEDIUM", "since=a=b"]) == {
        "labels": "complexity:MEDIUM",
        "since": "a=b",
    }


def test_commits_pins_rev_to_a_sha_and_refuses_an_unknown_rev(repo: Path) -> None:
    sha = _commit(repo, "feat: first (wd-1)")
    assert source_pass("commits", repo, GROUP.id).options["rev"] == sha
    assert source_pass("commits", repo, GROUP.id, options={"rev": "main"}).options["rev"] == sha
    with pytest.raises(BackfillError, match="does not name a commit"):
        source_pass("commits", repo, GROUP.id, options={"rev": "no-such-ref"})


# -- commits: DRIFT appended and applied --------------------------------------------------------


def test_commits_appends_shas_as_drift_through_the_envelope_then_converges(repo: Path) -> None:
    first = _commit(repo, "feat(x): start (wd-1)")
    second = _commit(repo, "fix(x): follow-up\n\nRefs: wd-1, wd-2")
    store = FoldStore([_row("wd-1", metadata={"git.commits": json.dumps([first])}), _row("wd-2")])
    pass_ = source_pass("commits", repo, GROUP.id)
    engine = BackfillEngine(groups=[GROUP], passes=[pass_], clock=_clock)

    plan = engine.plan(pass_, store)

    assert plan.pass_name == "commits" and plan.changes == 2
    assert plan.totals["by_class"].get("DRIFT") == 2  # both beads exist: appends are DRIFT
    assert plan.drift_reported() == ()  # ... and the pass applies them
    assert plan.totals["drift_reported"] == 0
    assert plan.report["options"]["rev"] == second and plan.report["judging"] is False

    sink = ListSink()
    entry = _apply(engine, plan, store, sink)

    assert entry.state.value == "applied" and sink.entries[0].state.value == "applying"
    assert entry.verification["second_dry_run_changes"] == 0
    assert json.loads(store.field("wd-1", "metadata.git.commits")) == [first, second]
    assert json.loads(store.field("wd-2", "metadata.git.commits")) == [second]
    assert engine.plan(pass_, store).changes == 0  # idempotent: nothing left to plan


def test_apply_reads_the_planned_history_even_when_the_branch_moved(repo: Path) -> None:
    _commit(repo, "feat: one (wd-1)")
    store = FoldStore([_row("wd-1")])
    pass_ = source_pass("commits", repo, GROUP.id)
    plan = BackfillEngine(groups=[GROUP], passes=[pass_], clock=_clock).plan(pass_, store)

    _commit(repo, "feat: two (wd-1)")  # the branch moves after the plan was made

    applier = source_pass_for_plan("commits", repo, GROUP.id, plan.report)
    engine = BackfillEngine(groups=[GROUP], passes=[applier], clock=_clock)
    entry = _apply(engine, plan, store)  # the second dry run reads the pinned rev: still empty

    assert entry.verification["second_dry_run_changes"] == 0
    assert len(json.loads(store.field("wd-1", "metadata.git.commits"))) == 1


def test_a_corpus_change_after_planning_is_refused_before_any_write(repo: Path) -> None:
    _commit(repo, "feat: one (wd-1)")
    store = FoldStore([_row("wd-1")])
    pass_ = source_pass("commits", repo, GROUP.id)
    engine = BackfillEngine(groups=[GROUP], passes=[pass_], clock=_clock)
    plan = engine.plan(pass_, store)
    before = len(store.events)

    store.events.extend(  # someone else stamps the same field after the plan
        [
            beadlog.mint_event(
                "set",
                beadlog.BeadRef(GROUP.id, "wd-1"),
                {"field": "metadata.git.commits", "value": json.dumps(["f" * 40])},
                occurred_at=STAMP,
                recorded_at=STAMP,
                extracted_at=STAMP,
                source=("bd", "elsewhere"),
                provenance="observed",
            )
        ]
    )
    with pytest.raises(BackfillError, match="corpus changed after planning"):
        _apply(engine, plan, store)
    assert len(store.events) == before + 1 and not store.exported


# -- docs: stamps are DRIFT, a judged NEW is a create, residuals are printed ------------------


DOC_A = "docs/design/a-stamped.md"
DOC_B = "docs/design/b-unknown.md"


def _docs_repo(repo: Path) -> Path:
    _commit(
        repo,
        "docs: add design docs",
        {
            DOC_A: "# Stamped\n\nBeads: wd-1\n",
            DOC_B: "# Something entirely unrelated\n\nNo bead here.\n",
        },
    )
    return repo


def test_docs_stamps_external_ref_and_leaves_the_unmatched_doc_a_residual(repo: Path) -> None:
    _docs_repo(repo)
    store = FoldStore([_row("wd-1"), _row("wd-9", title="Other work")])
    pass_ = source_pass("docs", repo, GROUP.id)
    engine = BackfillEngine(groups=[GROUP], passes=[pass_], clock=_clock)

    plan = engine.plan(pass_, store)

    assert plan.changes == 1 and plan.totals["by_class"].get("DRIFT") == 1
    assert plan.report["residual"] == [DOC_B]  # judge off: an abstain residual for a human
    assert plan.report["judging"] is False and plan.totals["judge_calls"] == 0

    entry = _apply(engine, plan, store)

    assert entry.verification["second_dry_run_changes"] == 0
    assert store.field("wd-1", "external_ref") == DOC_A
    assert store.field("wd-9", "external_ref") is None
    assert engine.plan(pass_, store).changes == 0
    assert engine.plan(pass_, store).report["residual"] == [DOC_B]  # still undecided


def test_docs_with_a_judge_creates_a_new_bead_and_a_second_run_proposes_nothing(
    repo: Path,
) -> None:
    _docs_repo(repo)
    store = FoldStore([_row("wd-1"), _row("wd-9", title="Entirely different matter")])
    pass_ = source_pass("docs", repo, GROUP.id, options={"labels": "team:core"}, judging=True)
    engine = BackfillEngine(groups=[GROUP], passes=[pass_], clock=_clock)
    judge = NewJudge()

    plan = engine.plan(pass_, store, judge)

    asked = judge.calls  # the docs pass's question, and reconcile's on the created bead
    assert asked >= 1 and plan.report["residual"] == []
    assert plan.totals["by_class"].get("NEW", 0) >= 1 and plan.changes == 2  # stamp + created bead

    entry = _apply(engine, plan, store)

    assert judge.calls == asked  # the apply and its proof use recorded verdicts, never the judge
    assert entry.verification["unrecorded_judgments"] == 0
    refs = {
        ref.id: state.fields.get("external_ref")
        for ref, state in beadlog.fold(store.events).items()
    }
    assert refs["wd-1"] == DOC_A and DOC_B in refs.values()
    created = next(
        state
        for ref, state in beadlog.fold(store.events).items()
        if state.fields.get("external_ref") == DOC_B
    )
    assert {"origin:backfill", "source:docs", "team:core"} <= set(created.labels)
    assert engine.plan(pass_, store, judge).changes == 0


def test_a_plan_made_with_a_judge_is_refused_by_a_pass_built_without_one(repo: Path) -> None:
    _docs_repo(repo)
    store = FoldStore([_row("wd-1")])
    judged = source_pass("docs", repo, GROUP.id, judging=True)
    plan = BackfillEngine(groups=[GROUP], passes=[judged], clock=_clock).plan(
        judged, store, NewJudge()
    )
    plain = BackfillEngine(groups=[GROUP], passes=[source_pass("docs", repo, GROUP.id)])
    with pytest.raises(BackfillError, match="pass settings differ"):
        _apply(plain, plan, store)


# -- planning: create, edges, closes; a later close is DRIFT ------------------------------------

P1 = ".planning/phases/01-foundation"
TREE = {
    f"{P1}/01-01-PLAN.md": "---\ndepends_on: []\n---\n# Foundation: set up\n",
    f"{P1}/01-02-PLAN.md": "---\ndepends_on:\n  - 01-01\n---\n# Foundation: wire CI\n",
}


def test_planning_creates_beads_then_applies_a_later_close_as_drift(repo: Path) -> None:
    _commit(repo, "docs: add the planning tree", TREE)
    store = FoldStore([_row("wd-1")])
    pass_ = source_pass("planning", repo, GROUP.id, options={"labels": "complexity:MEDIUM"})
    engine = BackfillEngine(groups=[GROUP], passes=[pass_], clock=_clock)

    plan = engine.plan(pass_, store)
    assert plan.totals["by_class"].get("NEW", 0) == 3  # one phase epic, two plans
    entry = _apply(engine, plan, store)
    assert entry.verification["second_dry_run_changes"] == 0
    assert len(beadlog.fold(store.events)) == 4
    assert engine.plan(pass_, store).changes == 0

    # a summary appears later: the close of an existing bead is DRIFT, and it is applied
    _commit(repo, "docs: summarize 01-01", {f"{P1}/01-01-SUMMARY.md": "# done\n"})
    closing = engine.plan(pass_, store)
    assert closing.changes == 1 and _nonzero(closing) == {"DRIFT": 1}
    assert closing.drift_reported() == ()
    _apply(engine, closing, store)
    closed = [s for s in beadlog.fold(store.events).values() if s.phase == "closed"]
    assert len(closed) == 1
    assert engine.plan(pass_, store).changes == 0


def test_a_plan_is_refused_by_a_different_pass_and_writes_a_file_round_trip(
    repo: Path, tmp_path: Path
) -> None:
    _commit(repo, "feat: one (wd-1)")
    store = FoldStore([_row("wd-1")])
    pass_ = source_pass("commits", repo, GROUP.id)
    engine = BackfillEngine(groups=[GROUP], passes=[pass_], clock=_clock)
    plan = engine.plan(pass_, store)
    path = tmp_path / "commits.plan.bdlg"
    plan.write(path)

    reread = Plan.read(path)
    assert reread.plan_hash == plan.plan_hash and reread.report["options"] == pass_.options
    other = BackfillEngine(groups=[GROUP], passes=[source_pass("planning", repo, GROUP.id)])
    with pytest.raises(BackfillError, match="plan was made by pass 'commits'"):
        other.apply(
            reread,
            store,
            store,
            JsonFileAuditSink(tmp_path / "a.json"),
            pre_state=store,
            pass_=source_pass("planning", repo, GROUP.id),
        )
