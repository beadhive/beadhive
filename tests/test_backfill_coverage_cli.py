"""``bh backfill coverage`` and the applied-pass cache refresh over the in-memory fake bd.

The bd store is :class:`FakeBd` behind the real StoreLoader; git and the doc walk are the
``backfill_coverage.hive_readers`` seam, a counting fake. Nothing touches a live hive, runs git,
or reads a clock: the cache stamp comes from ``system_clock`` and is never asserted on.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from tests.test_backfill_bd import FakeBd, Ticks
from tests.test_backfill_bd import cfg_path as cfg_path  # noqa: F401 — fixture resolved by name
from tests.test_backfill_timeline_cli import hive as hive  # noqa: F401, PLC0414 — fixture
from typer.testing import CliRunner

from beadhive import backfill_cli, backfill_coverage, cli, complexity_backfill
from beadhive.backfill_bd import BdStoreLoader
from beadhive_backfill.coverage import coverage_schema, read_coverage_cache

ENV = {"COLUMNS": "120", "BH_SKIP_SETUP_CHECK": "1", "NO_COLOR": "1"}
ENTRY = {"provider": "github", "org": "acme", "repo": "widgets", "prefix": "wd"}
SHA1, SHA2, SHA3 = ("a" * 40, "b" * 40, "c" * 40)


def _row(
    iid: str, issue_type: str = "task", status: str = "closed", **extra: Any
) -> dict[str, Any]:
    return {
        "id": iid,
        "issue_type": issue_type,
        "status": status,
        "title": f"Bead {iid}",
        "priority": 2,
        "labels": [],
        "created_at": "2026-08-10T09:00:00Z",
        "updated_at": "2026-08-20T00:00:00Z",
        **({"closed_at": "2026-08-12T10:00:00Z"} if status == "closed" else {}),
        **extra,
    }


# Hand-computed: closed code-bearing = wd-1, wd-2, wd-3 (the gate, event and molecule are closed
# but excluded, wd-7 is open); wd-1 and wd-3 record commits => 2/3.
ROWS = [
    _row("wd-1", metadata={"git.commits": json.dumps([SHA1])}, external_ref="docs/a.md"),
    _row("wd-2", "bug"),
    _row("wd-3", "feature", metadata={"git.commits": json.dumps([SHA2, SHA3])}),
    _row("wd-4", "gate"),
    _row("wd-5", "event"),
    _row("wd-6", "molecule"),
    _row("wd-7", status="open"),
]


@dataclass(frozen=True)
class Commit:
    sha: str
    subject: str

    @property
    def message(self) -> str:
        return self.subject


# 5 commits, 3 name a bead => 3/5; 2 docs, docs/a.md is wd-1's external_ref => 1/2.
COMMITS = [
    Commit(SHA1, "feat: add the widget (wd-1)"),
    Commit(SHA2, "fix: edge case wd-3"),
    Commit("d" * 40, "chore(merge): bead wd-2"),
    Commit("e" * 40, "docs: typo"),
    Commit("f" * 40, "build: bump wd-version"),
]
DOCS = ["docs/a.md", "docs/b.md"]


class CountingReaders:
    """The ``hive_readers`` seam: beads come from the store reader, git and docs are literals."""

    def __init__(self, reader: Any) -> None:
        self.reader = reader
        self.calls = {"beads": 0, "commits": 0, "docs": 0}
        self.fail = False

    def beads(self) -> Iterable[Any]:
        self.calls["beads"] += 1
        return self.reader.export()

    def commits(self) -> list[Commit]:
        self.calls["commits"] += 1
        if self.fail:
            raise OSError("git exploded")
        return list(COMMITS)

    def docs(self) -> list[str]:
        self.calls["docs"] += 1
        return list(DOCS)


@pytest.fixture
def cov(cfg_path, monkeypatch, tmp_path):  # noqa: F811 — the imported fixture
    """A fake hive at ``tmp_path`` and the readers that were last built for it."""
    monkeypatch.chdir(tmp_path)
    store = FakeBd([copy.deepcopy(row) for row in ROWS])
    group = backfill_cli.hive_group(ENTRY, tmp_path)
    holder: dict[str, CountingReaders] = {}
    monkeypatch.setattr(backfill_cli, "_target", lambda hive: (tmp_path, group))
    monkeypatch.setattr(
        backfill_cli,
        "store_loader",
        lambda cwd, *, group, pre_state_path=None: BdStoreLoader(
            store, group=group, clock=Ticks(), pre_state_path=pre_state_path
        ),
    )

    def readers(cwd: Path, *, reader: Any, ref: str, settings: Any) -> CountingReaders:
        holder["last"] = holder.get("last") or CountingReaders(reader)
        return holder["last"]

    monkeypatch.setattr(backfill_coverage, "hive_readers", readers)
    return store, group, holder


def _invoke(*args: str, code: int = 0) -> Any:
    result = CliRunner().invoke(cli.app, list(args), env=ENV)
    assert result.exit_code == code, result.output
    return result


def _enable(cfg_path: Path) -> None:  # noqa: F811
    cfg_path.write_text(cfg_path.read_text() + "\nbackfill:\n  coverage:\n    enabled: true\n")


def _cache_path(tmp_path: Path) -> Path:
    cfg = backfill_cli.config.load()
    return backfill_cli._coverage_target(cfg, "", tmp_path)[0]


def _schema_check(payload: dict[str, Any]) -> None:
    jsonschema.Draft202012Validator(coverage_schema()).validate(payload)


# -- bh backfill coverage --------------------------------------------------------------------


def test_refresh_computes_the_hand_computed_ratios_and_writes_the_cache(cov, tmp_path) -> None:
    _store, _group, holder = cov
    path = _cache_path(tmp_path)
    assert not path.exists()

    payload = json.loads(_invoke("backfill", "coverage", "--refresh", "--json").stdout)
    _schema_check(payload)
    assert payload["status"] == "fresh" and payload["refreshed"] is True
    assert payload["cache_path"] == str(path)
    ratios = payload["cache"]["ratios"]
    assert ratios["closed_beads_with_commits"] == {"linked": 2, "total": 3, "ratio": 0.666667}
    assert ratios["commits_resolving_to_bead"] == {"linked": 3, "total": 5, "ratio": 0.6}
    assert ratios["docs_linked_to_bead"] == {"linked": 1, "total": 2, "ratio": 0.5}
    examples = payload["cache"]["examples"]
    assert examples["closed_beads_with_commits"] == [{"id": "wd-2", "title": "Bead wd-2"}]
    assert [c["sha"] for c in examples["commits_resolving_to_bead"]] == ["e" * 40, "f" * 40]
    assert examples["docs_linked_to_bead"] == [{"path": "docs/b.md"}]
    # one bulk read of each input, however many beads there are
    assert holder["last"].calls == {"beads": 1, "commits": 1, "docs": 1}
    cached = read_coverage_cache(path)
    assert cached is not None and cached.to_json() == payload["cache"]


def test_plain_read_with_no_cache_says_missing_and_writes_nothing(cov, tmp_path) -> None:
    payload = json.loads(_invoke("backfill", "coverage", "--json").stdout)
    _schema_check(payload)
    assert payload["status"] == "missing" and payload["cache"] is None
    assert payload["refreshed"] is False
    assert not _cache_path(tmp_path).exists()

    text = _invoke("backfill", "coverage").stdout
    assert "cache missing" in text and "bh backfill coverage --refresh" in text


def test_a_fresh_cache_is_served_as_fresh(cov, tmp_path) -> None:
    _invoke("backfill", "coverage", "--refresh")
    text = _invoke("backfill", "coverage").stdout
    assert "cache fresh" in text and "STALE" not in text
    assert "closed code beads with recorded commits: 2/3 (66.7%)" in text
    assert "commits resolving to a bead: 3/5 (60.0%)" in text
    assert "docs linked to a bead: 1/2 (50.0%)" in text
    assert "unlinked docs (top 1):" in text and "docs/b.md" in text

    payload = json.loads(_invoke("backfill", "coverage", "--json").stdout)
    _schema_check(payload)
    assert payload["status"] == "fresh" and payload["detail"] == ""


def test_a_cache_computed_from_other_inputs_is_reported_stale_not_used(cov, tmp_path) -> None:
    store, _group, _holder = cov
    _invoke("backfill", "coverage", "--refresh")
    path = _cache_path(tmp_path)
    before = path.read_text()

    # wd-2 gains a recorded commit: the live ratio would be 3/3, the cache still says 2/3
    store.rows["wd-2"]["metadata"] = {"git.commits": json.dumps([SHA1])}

    payload = json.loads(_invoke("backfill", "coverage", "--json").stdout)
    _schema_check(payload)
    assert payload["status"] == "stale" and "inputs changed" in payload["detail"]
    assert payload["cache"]["ratios"]["closed_beads_with_commits"]["linked"] == 2
    assert payload["cache"]["inputs_hash"] != payload["current_inputs_hash"]

    text = _invoke("backfill", "coverage").stdout
    assert "STALE cache (not current)" in text
    assert "stale: inputs changed" in text and "--refresh" in text
    assert path.read_text() == before  # reading never rewrites

    refreshed = json.loads(_invoke("backfill", "coverage", "--refresh", "--json").stdout)
    assert refreshed["status"] == "fresh"
    assert refreshed["cache"]["ratios"]["closed_beads_with_commits"]["linked"] == 3


def test_a_corrupt_or_unsupported_cache_is_invalid_with_a_hint(cov, tmp_path) -> None:
    _invoke("backfill", "coverage", "--refresh")
    path = _cache_path(tmp_path)
    path.write_text("{not json")
    payload = json.loads(_invoke("backfill", "coverage", "--json").stdout)
    _schema_check(payload)
    assert payload["status"] == "invalid" and payload["cache"] is None
    assert "cache unusable" in _invoke("backfill", "coverage").stdout

    cache = json.loads(json.dumps({"schema": "beadhive.backfill.coverage", "version": 2}))
    path.write_text(json.dumps(cache))
    assert json.loads(_invoke("backfill", "coverage", "--json").stdout)["status"] == "invalid"
    # a refresh repairs it
    assert (
        json.loads(_invoke("backfill", "coverage", "--refresh", "--json").stdout)["status"]
        == "fresh"
    )


def test_the_settings_are_configurable_and_part_of_the_inputs_hash(
    cov,
    cfg_path,  # noqa: F811 — the imported fixture
    tmp_path,
) -> None:
    _invoke("backfill", "coverage", "--refresh")
    cfg_path.write_text(
        cfg_path.read_text()
        + "\nbackfill:\n  coverage:\n    code_types: [bug]\n    example_limit: 0\n"
    )
    payload = json.loads(_invoke("backfill", "coverage", "--json").stdout)
    assert payload["status"] == "stale"  # the measured types changed under the cache
    refreshed = json.loads(_invoke("backfill", "coverage", "--refresh", "--json").stdout)
    assert refreshed["cache"]["ratios"]["closed_beads_with_commits"]["total"] == 1  # wd-2 only
    assert refreshed["cache"]["settings"]["code_types"] == ["bug"]
    assert refreshed["cache"]["examples"]["closed_beads_with_commits"] == []


def test_a_reader_failure_is_a_clean_error_and_leaves_the_cache_alone(cov, tmp_path) -> None:
    _invoke("backfill", "coverage", "--refresh")
    path = _cache_path(tmp_path)
    before = path.read_text()
    _store, _group, holder = cov
    holder["last"].fail = True
    # OSError is one of the command's expected error classes: no traceback, exit 1
    result = _invoke("backfill", "coverage", "--refresh", code=1)
    assert "git exploded" in result.output
    assert path.read_text() == before


# -- the applied-pass seam -------------------------------------------------------------------


def _seam_readers(monkeypatch) -> dict[str, CountingReaders]:
    holder: dict[str, CountingReaders] = {}

    def readers(cwd: Path, *, reader: Any, ref: str, settings: Any) -> CountingReaders:
        holder["last"] = CountingReaders(reader)
        return holder["last"]

    monkeypatch.setattr(backfill_coverage, "hive_readers", readers)
    return holder


def test_an_applied_pass_leaves_the_cache_alone_unless_enabled(
    hive,
    cfg_path,  # noqa: F811 — the imported fixture
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.chdir(tmp_path)
    holder = _seam_readers(monkeypatch)
    plan_path = tmp_path / "timeline.bdlg"
    _invoke("backfill", "timeline", "--plan", str(plan_path))
    _invoke("backfill", "timeline", "--apply", "--plan", str(plan_path), "--json")
    assert not _cache_path(tmp_path).exists()
    assert holder == {}  # the default reads nothing extra


def test_an_applied_timeline_pass_rewrites_the_cache_when_enabled(
    hive,
    cfg_path,  # noqa: F811 — the imported fixture
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.chdir(tmp_path)
    holder = _seam_readers(monkeypatch)
    path = _cache_path(tmp_path)
    _enable(cfg_path)
    plan_path = tmp_path / "timeline.bdlg"
    _invoke("backfill", "timeline", "--plan", str(plan_path))
    assert not path.exists()  # a preview refreshes nothing

    out = _invoke("backfill", "timeline", "--apply", "--plan", str(plan_path), "--json").stdout
    assert json.loads(out)["state"] == "applied"  # stdout stays pure JSON
    assert holder["last"].calls == {"beads": 1, "commits": 1, "docs": 1}
    cached = read_coverage_cache(path)
    assert cached is not None
    # the cache describes the hive AFTER the apply: wd-1 and wd-2 closed, only wd-1 linked
    assert cached.beads.total == 2

    text = _invoke("backfill", "timeline", "--plan", str(tmp_path / "again.bdlg")).stdout
    assert "0 beads would change" in text


def test_a_failing_refresh_only_warns_and_the_apply_still_succeeds(
    hive,
    cfg_path,  # noqa: F811
    monkeypatch,
    tmp_path,
) -> None:
    _store, _group, _readers = hive
    monkeypatch.chdir(tmp_path)
    _enable(cfg_path)

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("no git today")

    monkeypatch.setattr(backfill_coverage, "hive_readers", broken)
    plan_path = tmp_path / "timeline.bdlg"
    _invoke("backfill", "timeline", "--plan", str(plan_path))
    result = _invoke("backfill", "timeline", "--apply", "--plan", str(plan_path))
    assert "applied" in result.stdout
    assert "coverage cache not refreshed: no git today" in result.output
    assert not _cache_path(tmp_path).exists()


def test_the_neutral_apply_and_the_complexity_pass_call_the_same_seam(
    hive,
    cfg_path,  # noqa: F811
    monkeypatch,
    tmp_path,
) -> None:
    _store, _group, _readers = hive
    monkeypatch.chdir(tmp_path)
    _enable(cfg_path)
    seen: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        backfill_cli,
        "refresh_coverage_after_apply",
        lambda hive, quiet=False: seen.append((hive, quiet)),
    )

    # the neutral apply (a saved timeline plan also applies through it)
    plan_path = tmp_path / "timeline.bdlg"
    _invoke("backfill", "timeline", "--plan", str(plan_path))
    _invoke("backfill", "apply", str(plan_path), "--json")
    assert seen == [("", True)]

    # the complexity command calls whatever is registered on its hook list, after an apply only
    calls: list[tuple[str, bool]] = []
    monkeypatch.setattr(complexity_backfill, "APPLIED_HOOKS", [lambda h, q: calls.append((h, q))])
    monkeypatch.setattr(complexity_backfill, "enumerate_corpus", lambda cwd: [])
    monkeypatch.setattr(complexity_backfill, "resolve_actor", lambda *a, **k: "dev/x")
    monkeypatch.setattr(
        complexity_backfill,
        "apply_plan",
        lambda plan, **kw: {"applied_count": 0},
    )
    monkeypatch.setattr(complexity_backfill.registry, "hive_dir_for", lambda cfg, hive: tmp_path)
    saved = tmp_path / "complexity.json"
    _invoke("backfill-complexity", "--json", "--plan", str(saved))
    assert calls == []  # a preview applies nothing
    _invoke("backfill-complexity", "--apply", "--plan", str(saved), "--json")
    assert calls == [("", True)]


def test_the_registered_hook_is_the_backfill_cli_refresh() -> None:
    assert backfill_cli.refresh_coverage_after_apply in complexity_backfill.APPLIED_HOOKS
    assert complexity_backfill.APPLIED_HOOKS.count(backfill_cli.refresh_coverage_after_apply) == 1


# -- the real readers and the state path -----------------------------------------------------


def test_the_state_path_is_per_hive_under_the_bh_state_dir(
    cfg_path,  # noqa: F811 — the imported fixture
    monkeypatch,
    tmp_path,
) -> None:
    cfg = backfill_cli.config.load()
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    monkeypatch.chdir(one)
    path, ref = backfill_cli._coverage_target(cfg, "", one)
    assert path.name == "coverage.json" and ref == "main"
    assert path.parent.parent == tmp_path / "state" / "backfill"  # <home>/state/backfill/<hive>
    assert backfill_cli._coverage_target(cfg, "", two)[0] != path  # a path-keyed hive, never shared

    entry = {"provider": "github", "org": "acme", "repo": "widgets"}
    keyed = backfill_coverage.state_path(entry, one, tmp_path)
    assert keyed == tmp_path / "state" / "backfill" / "github-acme-widgets" / "coverage.json"


def test_the_real_readers_walk_git_once_and_the_configured_doc_roots(tmp_path) -> None:
    import subprocess

    from beadhive_backfill.coverage import CoverageSettings

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )

    git("init", "-q", "-b", "main")
    (tmp_path / "docs" / "design").mkdir(parents=True)
    (tmp_path / "docs" / "design" / "x.md").write_text("# x\n")
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "n.md").write_text("# n\n")
    git("add", "-A")
    git("commit", "-q", "-m", "feat: first (wd-1)")
    git("commit", "-q", "--allow-empty", "-m", "chore: second")

    class Reader:
        def export(self) -> list[Any]:
            return []

    settings = CoverageSettings(doc_roots=("docs", "notes"))
    readers = backfill_coverage.HiveReaders(
        tmp_path, reader=Reader(), ref="main", settings=settings
    )
    assert [c.subject for c in readers.commits()] == ["chore: second", "feat: first (wd-1)"]
    assert readers.docs() == ["docs/design/x.md", "notes/n.md"]
    assert list(readers.beads()) == []
