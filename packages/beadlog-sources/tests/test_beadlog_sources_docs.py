"""The neutral ``docs`` strategy: bridges 0/1/2, the lexical shortlist, the judged residual.

The git history is real (a throwaway ``git init`` repo); the baseline is a BeadLog of plain
``create`` events, as ``beadlog extract bd`` would produce. The golden cases are the fixtures of
the skill's ``scripts/test-backfill.sh``, copied here (nothing at test time reads the skills repo).
No assertion depends on elapsed time: the large corpus asserts that every doc was processed, how
many judge questions were asked and how many git commands were run.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

import beadlog
from beadlog import BeadEvent, BeadRef, fold, mint_event
from beadlog_sources import (
    ABSTAIN,
    ChoiceQuery,
    DeterministicJudge,
    DocsStrategy,
    ExtractRequest,
    Judgment,
    reconcile,
)
from beadlog_sources import _sources as sources
from beadlog_sources.cli import discover_strategies, main
from beadlog_sources.docs import (
    FAMILY,
    Shortlist,
    discover_docs,
    frontmatter_candidate,
    propose,
    query_tokens,
    resolve_id,
)

GROUP = "urn:uuid:00000000-0000-4000-8000-0000000000d1"
EXTRACTED = "2026-10-31T00:00:00Z"
DOC = "docs/design/001-example.md"


# -- fixtures ------------------------------------------------------------------------------


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, **(env or {})},
    )
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test User")
    _git(root, "config", "commit.gpgsign", "false")
    return root


_CLOCK = {"n": 0}


def _write(repo: Path, path: str, text: str) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _commit(repo: Path, subject: str) -> str:
    _CLOCK["n"] += 1
    when = f"2026-09-01T10:{_CLOCK['n'] // 60 % 60:02d}:{_CLOCK['n'] % 60:02d}+00:00"
    _git(repo, "add", "-A")
    _git(
        repo,
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        subject,
        env={"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when},
    )
    return _git(repo, "rev-parse", "HEAD")


def _create(bead: str, title: str, **fields: Any) -> BeadEvent:
    at = "2026-08-01T00:00:00Z"
    return mint_event(
        "create",
        BeadRef(GROUP, bead),
        {"type": fields.pop("type", "task"), "fields": {"title": title, **fields}},
        occurred_at=at,
        recorded_at=at,
        extracted_at=EXTRACTED,
        source=("bd", f"issues/{bead}#create"),
        provenance="observed",
    )


def _request(
    repo: Path, baseline: Sequence[BeadEvent], judge: Any = None, **options: str
) -> ExtractRequest:
    return ExtractRequest(
        root=repo,
        group=GROUP,
        extracted_at=EXTRACTED,
        baseline=tuple(baseline),
        options=options,
        judge=judge,
    )


def _statuses(strategy: DocsStrategy) -> dict[str, str]:
    report = strategy.report()
    assert report is not None
    return {entry["doc"]: entry["status"] for entry in report["entries"]}


def _extract(repo: Path, baseline: Sequence[BeadEvent], judge: Any = None, **options: str):
    strategy = DocsStrategy()
    events = strategy.extract(_request(repo, baseline, judge, **options))
    return strategy, events


class FakeJudge:
    """Answers each choice from a script keyed by the doc's first context line (its title)."""

    engine = "fake"
    engine_version = "1"
    model = "fake-model"

    def __init__(self, answers: dict[str, tuple[str | None, float]] | None = None) -> None:
        self.answers = answers or {}
        self.queries: list[ChoiceQuery] = []

    def choose(self, query: ChoiceQuery) -> Judgment:
        self.queries.append(query)
        subject = query.context.split("\n", 1)[0]
        outcome, confidence = self.answers.get(subject, (None, 0.0))
        count = len(query.options)
        return Judgment(
            outcome,
            {outcome or ABSTAIN: 1.0},
            confidence,
            count,
            f"fake verdict for {subject}",
            self.engine,
            self.engine_version,
            self.model,
        )

    def score(self, query: Any) -> Judgment:  # pragma: no cover - never asked
        raise AssertionError("docs never scores")


# -- golden parity with scripts/test-backfill.sh -------------------------------------------


def _golden_repo(repo: Path) -> Path:
    _write(repo, DOC, "---\n- Beads: -looks-like-an-option\n---\n")
    return repo


@pytest.mark.parametrize(
    "baseline", [(), (_create("bh-other", "Other"),)], ids=["missing-corpus", "other-corpus"]
)
def test_golden_unmatched_without_errors(repo: Path, baseline: Sequence[BeadEvent]) -> None:
    """A back-ref that looks like an option names no bead: UNMATCHED, no crash (missing, empty
    and populated corpora alike). With no judge the doc is an abstain residual."""
    _golden_repo(repo)
    strategy, events = _extract(repo, baseline)
    assert events == ()
    assert _statuses(strategy) == {DOC: "UNMATCHED"}
    report = strategy.report()
    assert report is not None and report["residual"] == [DOC]


def test_golden_frontmatter_bridge_needs_stamp(repo: Path) -> None:
    _write(repo, DOC, "---\n- Beads: bh-example\n---\n")
    strategy, events = _extract(repo, [_create("bh-example", "Example")])
    assert _statuses(strategy) == {DOC: "PRESENT-needs-stamp"}
    report = strategy.report()
    assert report is not None
    entry = report["entries"][0]
    assert (entry["bead"], entry["bridge"], entry["class"]) == (
        "bh-example",
        "frontmatter",
        "DRIFT",
    )
    (event,) = events
    assert event.kind == "set" and event.bead.id == "bh-example"
    assert dict(event.data) == {"field": "external_ref", "value": DOC}


# -- bridges 0, 1 and 2 --------------------------------------------------------------------


def test_every_status_of_the_script(repo: Path) -> None:
    _write(repo, "docs/decisions/f-trailer.md", "# Added by a commit that names a bead\n")
    _commit(repo, "docs(bh-ddd): add the trailer doc")
    _write(repo, "docs/design/a-sync.md", "# Synced\n")
    _write(repo, "docs/design/b-stamp.md", "Beads: bh-bbb (epic), bh-bbb.5\n")
    _write(repo, "docs/design/c-drift.md", "- Bead: bh-ccc\n")
    _write(repo, "docs/design/d-dangling.md", "Beads: bh-nope\n")
    _write(repo, "docs/design/e-none.md", "# Nothing links here at all\n")
    _commit(repo, "docs: add everything")
    baseline = [
        _create("bh-aaa", "A", external_ref="docs/design/a-sync.md"),
        _create("bh-bbb", "B"),
        _create("bh-bbb.5", "B five"),
        _create("bh-ccc", "C", external_ref="docs/design/elsewhere.md"),
        _create("bh-ddd", "D"),
    ]
    strategy, events = _extract(repo, baseline)
    assert _statuses(strategy) == {
        "docs/design/a-sync.md": "PRESENT-in-sync",
        "docs/design/b-stamp.md": "PRESENT-needs-stamp",
        "docs/design/c-drift.md": "DRIFTED-ref",
        "docs/design/d-dangling.md": "DANGLING",
        "docs/design/e-none.md": "UNMATCHED",
        "docs/decisions/f-trailer.md": "PRESENT-needs-stamp",
    }
    # the last id on the Beads: line wins, and the .5 child is its own bead
    assert sorted((e.bead.id, e.data["value"]) for e in events) == [
        ("bh-bbb.5", "docs/design/b-stamp.md"),
        ("bh-ddd", "docs/decisions/f-trailer.md"),
    ]
    report = strategy.report()
    assert report is not None
    assert {d["bead"] for d in report["drift"]} == {"bh-ccc", "bh-nope"}
    assert report["docs"] == 6 and report["proposed"] == 2


def test_the_add_trailer_bridge_reads_the_commit_that_first_added_the_doc(repo: Path) -> None:
    _write(repo, "docs/design/x.md", "# X\n")
    sha = _commit(repo, "docs(bh-100): add x")
    _write(repo, "docs/design/x.md", "# X again\n")
    _commit(repo, "fix(bh-200): reword x")
    strategy, events = _extract(repo, [_create("bh-100", "One"), _create("bh-200", "Two")])
    (event,) = events
    assert event.bead.id == "bh-100"
    added = _git(repo, "log", "-1", "--format=%cI", sha)
    assert event.occurred_at == sources.commit_time(added)  # the add commit, not the reword
    assert any(item.type == "commit" and item.ref == sha for item in event.source.evidence)
    report = strategy.report()
    assert report is not None and report["entries"][0]["bridge"] == "add-trailer"


def test_a_commit_subject_naming_no_bead_does_not_hide_the_doc(repo: Path) -> None:
    """`bh-infra` is id-shaped but no bead: the doc stays UNMATCHED for the shortlist (the
    script would have reported it DANGLING)."""
    _write(repo, "docs/design/x.md", "# X\n")
    _commit(repo, "docs(bh-infra): add x")
    strategy, events = _extract(repo, [_create("bh-100", "One")])
    assert events == () and _statuses(strategy) == {"docs/design/x.md": "UNMATCHED"}


def test_a_renamed_doc_still_finds_its_original_add_commit(repo: Path) -> None:
    _write(repo, "docs/design/old-name.md", "a long enough body to be a rename target\n" * 5)
    _commit(repo, "docs(bh-100): add the doc")
    _git(repo, "mv", "docs/design/old-name.md", "docs/design/new-name.md")
    _commit(repo, "docs: rename the doc")
    strategy, events = _extract(repo, [_create("bh-100", "One")])
    assert [e.bead.id for e in events] == ["bh-100"]
    assert _statuses(strategy) == {"docs/design/new-name.md": "PRESENT-needs-stamp"}


def test_a_non_git_tree_still_bridges_by_frontmatter(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    _write(root, DOC, "Beads: bh-100\n")
    strategy, events = _extract(root, [_create("bh-100", "One")])
    (event,) = events
    assert event.occurred_at == "2026-08-01T00:00:00Z"  # undated: raised to the bead's create
    assert _statuses(strategy) == {DOC: "PRESENT-needs-stamp"}


def test_the_doc_set_is_widened_by_the_docs_option(repo: Path) -> None:
    _write(repo, "docs/design/a.md", "# A\n")
    _write(repo, "docs/design/sub/deeper.md", "# not in the default set\n")
    _write(repo, "notes/deep/b.md", "Beads: bh-100\n")
    _write(repo, "notes/readme.txt", "ignored")
    assert discover_docs(repo) == ["docs/design/a.md"]
    assert discover_docs(repo, ["notes"]) == ["notes/deep/b.md"]
    strategy, _ = _extract(repo, [_create("bh-100", "One")], docs="notes,docs/design")
    assert set(_statuses(strategy)) == {
        "notes/deep/b.md",
        "docs/design/a.md",
        "docs/design/sub/deeper.md",
    }


def test_resolve_id_trims_hyphen_segments_only() -> None:
    live = {"bh-example", "bh-example.5", "bh-baml-8u9.17"}
    assert resolve_id("bh-example-foo", live) == "bh-example"
    assert resolve_id("bh-example.5", live) == "bh-example.5"
    assert resolve_id("bh-example.6", live) is None  # a child suffix is never trimmed
    assert resolve_id("bh-baml-8u9.17", live) == "bh-baml-8u9.17"
    assert resolve_id("bh-nope", live) is None


def test_only_the_first_beads_line_is_read() -> None:
    import re

    pattern = re.compile(r"(?<![0-9A-Za-z_-])bh-[0-9a-z]+(?:[.-][0-9a-z]+)*", re.IGNORECASE)
    assert frontmatter_candidate("x\nBeads: none yet\nBeads: bh-1\n", pattern) is None
    assert frontmatter_candidate("  - beads: bh-1, bh-2.3\n", pattern) == "bh-2.3"


# -- idempotency ---------------------------------------------------------------------------


def test_a_full_apply_converges_to_in_sync(repo: Path) -> None:
    _write(repo, "docs/design/a.md", "Beads: bh-100\n")
    _write(repo, "docs/design/b.md", "Beads: bh-200\n")
    _commit(repo, "docs: add")
    baseline = [_create("bh-100", "One"), _create("bh-200", "Two")]
    _, events = _extract(repo, baseline)
    assert len(events) == 2
    strategy, again = _extract(repo, [*baseline, *events])
    assert again == ()
    assert set(_statuses(strategy).values()) == {"PRESENT-in-sync"}


def test_reconcile_classes_a_stamp_as_drift_then_proposes_nothing(repo: Path) -> None:
    _write(repo, DOC, "Beads: bh-100\n")
    baseline = [_create("bh-100", "One")]
    _, events = _extract(repo, baseline)
    plan = reconcile(events, baseline)
    assert plan.counts()["DRIFT"] == 1 and len(plan.events) == 1
    assert reconcile(events, [*baseline, *events]).empty


# -- the lexical shortlist and the judged residual -----------------------------------------


def test_the_shortlist_ranks_title_over_description_and_is_deterministic() -> None:
    folded = fold(
        [
            _create("bh-1", "Billing retries", description="payment gateway"),
            _create("bh-2", "Gateway timeouts", description="payment retries billing"),
            _create("bh-3", "Unrelated thing"),
            _create("bh-4", "Gate", type="gate"),  # gates are never candidates
        ]
    )
    live = {ref.id: state for ref, state in folded.items()}
    shortlist = Shortlist(live)
    assert len(shortlist) == 3
    query = query_tokens("docs/design/009-billing-retries.md", "# Retries for billing\n")
    top = shortlist.top(query, 2)
    assert [c.bead for c in top] == ["bh-1", "bh-2"]
    assert top[0].score > top[1].score
    assert shortlist.top(query, 1) == top[:1]


def _judged_repo(repo: Path) -> list[BeadEvent]:
    _write(repo, "docs/design/010-billing-retries.md", "# Billing retries design\n")
    _write(repo, "docs/design/020-auth-rework.md", "# Auth rework\n")
    _write(repo, "docs/design/030-search-index.md", "# Search index rebuild\n")
    _write(repo, "docs/design/040-zzzz-yyyy.md", "# Totally novel\n")
    _write(repo, "docs/design/050-gateway.md", "# Gateway epic coverage\n")
    _commit(repo, "docs: add design docs")
    return [
        _create("bh-10", "Billing retries"),
        _create("bh-20", "Auth rework", external_ref="docs/design/elsewhere.md"),
        _create("bh-30", "Search index rebuild"),
        _create("bh-50", "Gateway programme", type="epic", description="gateway epic coverage"),
    ]


def test_with_the_judge_disabled_every_unmatched_doc_is_an_abstain_residual(repo: Path) -> None:
    baseline = _judged_repo(repo)
    strategy, events = _extract(repo, baseline, judge=None)
    assert events == ()
    report = strategy.report()
    assert report is not None
    assert report["counts"]["UNMATCHED"] == 5 == len(report["residual"])
    assert report["judge_calls"] == 0 and report["judge"]["engine"] is None
    assert all(
        "verdict" not in entry and entry["review"] == "required" for entry in report["entries"]
    )
    # the shortlist is still recorded, to help the human decide
    first = report["entries"][0]
    assert first["candidates"][0]["bead"] == "bh-10"


def test_a_fake_judge_verdicts_and_confidences_are_recorded(repo: Path) -> None:
    baseline = _judged_repo(repo)
    judge = FakeJudge(
        {
            "Billing retries design": ("present:1", 0.9),
            "Auth rework": ("present:1", 0.8),  # that bead already points at another doc
            "Search index rebuild": (None, 0.0),  # the judge abstains
            "Totally novel": ("new", 0.7),  # no candidate: NEW without a question
            "Gateway epic coverage": ("noise:1", 0.6),
        }
    )
    strategy, events = _extract(repo, baseline, judge=judge, labels="repo:x,org:y")
    report = strategy.report()
    assert report is not None
    entries = {entry["doc"].split("/")[-1]: entry for entry in report["entries"]}

    billing = entries["010-billing-retries.md"]
    assert (billing["status"], billing["class"], billing["outcome"]) == (
        "UNMATCHED",
        "PRESENT",
        "present",
    )
    assert billing["match"] == "bh-10"
    verdict = billing["verdict"]
    assert verdict["family"] == FAMILY and verdict["outcome"] == "present:1"
    assert verdict["confidence"] == 0.9 and verdict["engine"] == "fake"
    assert verdict["model"] == "fake-model" and verdict["query_hash"].startswith("sha256:")
    assert verdict["option_count"] == len(judge.queries[0].options)

    assert entries["020-auth-rework.md"]["note"].startswith("bead already points at")
    assert entries["020-auth-rework.md"]["events"] == 0
    assert entries["030-search-index.md"]["outcome"] == "abstain"
    assert entries["040-zzzz-yyyy.md"]["note"] == "no baseline candidate"
    assert (
        entries["040-zzzz-yyyy.md"]["outcome"] == "new"
        and "verdict" not in entries["040-zzzz-yyyy.md"]
    )
    gateway = entries["050-gateway.md"]
    assert (gateway["class"], gateway["covered_by"]) == ("NOISE", "bh-50")
    assert report["residual"] == ["docs/design/030-search-index.md"]
    assert report["judge"] == {"engine": "fake", "version": "1", "model": "fake-model"}

    # four questions were asked (the doc without any candidate was not one of them)
    assert report["judge_calls"] == len(judge.queries) == 4
    kinds = sorted((e.kind, e.bead.id) for e in events)
    new_id = entries["040-zzzz-yyyy.md"]["bead"]
    assert kinds == sorted(
        [
            ("set", "bh-10"),
            ("create", new_id),
            ("label_add", new_id),
            ("label_add", new_id),
            ("label_add", new_id),
            ("label_add", new_id),
        ]
    )
    created = next(e for e in events if e.kind == "create")
    assert created.provenance == "inferred"
    assert dict(created.data["fields"])["external_ref"] == "docs/design/040-zzzz-yyyy.md"
    labels = {str(e.data["label"]) for e in events if e.kind == "label_add"}
    assert labels == {"origin:backfill", "source:docs", "repo:x", "org:y"}
    stamp = next(e for e in events if e.kind == "set")
    assert stamp.provenance == "inferred" and stamp.confidence == 0.9
    assert new_id.startswith("bh-") and new_id not in {"bh-10", "bh-20", "bh-30", "bh-50"}


def test_judged_stamps_and_creates_converge(repo: Path) -> None:
    baseline = _judged_repo(repo)
    judge = FakeJudge({"Billing retries design": ("present:1", 0.9), "Totally novel": ("new", 0.7)})
    _, events = _extract(repo, baseline, judge=judge)
    after = [*baseline, *events]
    strategy, again = _extract(repo, after, judge=FakeJudge())
    assert again == ()
    statuses = _statuses(strategy)
    assert statuses["docs/design/010-billing-retries.md"] == "PRESENT-in-sync"
    assert statuses["docs/design/040-zzzz-yyyy.md"] == "PRESENT-in-sync"


def test_a_recorded_verdict_from_a_prior_extract_is_never_asked_again(
    repo: Path, tmp_path: Path
) -> None:
    baseline = _judged_repo(repo)
    judge = FakeJudge({"Search index rebuild": (None, 0.0)})
    strategy, events = _extract(repo, baseline, judge=judge)
    report = strategy.report()
    assert report is not None and report["judge_calls"] == 4
    prior = tmp_path / "prior.bdlg"
    beadlog.write_archive(
        prior,
        events,
        groups=[beadlog.Group(GROUP, "bh")],
        generator=beadlog.Generator("t", "0"),
        annotations={"bdlg.docs": report},
    )
    again = FakeJudge()
    strategy2, _ = _extract(repo, baseline, judge=again, prior=str(prior))
    report2 = strategy2.report()
    assert report2 is not None
    assert again.queries == [] and report2["judge_calls"] == 0
    assert [e["verdict"]["query_hash"] for e in report2["entries"] if "verdict" in e] == [
        e["verdict"]["query_hash"] for e in report["entries"] if "verdict" in e
    ]


def test_a_judge_outcome_that_was_not_offered_is_refused(repo: Path) -> None:
    baseline = _judged_repo(repo)
    judge = FakeJudge({"Billing retries design": ("present:9", 0.99)})
    strategy, events = _extract(repo, baseline, judge=judge)
    report = strategy.report()
    assert report is not None
    entry = next(e for e in report["entries"] if e["doc"].endswith("010-billing-retries.md"))
    assert entry["outcome"] == "abstain" and entry["verdict"]["outcome"] is None
    assert entry["verdict"]["confidence"] == 0.0
    assert not any(e.kind == "set" and e.bead.id == "bh-10" for e in events)


def test_the_deterministic_judge_is_a_judge_when_handed_over(repo: Path) -> None:
    baseline = _judged_repo(repo)
    strategy, events = _extract(repo, baseline, judge=DeterministicJudge())
    report = strategy.report()
    assert report is not None and report["judge"]["engine"] == "deterministic-lexical"
    # "Search index rebuild" is the bead's exact title; the fuzzier titles stay residual
    assert [(e.kind, e.bead.id) for e in events if e.kind == "set"] == [("set", "bh-30")]
    assert report["judge_calls"] == 4


def test_a_judged_new_with_no_bead_to_read_a_prefix_from_asks_for_one(repo: Path) -> None:
    _write(repo, DOC, "# Anything\n")
    with pytest.raises(ValueError, match="id_prefix"):
        _extract(repo, [], judge=FakeJudge())
    _, events = _extract(repo, [], judge=FakeJudge(), id_prefix="zz")
    assert {e.bead.id.split("-")[0] for e in events} == {"zz"}


# -- the large corpus ----------------------------------------------------------------------


def test_a_large_corpus_processes_every_doc_without_per_doc_store_or_git_calls(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """bh-skls-svo: the script stopped at 37 of 120 docs. 5000 beads that all match every doc
    give every doc a full-corpus shortlist; each must still be processed and costs no git or store
    call of its own."""
    docs, beads = 120, 5000
    for number in range(1, docs + 1):
        _write(repo, f"docs/design/{number:03d}-widget-gadget-{number}.md", "---\ntitle: x\n---\n")
    _commit(repo, "docs: add the corpus")
    baseline = [_create(f"bh-b{n:05d}", f"Widget gadget corpus bead {n}") for n in range(beads)]

    git_calls = {"n": 0}
    real = sources._run

    def counting(root: Any, args: Sequence[str], git: str) -> Any:
        git_calls["n"] += 1
        return real(root, args, git)

    monkeypatch.setattr(sources, "_run", counting)

    strategy, events = _extract(repo, baseline)
    report = strategy.report()
    assert report is not None
    assert events == () and report["docs"] == docs and report["beads"] == beads
    assert report["counts"]["UNMATCHED"] == docs == len(report["residual"])
    assert all(len(entry["candidates"]) == 3 for entry in report["entries"])
    assert git_calls["n"] <= 6, "git was read per doc"  # is-repo, shallow, one batched log, ...

    judge = FakeJudge()
    strategy, _ = _extract(repo, baseline, judge=judge)
    report = strategy.report()
    assert report is not None
    assert len(judge.queries) == docs == report["judge_calls"]  # every doc was put to the judge
    assert report["judge"]["engine"] == "fake"


# -- the beadlog CLI -----------------------------------------------------------------------


class _Point:
    name = "docs"

    def load(self) -> Any:
        return DocsStrategy


def test_beadlog_extract_docs_writes_the_report_and_lists_the_residual(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(repo, "docs/design/a.md", "Beads: bh-100\n")
    _write(repo, "docs/design/b.md", "# Nothing here\n")
    _write(repo, "docs/design/c.md", "Beads: bh-gone\n")
    baseline_path = tmp_path / "baseline.bdlg"
    beadlog.write_archive(
        baseline_path,
        [_create("bh-100", "One")],
        groups=[beadlog.Group(GROUP, "bh")],
        generator=beadlog.Generator("test", "0"),
    )
    out = tmp_path / "docs.bdlg"
    argv = [
        "extract",
        "docs",
        "--root",
        str(repo),
        "--group",
        GROUP,
        "--group-dir",
        "bh",
        "--extracted-at",
        EXTRACTED,
        "--baseline",
        str(baseline_path),
        "-o",
        str(out),
    ]
    assert main(argv, entry_points=lambda: [_Point()]) == 0
    printed = capsys.readouterr().out
    assert "extracted 1 events with docs" in printed
    assert "DRIFT bh-gone" in printed
    assert "RESIDUAL docs/design/b.md" in printed
    with beadlog.ArchiveReader(out) as reader:
        annotation = reader.manifest.annotations["bdlg.docs"]
    assert annotation["counts"]["PRESENT-needs-stamp"] == 1
    assert list(annotation["residual"]) == ["docs/design/b.md"]


def test_the_cli_hands_the_judge_over_only_when_enabled(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(repo, "docs/design/b.md", "# Nothing here\n")
    baseline_path = tmp_path / "baseline.bdlg"
    beadlog.write_archive(
        baseline_path,
        [_create("bh-100", "One")],
        groups=[beadlog.Group(GROUP, "bh")],
        generator=beadlog.Generator("test", "0"),
    )
    seen: list[ExtractRequest] = []

    class Spy(DocsStrategy):
        def propose(self, request: ExtractRequest):  # type: ignore[no-untyped-def]
            seen.append(request)
            return super().propose(request)

    class SpyPoint:
        name = "docs"

        def load(self) -> Any:
            return Spy

    argv = [
        "extract",
        "docs",
        "--root",
        str(repo),
        "--group",
        GROUP,
        "--group-dir",
        "bh",
        "--extracted-at",
        EXTRACTED,
        "--baseline",
        str(baseline_path),
        "-o",
        str(tmp_path / "o.bdlg"),
    ]
    assert main(argv, entry_points=lambda: [SpyPoint()], environ={}) == 0
    assert seen[-1].judge is None
    assert (
        main(
            [*argv, "--judge-enabled", "true"],
            entry_points=lambda: [SpyPoint()],
            environ={},
            runner=lambda *a, **k: None,  # type: ignore[arg-type,return-value]
        )
        == 0
    )
    assert seen[-1].judge is not None


# -- registration --------------------------------------------------------------------------


def test_it_is_a_beadlog_extract_subcommand() -> None:
    assert "docs" in discover_strategies(lambda: [_Point()])


def test_entry_point_is_declared() -> None:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    if not pyproject.is_file():  # the Pants sandbox ships sources only
        pytest.skip("package metadata not in this sandbox")
    assert 'docs = "beadlog_sources.docs:DocsStrategy"' in pyproject.read_text(encoding="utf-8")


def test_propose_is_importable_as_a_function(repo: Path) -> None:
    _write(repo, DOC, "Beads: bh-100\n")
    proposal = propose(_request(repo, [_create("bh-100", "One")]))
    assert proposal.report.counts()["PRESENT-needs-stamp"] == 1
