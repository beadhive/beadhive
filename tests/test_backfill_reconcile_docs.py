"""The ``docs`` and ``planning`` strategies applied through the bd StoreLoader.

The neutral packages prove the event sets (``packages/beadlog-sources/tests``); this root suite
proves the two things only BeadHive can: the events load into a bd store (the in-memory ``FakeBd``
of ``test_backfill_bd``) and converge, and the beads the planning pass creates pass
``bh label validate`` (``validate.bead_violations``). The neutral ``bh backfill apply`` applies
only NEW beads and reconcile classes a ``set`` on an existing bead as DRIFT, so stamps are loaded
through the StoreLoader directly, as the ``git-commits`` tests do.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from beadhive import validate
from beadlog_sources import ChoiceQuery, DocsStrategy, ExtractRequest, Judgment, PlanningStrategy
from test_backfill_bd import GROUP, TRIPLET, FakeBd, loader

EXTRACTED = "2026-10-31T00:00:00Z"
SEED = {"id": "ag-infra-1", "title": "Seed bead", "status": "open", "issue_type": "task"}
P1 = ".planning/phases/01-foundation"
P2 = ".planning/phases/02-features"


@pytest.fixture
def cfg_path(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text((Path(__file__).parent / "fixture_config.yaml").read_text())
    monkeypatch.setenv("BH_HOME", str(tmp_path))
    monkeypatch.setenv("BH_CONFIG", str(path))
    return path


def _git(repo: Path, *args: str, when: str | None = None) -> None:
    env = {**os.environ, **({"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when} if when else {})}
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


def _repo(
    tmp_path: Path, files: dict[str, str], *, when: str = "2026-09-01T10:00:00+00:00"
) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for command in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@example.com"],
        ["config", "user.name", "T"],
        ["config", "commit.gpgsign", "false"],
    ):
        _git(repo, *command)
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "docs: add the tree", when=when)
    return repo


def _request(
    repo: Path, baseline: Sequence[Any], judge: Any = None, **options: str
) -> ExtractRequest:
    return ExtractRequest(
        root=repo,
        group=GROUP,
        extracted_at=EXTRACTED,
        baseline=tuple(baseline),
        options=options,
        judge=judge,
    )


GSD_TREE = {
    f"{P1}/01-01-PLAN.md": "---\ndepends_on: []\n---\n# Foundation: set up\n",
    f"{P1}/01-01-SUMMARY.md": "# done\n",
    f"{P1}/01-02-PLAN.md": "---\ndepends_on:\n  - 01-01\n---\n# Foundation: wire CI\n",
    f"{P2}/02-01-PLAN.md": '---\ndepends_on: ["01-02"]\n---\n# Features: first slice\n',
}


def test_planning_events_load_into_a_bd_store_pass_label_validation_and_converge(
    tmp_path: Path, cfg_path: Path
) -> None:
    repo = _repo(tmp_path, GSD_TREE)
    store = FakeBd([SEED], identity_labels=TRIPLET)
    bd_loader = loader(store)
    strategy = PlanningStrategy()
    # complexity routing is hive policy, not a source fact: either name a tier through option
    # `labels`, as here, or run `bh backfill complexity` afterwards (see the test below)
    events = strategy.extract(_request(repo, bd_loader.export(), labels="complexity:MEDIUM"))
    assert events

    bd_loader.load(events, dry_run=False)

    created = {key: row for key, row in store.rows.items() if key != "ag-infra-1"}
    assert len(created) == 5  # two phases, three plans
    by_ref = {row["external_ref"]: row for row in created.values()}
    assert set(by_ref) == {
        P1,
        P2,
        f"{P1}/01-01-PLAN.md",
        f"{P1}/01-02-PLAN.md",
        f"{P2}/02-01-PLAN.md",
    }
    for key, row in created.items():
        assert {"origin:backfill", "source:planning", *TRIPLET} <= set(row["labels"])
        assert validate.bead_violations(None, key, row["labels"], row["issue_type"]) == []

    assert by_ref[P1]["issue_type"] == "epic" and by_ref[P2]["status"] == "open"
    assert by_ref[f"{P1}/01-01-PLAN.md"]["status"] == "closed"
    assert by_ref[f"{P1}/01-02-PLAN.md"]["status"] == "open"  # no summary
    assert by_ref[P1]["status"] == "open"  # not every plan is summarized
    assert by_ref[f"{P1}/01-01-PLAN.md"]["closed_at"] == "2026-09-01T10:00:00Z"

    def deps(ref: str) -> set[tuple[str, str]]:
        row = by_ref[ref]
        ids = {r["id"]: r["external_ref"] for r in created.values()}
        return {(d["type"], ids[d["depends_on_id"]]) for d in row.get("dependencies", [])}

    assert deps(f"{P1}/01-02-PLAN.md") == {
        ("parent-child", P1),
        ("blocks", f"{P1}/01-01-PLAN.md"),
    }
    assert deps(f"{P2}/02-01-PLAN.md") == {
        ("parent-child", P2),
        ("blocks", f"{P1}/01-02-PLAN.md"),
    }

    writes = list(store.writes)
    bd_loader.load(events, dry_run=False)  # loading the same events again writes nothing
    assert store.writes == writes
    again = strategy.extract(_request(repo, bd_loader.export(), labels="complexity:MEDIUM"))
    assert again == ()  # converged: zero events


def test_without_a_tier_the_only_label_problem_is_the_routing_contract(
    tmp_path: Path, cfg_path: Path
) -> None:
    """Origin and identity labels are valid as created; the one thing left for hive policy is the
    singular `complexity:<TIER>` label, which `bh backfill complexity` converges afterwards."""
    repo = _repo(tmp_path, GSD_TREE)
    store = FakeBd([SEED], identity_labels=TRIPLET)
    bd_loader = loader(store)
    bd_loader.load(PlanningStrategy().extract(_request(repo, bd_loader.export())), dry_run=False)
    created = {key: row for key, row in store.rows.items() if key != "ag-infra-1"}
    assert created
    for key, row in created.items():
        (problem,) = validate.bead_violations(None, key, row["labels"], row["issue_type"])
        assert problem.endswith("bad-routing:expected exactly one complexity label, found 0")
        assert validate.bead_violations(None, key, row["labels"]) == []  # nothing else is wrong


def test_docs_stamps_load_as_external_ref_and_converge_to_in_sync(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        {
            "docs/design/a.md": "---\n- Beads: ag-infra-1\n---\n# A\n",
            "docs/design/b.md": "# nothing links here\n",
        },
    )
    store = FakeBd([SEED], identity_labels=TRIPLET)
    bd_loader = loader(store)
    strategy = DocsStrategy()
    events = strategy.extract(_request(repo, bd_loader.export()))
    assert [(e.kind, e.bead.id) for e in events] == [("set", "ag-infra-1")]

    bd_loader.load(events, dry_run=False)
    assert store.rows["ag-infra-1"]["external_ref"] == "docs/design/a.md"

    again = DocsStrategy()
    assert again.extract(_request(repo, bd_loader.export())) == ()
    report = again.report()
    assert report is not None
    assert report["counts"]["PRESENT-in-sync"] == 1
    assert report["residual"] == ["docs/design/b.md"]  # the judge is off: a human decides


class _SaysNew:
    engine, engine_version, model = "fake", "1", None

    def choose(self, query: ChoiceQuery) -> Judgment:
        return Judgment("new", {"new": 1.0}, 0.7, len(query.options), "novel", "fake", "1")

    def score(self, query: Any) -> Judgment:  # pragma: no cover
        raise AssertionError


def test_a_judged_new_doc_loads_with_origin_labels_and_passes_label_validation(
    tmp_path: Path, cfg_path: Path
) -> None:
    repo = _repo(tmp_path, {"docs/design/seed-notes.md": "# Seed notes\n"})
    store = FakeBd([SEED], identity_labels=TRIPLET)
    bd_loader = loader(store)
    judge = _SaysNew()
    options = {"labels": "complexity:SIMPLE"}
    events = DocsStrategy().extract(_request(repo, bd_loader.export(), judge, **options))
    bd_loader.load(events, dry_run=False)
    (key,) = (k for k in store.rows if k != "ag-infra-1")
    row = store.rows[key]
    assert row["external_ref"] == "docs/design/seed-notes.md"
    assert {"origin:backfill", "source:docs", *TRIPLET} <= set(row["labels"])
    assert validate.bead_violations(None, key, row["labels"], row["issue_type"]) == []
    assert DocsStrategy().extract(_request(repo, bd_loader.export(), judge, **options)) == ()
