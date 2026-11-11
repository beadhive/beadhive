"""The neutral ``planning`` strategy: a GSD ``.planning`` tree as epics, issues and edges.

The tree is real files in a throwaway ``git init`` repo (plans committed first, summaries later,
so ``occurred_at`` has two distinct, checkable dates). "Applying into a store" is the BeadLog fold
of baseline plus events, which is what a StoreLoader reproduces; the root suite proves the same
through the bd StoreLoader. No BeadHive is involved.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

import beadlog
from beadlog import BeadEvent, BeadRef, Edge, fold, mint_event
from beadlog_sources import ExtractRequest, PlanningStrategy, reconcile
from beadlog_sources import _sources as sources
from beadlog_sources.cli import discover_strategies, main
from beadlog_sources.planning import deps_of, parse_depends, scan_tree

GROUP = "urn:uuid:00000000-0000-4000-8000-0000000000e1"
EXTRACTED = "2026-10-31T00:00:00Z"
TREE = ".planning/phases"
P1 = f"{TREE}/01-foundation"
P2 = f"{TREE}/02-features"
P3 = f"{TREE}/03-empty"
A1, A2 = f"{P1}/01-01-PLAN.md", f"{P1}/01-02-PLAN.md"
B1, B2 = f"{P2}/02-01-PLAN.md", f"{P2}/02-02-PLAN.md"
PLANS_AT = "2026-09-01T10:00:00Z"
SUMMARIES_AT = "2026-09-05T12:30:00Z"


# -- deps_of: the selftest of reconcile.sh, ported -----------------------------------------


@pytest.mark.parametrize(
    ("name", "text", "expected"),
    [
        ("inline-empty", "---\ndepends_on: []\n---\n", "[]"),
        ("inline-quoted", '---\ndepends_on: ["10-01"]\n---\n', '["10-01"]'),
        ("inline-bare", "---\ndepends_on: [11-01]\n---\n", "[11-01]"),
        ("block-quoted", '---\ndepends_on:\n  - "01-01"\nx: 1\n---\n', "[01-01]"),
        ("block-multi", "---\ndepends_on:\n  - 01-01\n  - 01-02\n---\n", "[01-01,01-02]"),
        ("none", "---\nphase: 05\n---\n", ""),
    ],
)
def test_deps_of_selftest_cases(name: str, text: str, expected: str) -> None:
    assert deps_of(text) == expected, name


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("---\ndepends_on: []\n---\n", []),
        ('---\ndepends_on: ["10-01"]\n---\n', ["10-01"]),
        ("---\ndepends_on: [11-01, '11-02']\n---\n", ["11-01", "11-02"]),
        ('---\ndepends_on:\n  - "01-01"\nx: 1\n---\n', ["01-01"]),
        ("---\ndepends_on:\n  - 01-01\n  - 01-02\n---\n", ["01-01", "01-02"]),
        ("---\nphase: 05\n---\n", []),
        ("depends_on: [01-01]\n", []),  # no frontmatter: nothing is read
        ("---\nphase: 5\n---\ndepends_on: [01-01]\n", []),  # only the frontmatter is read
        ("---\r\ndepends_on:\r\n  - 01-01\r\n---\r\n", ["01-01"]),
        ("---\ndepends_on:\n  - 01-01\nwave: 1\n  - 99-99\n---\n", ["01-01"]),  # block ended
    ],
)
def test_parse_depends(text: str, expected: list[str]) -> None:
    assert parse_depends(text) == expected


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


def _write(repo: Path, path: str, text: str) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _commit(repo: Path, subject: str, when: str) -> None:
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


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test User")
    _git(root, "config", "commit.gpgsign", "false")
    return root


@pytest.fixture
def tree(repo: Path) -> Path:
    """The GSD fixture: 3 phases, 4 plans; phase 01 fully summarized, phase 03 empty."""
    _write(repo, A1, "---\nphase: 01\ndepends_on: []\n---\n# Foundation: set up the repo\n")
    _write(repo, A2, "---\nphase: 01\ndepends_on:\n  - 01-01\n---\n# Foundation: wire CI\n")
    _write(repo, B1, '---\nphase: 02\ndepends_on: ["01-02"]\n---\n# Features: first slice\n')
    _write(repo, B2, "---\nphase: 02\ndepends_on: [02-01, 99-99]\n---\n# Features: second slice\n")
    (repo / P3).mkdir(parents=True)
    (repo / P3 / ".gitkeep").write_text("")
    _commit(repo, "docs(planning): add plans", PLANS_AT)
    _write(repo, f"{P1}/01-01-SUMMARY.md", "# done\n")
    _write(repo, f"{P1}/01-02-SUMMARY.md", "# done\n")
    _commit(repo, "docs(planning): summarize phase 01", SUMMARIES_AT)
    return repo


def _existing(bead: str, title: str, **extra: Any) -> BeadEvent:
    at = "2026-08-01T00:00:00Z"
    return mint_event(
        "create",
        BeadRef(GROUP, bead),
        {"type": extra.pop("type", "task"), "fields": {"title": title, **extra}},
        occurred_at=at,
        recorded_at=at,
        extracted_at=EXTRACTED,
        source=("bd", f"issues/{bead}#create"),
        provenance="observed",
    )


SEED = (_existing("ag-infra-1", "Seed bead"),)


def _request(root: Path, baseline: Sequence[BeadEvent] = SEED, **options: str) -> ExtractRequest:
    return ExtractRequest(
        root=root, group=GROUP, extracted_at=EXTRACTED, baseline=tuple(baseline), options=options
    )


def _extract(root: Path, baseline: Sequence[BeadEvent] = SEED, **options: str):
    options.setdefault("id_prefix", "ag-infra")  # prefixes are explicit, never derived
    strategy = PlanningStrategy()
    return strategy, strategy.extract(_request(root, baseline, **options))


def _paths(events: Sequence[BeadEvent]) -> dict[str, str]:
    """Bead id -> the doc path its create names."""
    return {
        e.bead.id: str(dict(e.data["fields"])["external_ref"]) for e in events if e.kind == "create"
    }


def _shape(
    events: Sequence[BeadEvent], known: dict[str, str] | None = None
) -> set[tuple[Any, ...]]:
    """Every event as a tuple over doc paths instead of minted bead ids (``known``: the ids of
    beads an earlier extraction created)."""
    path = {**(known or {}), **_paths(events)}
    out: set[tuple[Any, ...]] = set()
    for e in events:
        who = path.get(e.bead.id, e.bead.id)
        if e.kind == "create":
            fields = dict(e.data["fields"])
            out.add(("create", who, e.data["type"], fields["title"], e.occurred_at))
        elif e.kind == "label_add":
            out.add(("label", who, e.data["label"]))
        elif e.kind == "edge_add":
            out.add(
                (
                    "edge",
                    who,
                    e.data["type"],
                    path.get(e.data["target"]["id"], e.data["target"]["id"]),
                )
            )
        elif e.kind == "phase":
            out.add(("phase", who, e.data["phase"], e.occurred_at))
        else:  # pragma: no cover
            raise AssertionError(f"unexpected {e.kind}")
    return out


# -- the fixture tree produces exactly the expected event set -------------------------------


def test_scan_tree_reads_phases_plans_and_summaries(tree: Path) -> None:
    phases = scan_tree(tree)
    assert [p.path for p in phases] == [P1, P2, P3]
    assert [p.title for p in phases] == [
        "Phase 01: Foundation",
        "Phase 02: Features",
        "Phase 03: Empty",
    ]
    assert [(plan.key, plan.deps, bool(plan.summary)) for plan in phases[0].plans] == [
        ("01-01", [], True),
        ("01-02", ["01-01"], True),
    ]
    assert [(plan.key, plan.deps) for plan in phases[1].plans] == [
        ("02-01", ["01-02"]),
        ("02-02", ["02-01", "99-99"]),
    ]
    assert phases[0].closed and not phases[1].closed and not phases[2].closed


def test_the_fixture_tree_produces_exactly_the_expected_events(tree: Path) -> None:
    strategy, events = _extract(tree)
    expected: set[tuple[Any, ...]] = set()
    for path, kind, title, when in [
        (P1, "epic", "Phase 01: Foundation", PLANS_AT),
        (P2, "epic", "Phase 02: Features", PLANS_AT),
        (P3, "epic", "Phase 03: Empty", "2026-09-01T10:00:00Z"),
        (A1, "task", "Foundation: set up the repo", PLANS_AT),
        (A2, "task", "Foundation: wire CI", PLANS_AT),
        (B1, "task", "Features: first slice", PLANS_AT),
        (B2, "task", "Features: second slice", PLANS_AT),
    ]:
        expected.add(("create", path, kind, title, when))
        expected.add(("label", path, "origin:backfill"))
        expected.add(("label", path, "source:planning"))
    expected |= {
        ("edge", A1, "parent-child", P1),
        ("edge", A2, "parent-child", P1),
        ("edge", B1, "parent-child", P2),
        ("edge", B2, "parent-child", P2),
        ("edge", A2, "blocks", A1),  # block-style depends_on
        ("edge", B1, "blocks", A2),  # inline, quoted, across phases
        ("edge", B2, "blocks", B1),  # inline, bare; 99-99 names no plan
        ("phase", A1, "closed", SUMMARIES_AT),
        ("phase", A2, "closed", SUMMARIES_AT),
        ("phase", P1, "closed", SUMMARIES_AT),
    }
    assert _shape(events) == expected
    assert len(events) == len(expected) == 31  # no duplicates

    report = strategy.report()
    assert report is not None
    assert report["counts"] == {"phases": 3, "plans": 4, "new": 7, "present": 0, "closed": 3}
    assert report["edges"] == 7
    assert [(u["plan"], u["dep"]) for u in report["unresolved"]] == [(B2, "99-99")]
    assert report["proposed"] == 31


def test_epic_with_no_plans_is_dated_by_its_first_tracked_file(tree: Path) -> None:
    _, events = _extract(tree)
    create = next(e for e in events if e.kind == "create" and e.data["type"] == "epic")
    assert create.occurred_at == PLANS_AT
    assert {e.provenance for e in events} == {"derived"}
    assert all(e.recorded_at is None for e in events)


def test_the_events_are_deterministic_and_reconcile_as_all_new(tree: Path) -> None:
    _, first = _extract(tree)
    _, second = _extract(tree)
    assert [e.id for e in first] == [e.id for e in second]
    plan = reconcile(first, SEED)
    assert plan.counts() == {"NEW": 7, "PRESENT": 0, "NOISE": 0, "DRIFT": 0}


def test_minted_ids_use_the_given_prefix_and_never_collide(tree: Path) -> None:
    _, events = _extract(tree, [_existing("ag-infra-9", "x"), _existing("ag-infra-10", "y")])
    ids = set(_paths(events))
    assert len(ids) == 7 and all(i.startswith("ag-infra-") for i in ids)
    assert not ids & {"ag-infra-9", "ag-infra-10"}
    _, again = _extract(tree, [_existing("ag-infra-9", "x"), _existing("ag-infra-10", "y")])
    assert set(_paths(again)) == ids
    _, other = _extract(tree, [], id_prefix="zz")
    assert {i.split("-")[0] for i in _paths(other)} == {"zz"}


def test_minting_without_an_id_prefix_fails_naming_the_option(tree: Path) -> None:
    for baseline in ([], SEED):  # a populated baseline is never read for a prefix guess
        with pytest.raises(ValueError, match="id_prefix"):
            PlanningStrategy().extract(_request(tree, baseline))


def test_extra_labels_and_the_issue_type_are_options(tree: Path) -> None:
    _, events = _extract(tree, labels="provider:p,org:o", issue_type="feature")
    labels = {str(e.data["label"]) for e in events if e.kind == "label_add"}
    assert labels == {"origin:backfill", "source:planning", "provider:p", "org:o"}
    assert {e.data["type"] for e in events if e.kind == "create"} == {"epic", "feature"}


# -- idempotency ---------------------------------------------------------------------------


def test_applying_the_events_and_re_running_proposes_zero_events(tree: Path) -> None:
    _, events = _extract(tree)
    applied = [*SEED, *events]
    state = fold(applied)
    paths = _paths(events)
    by_path = {path: state[BeadRef(GROUP, ident)] for ident, path in paths.items()}
    assert all(s.exists and "origin:backfill" in s.labels for s in by_path.values())
    assert by_path[P1].phase == "closed" and by_path[P2].phase == "open"
    assert by_path[A2].phase == "closed" and by_path[B1].phase == "open"
    assert Edge("parent-child", BeadRef(GROUP, next(i for i, p in paths.items() if p == P2))) in (
        by_path[B1].edges
    )

    strategy, again = _extract(tree, applied)
    assert again == ()
    report = strategy.report()
    assert report is not None
    assert report["counts"]["new"] == 0 and report["counts"]["present"] == 7
    assert report["proposed"] == 0
    assert reconcile(events, applied).empty


def test_a_summary_that_appears_later_proposes_only_the_closes(tree: Path) -> None:
    _, events = _extract(tree)
    applied = [*SEED, *events]
    _write(tree, f"{P2}/02-01-SUMMARY.md", "# done\n")
    _write(tree, f"{P2}/02-02-SUMMARY.md", "# done\n")
    later = "2026-10-01T09:00:00Z"
    _commit(tree, "docs(planning): summarize phase 02", later)
    _, more = _extract(tree, applied)
    assert _shape(more, _paths(events)) == {
        ("phase", B1, "closed", later),
        ("phase", B2, "closed", later),
        ("phase", P2, "closed", later),
    }
    _, none = _extract(tree, [*applied, *more])
    assert none == ()


def test_a_new_plan_in_an_existing_phase_hangs_off_the_existing_epic(tree: Path) -> None:
    _, events = _extract(tree)
    applied = [*SEED, *events]
    epic = next(i for i, p in _paths(events).items() if p == P2)
    first = next(i for i, p in _paths(events).items() if p == B1)
    _write(tree, f"{P2}/02-03-PLAN.md", "---\ndepends_on: [02-01]\n---\n# Features: third\n")
    _commit(tree, "docs(planning): add 02-03", "2026-10-02T08:00:00Z")
    _, more = _extract(tree, applied)
    new = f"{P2}/02-03-PLAN.md"
    assert {s[:3] for s in _shape(more, _paths(events))} == {
        ("create", new, "task"),
        ("label", new, "origin:backfill"),
        ("label", new, "source:planning"),
        ("edge", new, "parent-child"),
        ("edge", new, "blocks"),
    }
    edges = {(e.data["type"], e.data["target"]["id"]) for e in more if e.kind == "edge_add"}
    assert edges == {("parent-child", epic), ("blocks", first)}


def test_a_hand_made_bead_matched_by_external_ref_is_left_alone(tree: Path) -> None:
    hand = _existing("ag-infra-77", "Hand-made phase", external_ref=P1, type="epic")
    _, events = _extract(tree, [*SEED, hand])
    assert P1 not in _paths(events).values()
    assert not [e for e in events if e.bead.id == "ag-infra-77" and e.kind == "label_add"]
    # but its closed phase is proposed, because its plans all have summaries
    assert [e for e in events if e.bead.id == "ag-infra-77"][0].kind == "phase"
    # and the plans hang off the hand-made epic
    parents = {
        e.data["target"]["id"]
        for e in events
        if e.kind == "edge_add" and e.data["type"] == "parent-child"
    }
    assert "ag-infra-77" in parents


def test_no_planning_tree_proposes_nothing(repo: Path) -> None:
    strategy, events = _extract(repo)
    assert events == ()
    report = strategy.report()
    assert report is not None and report["counts"]["phases"] == 0


def test_an_untracked_tree_is_undated(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    _write(root, A1, "# One\n")
    _, events = _extract(root)
    assert {e.occurred_at for e in events} == {None}
    assert len(events) == 7  # epic + plan, two labels each, the parent edge


def test_git_is_read_in_bulk_not_per_file(tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}
    real = sources._run

    def counting(root: Any, args: Sequence[str], git: str) -> Any:
        calls["n"] += 1
        return real(root, args, git)

    monkeypatch.setattr(sources, "_run", counting)
    _extract(tree)
    assert calls["n"] <= 4  # is-repo (twice), shallow check, one `git log`


# -- the beadlog CLI -----------------------------------------------------------------------


class _Point:
    name = "planning"

    def load(self) -> Any:
        return PlanningStrategy


def test_beadlog_extract_planning_writes_a_plan_that_verifies(
    tree: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    baseline = tmp_path / "baseline.bdlg"
    beadlog.write_archive(
        baseline,
        SEED,
        groups=[beadlog.Group(GROUP, "ag")],
        generator=beadlog.Generator("test", "0"),
    )
    out = tmp_path / "planning.bdlg"
    code = main(
        [
            "extract",
            "planning",
            "--root",
            str(tree),
            "--group",
            GROUP,
            "--group-dir",
            "ag",
            "--extracted-at",
            EXTRACTED,
            "--baseline",
            str(baseline),
            "--option",
            "id_prefix=ag-infra",
            "-o",
            str(out),
        ],
        entry_points=lambda: [_Point()],
    )
    assert code == 0
    printed = capsys.readouterr().out
    assert "extracted 31 events with planning" in printed
    assert "DRIFT" in printed and "99-99" in printed  # the unresolved dependency
    assert main(["verify", str(out)]) == 0
    with beadlog.ArchiveReader(out) as reader:
        annotation = reader.manifest.annotations["bdlg.planning"]
    assert annotation["counts"]["new"] == 7


def test_it_is_a_beadlog_extract_subcommand() -> None:
    assert "planning" in discover_strategies(lambda: [_Point()])


def test_entry_point_is_declared() -> None:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    if not pyproject.is_file():  # the Pants sandbox ships sources only
        pytest.skip("package metadata not in this sandbox")
    text = pyproject.read_text(encoding="utf-8")
    assert 'planning = "beadlog_sources.planning:PlanningStrategy"' in text
