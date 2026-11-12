"""End-to-end trials of ``bh backfill history`` over synthetic hives (bh-tkvs1).

Three hives mirror the reporters' trials (#143 create beads, #144 more sources): a commit-only
repository, a docs-heavy one with the judge off and a threshold, and one documented in HTML.
Each is driven through the real ``bh backfill history --until apply --yes`` CLI over a fake bd
store, then run again: the converged run proposes zero events. Histories are built with one
``git fast-import`` each (fixed commit dates, no per-commit subprocess), so a trial takes seconds
and uses no network, wall clock or sleeps. Commit-only is 250 commits, the reporter's size."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from tests.test_backfill_bd import FakeBd, Ticks
from tests.test_backfill_bd import cfg_path as cfg_path  # noqa: F401 — fixture resolved by name
from tests.test_backfill_history_cli import (
    ENTRY,
    TRIPLET,
    InMemoryLedgerDirectory,
    _config,
    _row,
    _run,
)

from beadhive import backfill_cli, backfill_create
from beadhive.backfill_bd import BdStoreLoader

pytestmark = pytest.mark.integration

EPOCH = 1_788_000_000  # 2026-08-29; every synthetic commit is a fixed second after it


def _fast_import(repo: Path, commits: list[dict[str, Any]]) -> None:
    """Build a whole history with one ``git fast-import`` (no per-commit subprocess, fixed dates).

    Each commit is ``{"msg", "files", "merge"?}``; ``merge`` is the 1-based index of a commit to
    merge in as a second parent."""
    lines: list[str] = []
    for n, commit in enumerate(commits, 1):
        when = f"{EPOCH + n * 3600} +0000"
        msg = commit["msg"].encode()
        lines += [
            "commit refs/heads/main",
            f"mark :{n}",
            f"author T <t@example.com> {when}",
            f"committer T <t@example.com> {when}",
            f"data {len(msg)}",
            commit["msg"],
        ]
        if n > 1:
            lines.append(f"from :{n - 1}")
        if commit.get("merge"):
            lines.append(f"merge :{commit['merge']}")
        for name, text in commit["files"].items():
            data = text.encode()
            lines += [f"M 100644 inline {name}", f"data {len(data)}", text]
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "fast-import", "--quiet"],
        input=("\n".join(lines) + "\n").encode(),
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "-C", str(repo), "reset", "-q", "--hard"], check=True)


def _wire(monkeypatch, repo: Path, rows: list[dict[str, Any]]) -> FakeBd:
    store = FakeBd(rows, identity_labels=TRIPLET)
    group = backfill_cli.hive_group(ENTRY, repo)
    monkeypatch.setattr(backfill_cli, "_target", lambda hive: (repo, group))
    monkeypatch.setattr(backfill_cli, "hive_prefix", lambda hive: ENTRY["prefix"])
    ids = sorted(store.rows)
    monkeypatch.setattr(
        backfill_cli,
        "history_directory",
        lambda hive, cfg, backfill: InMemoryLedgerDirectory({"ag-infra": ids}),
    )

    def loader(cwd: Path, *, group: str, pre_state_path: Path | None = None) -> BdStoreLoader:
        return BdStoreLoader(store, group=group, clock=Ticks(), pre_state_path=pre_state_path)

    monkeypatch.setattr(backfill_cli, "store_loader", loader)
    monkeypatch.setattr(
        backfill_cli, "refresh_coverage_after_apply", lambda hive, quiet=False: None
    )
    real = backfill_create.create_mode_options
    monkeypatch.setattr(backfill_create, "create_mode_options", lambda h, c, m: real(ENTRY, c, m))
    return store


def _md(title: str, status: str = "accepted") -> str:
    return f"---\nstatus: {status}\ndate: 2026-08-20\n---\n\n# {title}\n\n{title}\n"


def _html(title: str) -> str:
    return (
        f"<!DOCTYPE html><html><head><title>{title}</title></head>"
        f"<body><h1>{title}</h1><p>Status: accepted. {title}.</p></body></html>\n"
    )


TOPICS = [
    "cache",
    "queue",
    "auth",
    "billing",
    "search",
    "export",
    "import",
    "audit",
    "sync",
    "report",
    "notify",
    "schedule",
    "backup",
    "metrics",
    "gateway",
    "storage",
]


def _converge(tmp_path: Path, *sources: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply, then run again; the two run summaries."""
    args = ("--sources", ",".join(sources), "--until", "apply", "--yes", "--json")
    first = json.loads(_run(tmp_path, *args).stdout)
    second = json.loads(_run(tmp_path, *args).stdout)
    return first, second


def _assert_converged(first: dict[str, Any], second: dict[str, Any], store: FakeBd, before: int):
    """The second run proposed zero events and wrote nothing."""
    again = second["stages"]["plan"]
    assert again["events"] == 0 and again["changes"] == 0
    assert again["units"]["NEW"] == 0
    assert second["stages"]["apply"]["completed"] == 0
    assert len(store.writes) == before
    assert second["stages"]["collate"]["units"] == first["stages"]["collate"]["units"]


def _created(store: FakeBd, source: str) -> list[dict[str, Any]]:
    return [
        r
        for r in store.rows.values()
        if "origin:backfill" in r["labels"] and f"source:{source}" in r["labels"]
    ]


def _assert_well_formed(rows: list[dict[str, Any]], source: str) -> None:
    """Created beads carry provenance, the source label and the identity triplet, and are closed
    (the units are history), apart from a doc still drafted."""
    assert rows
    for row in rows:
        labels = set(row["labels"])
        assert {"origin:backfill", f"source:{source}", *TRIPLET} <= labels, row["labels"]
        assert row["id"].startswith(ENTRY["prefix"] + "-")
        assert row["external_ref"]
        assert row["title"]


def _by_ref(store: FakeBd) -> dict[str, dict[str, Any]]:
    return {r["external_ref"]: r for r in store.rows.values() if r.get("external_ref")}


# -- commit-only (#143.1) ----------------------------------------------------------------------

AREAS = ("core", "web", "cli", "store", "sync")
TYPES = ("feat", "feat", "fix", "chore", "docs", "feat", "test", "refactor")


def _commit_only_history(count: int) -> tuple[list[dict[str, Any]], int]:
    commits: list[dict[str, Any]] = []
    feats = 0
    for n in range(1, count + 1):
        area = AREAS[n % len(AREAS)]
        kind = TYPES[n % len(TYPES)]
        if n % 25 == 0 and n > 5:  # a merge of an earlier line of work
            commits.append(
                {
                    "msg": f"Merge pull request #{n} from team/{area}-work\n",
                    "merge": n - 4,
                    "files": {f"{area}/merged{n}.txt": "m"},
                }
            )
            continue
        feats += kind == "feat"
        commits.append(
            {
                "msg": f"{kind}({area}): change number {n} of the {area} module\n",
                "files": {f"{area}/f{n}.txt": f"{n}"},
            }
        )
    return commits, feats


def test_commit_only_hive_gets_epics_and_feat_issues(cfg_path, monkeypatch, tmp_path) -> None:  # noqa: F811
    repo = tmp_path / "repo"
    commits, feats = _commit_only_history(250)
    _fast_import(repo, commits)
    store = _wire(monkeypatch, repo, [])  # no baseline bead at all
    _config(cfg_path, collate={"create": "propose"})

    first, second = _converge(tmp_path, "git")

    plan = first["stages"]["plan"]
    assert plan["create"] == "propose" and plan["creates_skipped"] == 0
    rows = _created(store, "commit")
    issues = [r for r in rows if r["issue_type"] == "feature"]
    epics = [r for r in store.rows.values() if r["issue_type"] == "epic"]
    assert len(issues) == feats > 0, "one feat issue per feat commit, never 0"
    assert len(epics) >= 2, "areas group the units into epics"
    assert plan["units"]["NEW"] == len(store.rows) == plan["by_class"]["NEW"]
    assert plan["units"]["residual"] == 0
    _assert_well_formed(rows, "commit")
    assert {r["status"] for r in rows} == {"closed"}
    _assert_converged(first, second, store, first["stages"]["apply"]["completed"])
    assert second["stages"]["collate"]["units"] == len(store.rows)


# -- docs-heavy, judge off, threshold (#143.2) ---------------------------------------------------


def test_docs_heavy_hive_judge_off_with_threshold(cfg_path, monkeypatch, tmp_path) -> None:  # noqa: F811
    repo = tmp_path / "repo"
    files: dict[str, str] = {}
    rows = []
    for n, topic in enumerate(TOPICS[:6], 1):
        rows.append(
            _row(f"ag-infra-{n}", title=f"{topic.title()} layer rollout plan", status="closed")
        )
        files[f"docs/design/{topic}-plan.md"] = _md(f"{topic.title()} layer rollout plan")  # same
        files[f"docs/design/{topic}-notes.md"] = _md(f"{topic.title()} layer rollout notes")  # near
        files[f"docs/design/{topic}-steps.md"] = _md(
            f"{topic.title()} rollout migration steps for tenants"
        )
    for topic in TOPICS[6:]:  # nothing in the baseline shares a token with these
        files[f"docs/adr/{topic}-handbook.md"] = _md(f"{topic.title()}ish zeta handbook")
    files["README.md"] = "# readme\n"
    _fast_import(repo, [{"msg": "docs: add the design docs\n", "files": files}])
    store = _wire(monkeypatch, repo, rows)
    _config(cfg_path, collate={"create": "propose", "identify": {"new_threshold": 50}})

    first, second = _converge(tmp_path, "docs")

    units = first["stages"]["plan"]["units"]
    assert units["NEW"] == 16 and units["PRESENT"] == 6 and units["residual"] == 6
    assert first["stages"]["collate"]["residual"] == 6, "the residual is reported"
    collation = json.loads(Path(first["files"]["collation"]).read_text())
    steps = {(v["step"], v["class"]) for v in collation["verdicts"]}
    assert ("external-ref", "PRESENT") in steps
    assert {u["unit"] for u in collation["residual"]} == {
        f"doc:docs/design/{t}-notes.md" for t in TOPICS[:6]
    }
    created = _created(store, "docs")
    assert len(created) == 16
    _assert_well_formed(created, "docs")
    refs = _by_ref(store)
    assert all(f"doc:docs/design/{t}-plan.md" in refs for t in TOPICS[:6]), "linked, not duplicated"
    assert all(
        r["id"] in {f"ag-infra-{n}" for n in range(1, 7)}
        for t in TOPICS[:6]
        for r in [refs[f"doc:docs/design/{t}-plan.md"]]
    )
    assert not any(f"doc:docs/design/{t}-notes.md" in refs for t in TOPICS[:6])  # left to a human
    _assert_converged(first, second, store, first["stages"]["apply"]["completed"])
    assert second["stages"]["collate"]["residual"] == 6  # still reported, still not guessed


# -- HTML-documented (#144.1) -------------------------------------------------------------------


def test_html_documented_hive_finds_all_eleven_design_docs(cfg_path, monkeypatch, tmp_path) -> None:  # noqa: F811
    repo = tmp_path / "repo"
    html_topics = TOPICS[:11]
    # The first three share their whole title with a baseline bead (linked); the rest share no
    # token with any bead, so B5 decides them NEW by rule. A token shared with a bead (every doc
    # titled "... architecture") would leave them residual without a threshold, by design.
    word = {t: ("architecture" if n < 3 else "blueprint") for n, t in enumerate(html_topics)}
    files = {
        f"docs/design/{t}-{word[t]}.html": _html(f"{t.title()} {word[t]}") for t in html_topics
    }
    files["docs/adr/001-adopt-the-docs.md"] = _md("Adopt the docs convention")
    files["docs/guide/start.md"] = _md("Getting started guide")
    _fast_import(repo, [{"msg": "docs: add the design docs\n", "files": files}])
    rows = [
        _row(f"ag-infra-{n}", title=f"{t.title()} architecture", status="closed")
        for n, t in enumerate(html_topics[:3], 1)
    ]
    store = _wire(monkeypatch, repo, rows)
    _config(
        cfg_path, collate={"create": "propose"}, sources={"docs": {"formats": ["markdown", "html"]}}
    )

    first, second = _converge(tmp_path, "docs")

    refs = _by_ref(store)
    for t in html_topics:
        assert f"doc:docs/design/{t}-{word[t]}.html" in refs, f"{t} neither linked nor created"
    linked = {refs[f"doc:docs/design/{t}-{word[t]}.html"]["id"] for t in html_topics[:3]}
    assert linked == {"ag-infra-1", "ag-infra-2", "ag-infra-3"}
    html_created = [r for r in _created(store, "docs") if r["external_ref"].endswith(".html")]
    assert len(html_created) == 8
    _assert_well_formed(_created(store, "docs"), "docs")
    assert first["stages"]["plan"]["units"]["residual"] == 0
    _assert_converged(first, second, store, first["stages"]["apply"]["completed"])


def test_html_is_not_read_unless_opted_in(cfg_path, monkeypatch, tmp_path) -> None:  # noqa: F811
    repo = tmp_path / "repo"
    _fast_import(
        repo,
        [
            {
                "msg": "docs: add one\n",
                "files": {"docs/design/a-architecture.html": _html("A architecture")},
            }
        ],
    )
    store = _wire(monkeypatch, repo, [])
    _config(cfg_path, collate={"create": "propose"})
    _converge(tmp_path, "docs")
    assert not any(r.get("external_ref", "").endswith(".html") for r in store.rows.values())
