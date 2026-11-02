"""``--since-cursor`` and continuous backfill over the in-memory fake bd.

The bd store is :class:`FakeBd` behind the real StoreLoader; the history sources (the events
table, first-parent lands, commit dates) are a growing fixture hive behind the two timeline seams;
git for the ``commits`` pass is a throwaway repository. Nothing touches a live hive and nothing
reads a clock: the loader and the fixtures are ticked or constant.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from tests.test_backfill_bd import FakeBd, Ticks
from tests.test_backfill_bd import cfg_path as cfg_path  # noqa: F401 — fixture resolved by name
from typer.testing import CliRunner

from beadhive import backfill_cli, backfill_continuous, backfill_coverage, cli
from beadhive.backfill_bd import BdLoadError, BdStoreLoader
from beadhive_backfill import (
    BackfillEngine,
    JsonFileAuditSink,
    PassCursor,
    Plan,
    TimelinePass,
    read_cursor,
    write_cursor,
)
from beadhive_backfill.merges import parse_first_parent
from beadhive_backfill.run_journals import RunJournalRead
from beadlog import ArchiveReader, instant
from beadlog_sources import CommitDates, HistoryEvent, HistoryRead

ENV = {"COLUMNS": "120", "BH_SKIP_SETUP_CHECK": "1", "NO_COLOR": "1"}
ENTRY = {"provider": "github", "org": "acme", "repo": "widgets", "prefix": "wd"}
ON = {"backfill": {"continuous": {"enabled": True}}}


def _sha(n: int) -> str:
    return f"{n:02x}" * 20


class World:
    """A hive that grows by merges: bd rows, events-table rows and first-parent lands, served to
    the timeline pass through fakes that also make the narrow reads of an incremental run."""

    def __init__(self) -> None:
        self.store = FakeBd()
        self.events: list[HistoryEvent] = []
        self.log: list[str] = []
        self.order: list[str] = []
        self.head = _sha(0)
        self.day = 10
        self.calls: dict[str, int] = {}

    def merge(self, bead: str) -> None:
        """Create, claim, close and land ``bead`` one day after the previous merge."""
        self.day += 1
        at = f"2026-08-{self.day:02d}T10:00:00Z"
        self.store.rows[bead] = {
            "id": bead,
            "issue_type": "task",
            "status": "closed",
            "title": f"Bead {bead}",
            "priority": 2,
            "labels": [],
            "created_at": "2026-08-01T09:00:00Z",
            "updated_at": at,
            "closed_at": at,
            "assignee": "dev/a",
        }
        self.events += [
            HistoryEvent(f"c-{bead}", bead, "created", "2026-08-01T09:00:00Z", actor="dev/a"),
            HistoryEvent(
                f"a-{bead}",
                bead,
                "claimed",
                f"2026-08-{self.day:02d}T08:00:00Z",
                actor="dev/a",
                assignee="dev/a",
                new_status="in_progress",
                changes={"assignee": "dev/a", "status": "in_progress"},
            ),
            HistoryEvent(f"x-{bead}", bead, "closed", at, actor="dev/a", new_status="closed"),
        ]
        land = _sha(len(self.order) + 1)
        landed = f"2026-08-{self.day:02d}T09:00:00+00:00"
        self.log.insert(0, f"{land}\t{landed}\tchore(merge): bead {bead}")
        self.order.append(land)
        self.head = land

    # -- TimelineReaders
    def _count(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1

    def history(self) -> HistoryRead:
        self._count("history")
        return self._read(self.events)

    def lands(self):
        return parse_first_parent(self.log)

    def commit_dates(self, shas: Sequence[str]) -> CommitDates:
        return CommitDates({}, tuple(shas))

    def run_journals(self) -> RunJournalRead:
        return RunJournalRead()

    # -- IncrementalReaders
    def head_sha(self) -> str:
        return self.head

    @staticmethod
    def _read(events) -> HistoryRead:
        items = tuple(sorted(events, key=lambda event: event.sort_key))
        return HistoryRead(items, rows=len(items))

    def history_since(self, at: str) -> HistoryRead:
        self._count("history_since")
        return self._read(e for e in self.events if instant(e.at) >= instant(at))

    def history_of(self, ids: Sequence[str]) -> HistoryRead:
        self._count("history_of")
        return self._read(e for e in self.events if e.bead in set(ids))

    def lands_since(self, sha: str):
        after = set(self.order[self.order.index(sha) + 1 :]) if sha in self.order else set()
        return parse_first_parent(line for line in self.log if line.split("\t")[0] in after)

    def reaches(self, sha: str) -> bool:
        return sha in self.order or sha == _sha(0)


class WorldReaders:
    """The :class:`~beadhive_backfill.IncrementalReaders` face of a :class:`World`."""

    def __init__(self, world: World) -> None:
        self.world = world

    def history(self):
        return self.world.history()

    def lands(self):
        return self.world.lands()

    def commit_dates(self, shas):
        return self.world.commit_dates(shas)

    def run_journals(self):
        return self.world.run_journals()

    def head(self):
        return self.world.head_sha()

    def reaches(self, sha):
        return self.world.reaches(sha)

    def history_since(self, at):
        return self.world.history_since(at)

    def history_of(self, ids):
        return self.world.history_of(ids)

    def lands_since(self, sha):
        return self.world.lands_since(sha)


@pytest.fixture
def world(cfg_path, monkeypatch, tmp_path):  # noqa: F811 — the imported fixture
    monkeypatch.chdir(tmp_path)
    hive = World()
    group = backfill_cli.hive_group(ENTRY, tmp_path)
    hive.group = group  # type: ignore[attr-defined]
    monkeypatch.setattr(backfill_cli, "_target", lambda name: (tmp_path, group))

    def loader(cwd: Path, *, group: str, pre_state_path: Path | None = None) -> BdStoreLoader:
        return BdStoreLoader(hive.store, group=group, clock=Ticks(), pre_state_path=pre_state_path)

    monkeypatch.setattr(backfill_cli, "store_loader", loader)
    monkeypatch.setattr(
        backfill_cli,
        "timeline_pass",
        lambda cwd, name, **kw: TimelinePass(WorldReaders(hive), **kw),
    )
    monkeypatch.setattr(
        backfill_cli,
        "timeline_pass_for",
        lambda cwd, entry, *, ref, **kw: TimelinePass(WorldReaders(hive), **kw),
    )
    return hive


def _invoke(*args: str, code: int = 0) -> Any:
    result = CliRunner().invoke(cli.app, list(args), env=ENV)
    assert result.exit_code == code, result.output
    return result


def _entry() -> Any:
    """The registry entry the CLI resolves for the fixture config (none: a bare directory)."""
    return backfill_cli.registry.current_hive(backfill_cli.config.load())


def _cursor_file(tmp_path: Path) -> Path:
    return backfill_cli._cursor_file("", tmp_path)


def _lifecycle(world: World, bead: str) -> dict[str, Any]:
    metadata = world.store.rows[bead].get("metadata", {})
    return {k: v for k, v in metadata.items() if k.startswith("lifecycle.")}


def _apply_full_since_cursor(world: World, tmp_path: Path) -> Path:
    """What a first incremental timeline run earns: a full run applied through the envelope and
    the cursor it recorded (the published ``timeline`` verb has no ``--since-cursor``)."""
    pass_ = backfill_cli.timeline_pass_for(tmp_path, None, ref="main", incremental=True)
    engine = BackfillEngine(groups=[world.group], passes=[pass_])  # type: ignore[attr-defined]
    plan = engine.plan(pass_, backfill_cli.store_loader(tmp_path, group=world.group.id))  # type: ignore[attr-defined]
    assert plan.report["incremental"]["mode"] == "full"
    audit = tmp_path / "first.audit.json"
    loader = backfill_cli.store_loader(
        tmp_path,
        group=world.group.id,
        pre_state_path=tmp_path / "first.pre-state.jsonl",  # type: ignore[attr-defined]
    )
    engine.apply(plan, loader, loader, JsonFileAuditSink(audit), pre_state=loader)
    assert backfill_continuous.record_cursor(_cursor_file(tmp_path), plan, audit) is not None
    return audit


# -- bh backfill commits --since-cursor and the cursor lifecycle -------------------------------


def _git(repo: Path, *args: str) -> str:
    when = "2026-09-01T10:00:00+00:00"
    env = {**os.environ, "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
    done = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env
    )
    return done.stdout.strip()


def _init(repo: Path) -> None:
    repo.mkdir(exist_ok=True)
    for command in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@example.com"],
        ["config", "user.name", "T"],
        ["config", "commit.gpgsign", "false"],
    ):
        _git(repo, *command)


def _commit(repo: Path, message: str) -> str:
    _git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def commits_hive(world, tmp_path, monkeypatch):
    """The fixture hive, with ``tmp_path/repo`` as the git checkout the commits pass reads."""
    repo = tmp_path / "repo"
    _init(repo)
    for bead in ("wd-1", "wd-2", "wd-3"):
        world.merge(bead)
    monkeypatch.setattr(backfill_cli, "_target", lambda name: (repo, world.group))
    return repo


def _linkage(world: World, bead: str) -> list[str]:
    raw = world.store.rows[bead].get("metadata", {}).get("git.commits")
    return json.loads(raw) if raw else []


def test_since_cursor_follows_the_commits_and_an_applied_plan_moves_the_cursor(
    world, commits_hive, tmp_path
) -> None:
    repo = commits_hive
    first = _commit(repo, "feat: first change (wd-1)")
    cursor_file = backfill_cli._cursor_file("", repo)
    assert (
        "incremental: full (no cursor recorded yet)"
        in _invoke("backfill", "commits", "--since-cursor").stdout
    )
    assert not cursor_file.exists()  # a dry run never moves a cursor

    plan = tmp_path / "first.bdlg"
    _invoke("backfill", "commits", "--since-cursor", "--plan", str(plan))
    _invoke("backfill", "commits", "--apply", "--plan", str(plan))
    cursor = read_cursor(cursor_file, "commits").cursor
    assert cursor is not None and cursor.land_sha == first and cursor.plan_hash
    assert _linkage(world, "wd-1") == [first]

    second = _commit(repo, "feat: second change (wd-2)")
    third = _commit(repo, "fix: third change (wd-3)")
    incremental, full = tmp_path / "incr.bdlg", tmp_path / "full.bdlg"
    summary = json.loads(
        _invoke(
            "backfill", "commits", "--since-cursor", "--json", "--plan", str(incremental)
        ).stdout
    )
    _invoke("backfill", "commits", "--json", "--plan", str(full))

    info = summary["report"]["incremental"]
    assert info["mode"] == "incremental" and info["warnings"] == []
    assert summary["report"]["options"]["since"] == first
    ids = {event.id for event in Plan.read(incremental).applicable_events()}
    assert len(ids) == 2 and ids == {event.id for event in Plan.read(full).applicable_events()}

    _invoke("backfill", "commits", "--apply", "--plan", str(incremental))
    assert _linkage(world, "wd-2") == [second] and _linkage(world, "wd-3") == [third]
    moved = read_cursor(cursor_file, "commits").cursor
    assert moved is not None and moved.land_sha == third
    after = json.loads(_invoke("backfill", "commits", "--since-cursor", "--json").stdout)
    assert after["changes"] == 0


def test_a_corrupt_cursor_file_warns_and_the_commits_run_is_full(
    world, commits_hive, tmp_path
) -> None:
    repo = commits_hive
    _commit(repo, "feat: first change (wd-1)")
    plan = tmp_path / "first.bdlg"
    _invoke("backfill", "commits", "--since-cursor", "--plan", str(plan))
    _invoke("backfill", "commits", "--apply", "--plan", str(plan))
    _commit(repo, "feat: second change (wd-2)")
    backfill_cli._cursor_file("", repo).write_text("{ not a cursor")

    result = CliRunner().invoke(
        cli.app, ["backfill", "commits", "--since-cursor", "--json"], env=ENV
    )

    assert result.exit_code == 0
    assert "corrupt" in result.output and "running in full" in result.output
    summary = json.loads(result.stdout)
    assert summary["report"]["incremental"]["mode"] == "full"
    assert "since" not in summary["report"]["options"]
    assert summary["changes"] == 1  # a full run: wd-2 is the only bead still to link


def test_a_commits_cursor_that_left_the_history_warns_and_the_run_is_full(
    world, commits_hive
) -> None:
    repo = commits_hive
    _commit(repo, "feat: first change (wd-1)")
    write_cursor(backfill_cli._cursor_file("", repo), PassCursor("commits", "ee" * 20))

    result = CliRunner().invoke(
        cli.app, ["backfill", "commits", "--since-cursor", "--json"], env=ENV
    )

    assert result.exit_code == 0 and "is not in the history" in result.output
    assert json.loads(result.stdout)["report"]["incremental"]["mode"] == "full"


def test_since_cursor_is_a_dry_run_option(world) -> None:
    result = _invoke("backfill", "commits", "--apply", "--since-cursor", code=2)
    assert "applies to the dry run" in result.output


def test_only_the_commits_pass_takes_since_cursor_the_published_timeline_verb_is_unchanged(
    world,
) -> None:
    for name, takes in (("commits", True), ("timeline", False), ("docs", False)):
        helped = CliRunner().invoke(cli.app, ["backfill", name, "--help"], env=ENV)
        assert ("--since-cursor" in helped.output) is takes, name


# -- continuous backfill: the post-merge trigger -----------------------------------------------


def _hook(world: World, tmp_path: Path, cfg: dict[str, Any], beads: Sequence[str], **kw: Any):
    backfill_continuous.on_work_merged(
        cfg, _entry(), tmp_path, "main", list(beads), seams=backfill_cli, home=tmp_path, **kw
    )


def _continuous(**continuous: Any) -> dict[str, Any]:
    return {"backfill": {"continuous": {"enabled": True, "passes": ["timeline"], **continuous}}}


def test_the_settings_are_off_by_default_and_a_bad_section_is_off() -> None:
    assert not backfill_continuous.settings({}).enabled
    assert not backfill_continuous.settings(
        {"backfill": {"continuous": {"enabled": "maybe"}}}
    ).enabled
    on = backfill_continuous.settings(ON)
    assert on.enabled and on.passes == ("commits", "timeline") and on.triggers == ("post-merge",)
    assert on.runs_on("post-merge") and not on.runs_on("sync")


def test_disabled_by_default_a_merge_reads_and_writes_nothing(world, tmp_path, monkeypatch) -> None:
    world.merge("wd-1")
    monkeypatch.setattr(
        backfill_cli, "store_loader", lambda *a, **k: pytest.fail("the store was touched")
    )

    _hook(world, tmp_path, {}, ["wd-1"])
    _hook(world, tmp_path, {"backfill": {"continuous": {"enabled": False}}}, ["wd-1"])
    _hook(world, tmp_path, _continuous(triggers=["sync"]), ["wd-1"])  # on, but not for a merge

    assert world.store.writes == [] and world.store.reads == 0


def test_enabled_a_merge_updates_the_lifecycle_and_the_coverage_cache(
    world, tmp_path, monkeypatch, capsys
) -> None:
    world.merge("wd-1")
    world.merge("wd-2")
    cfg = _continuous()
    cfg["backfill"]["coverage"] = {"enabled": True}
    refreshed: list[Path] = []
    monkeypatch.setattr(
        backfill_coverage, "refresh_cache", lambda *a, **k: refreshed.append(k["path"])
    )
    monkeypatch.setattr(
        world.store, "export_to", lambda path: pytest.fail("a corpus export was written")
    )

    _hook(world, tmp_path, cfg, ["wd-2"])

    out = capsys.readouterr()
    assert _lifecycle(world, "wd-2")["lifecycle.merged_at"] == "2026-08-12T09:00:00Z"
    assert _lifecycle(world, "wd-1") == {}  # only the merged bead moved
    assert out.out.strip() == "backfill (post-merge): timeline: 1 bead(s) updated"
    assert refreshed == [backfill_coverage.state_path(_entry(), tmp_path, tmp_path)]
    # no cursor existed, so the run was scoped to the merged bead and moved nothing
    assert not _cursor_file(tmp_path).exists()
    # the recovery export holds the bead it changed, not the corpus
    work = _cursor_file(tmp_path).parent / backfill_continuous.WORK_DIR
    with ArchiveReader(work / "timeline.pre-state.bdlg") as archive:
        assert archive.manifest.to_json()["counts"]["beads"] == 1
    assert json.loads((work / "timeline.audit.json").read_text())["state"] == "applied"


def test_a_second_merge_replaces_the_previous_runs_files(world, tmp_path) -> None:
    world.merge("wd-1")
    _hook(world, tmp_path, _continuous(), ["wd-1"])
    world.merge("wd-2")
    _hook(world, tmp_path, _continuous(), ["wd-2"])

    work = _cursor_file(tmp_path).parent / backfill_continuous.WORK_DIR
    assert sorted(path.name for path in work.iterdir()) == [
        "timeline.audit.json",
        "timeline.pre-state.bdlg",
    ]


def test_with_a_cursor_the_hook_follows_it_and_moves_it(world, tmp_path) -> None:
    world.merge("wd-1")
    _apply_full_since_cursor(world, tmp_path)
    before = read_cursor(_cursor_file(tmp_path), "timeline").cursor
    world.merge("wd-2")
    world.merge("wd-3")  # two merges, the hook is told only about the last

    _hook(world, tmp_path, _continuous(), ["wd-3"])

    assert _lifecycle(world, "wd-2") and _lifecycle(world, "wd-3")  # the cursor found wd-2 too
    after = read_cursor(_cursor_file(tmp_path), "timeline").cursor
    assert before is not None and after is not None and after.land_sha == world.head
    assert after.land_sha != before.land_sha


def test_a_failing_loader_is_a_warning_and_the_hook_returns(world, tmp_path, monkeypatch, capsys):
    world.merge("wd-1")

    def broken(cwd: Path, *, group: str, pre_state_path: Path | None = None) -> Any:
        raise BdLoadError("bd export failed; nothing was written")

    monkeypatch.setattr(backfill_cli, "store_loader", broken)

    _hook(world, tmp_path, _continuous(), ["wd-1"])  # does not raise

    err = capsys.readouterr().err
    assert "backfill timeline not updated: bd export failed" in err
    assert world.store.writes == []


def test_a_write_that_fails_midway_rolls_back_and_only_warns(world, tmp_path, capsys) -> None:
    world.merge("wd-1")
    world.store.fail = lambda op, bead: op == "update"

    _hook(world, tmp_path, _continuous(), ["wd-1"])

    assert "backfill timeline not updated" in capsys.readouterr().err
    assert _lifecycle(world, "wd-1") == {}


def test_an_unexpected_error_in_the_run_never_escapes(world, tmp_path, monkeypatch, capsys) -> None:
    def explode(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("surprise")

    monkeypatch.setattr(backfill_continuous, "run", explode)

    _hook(world, tmp_path, _continuous(), ["wd-1"])

    assert "did not run (the merge is unaffected): surprise" in capsys.readouterr().err


def test_one_failing_pass_does_not_stop_the_next(world, tmp_path, monkeypatch, capsys) -> None:
    world.merge("wd-1")

    def source_pass_for(*args: Any, **kwargs: Any) -> Any:
        raise BdLoadError("no git")

    monkeypatch.setattr(backfill_cli, "source_pass_for", source_pass_for)

    _hook(world, tmp_path, _continuous(passes=["commits", "timeline"]), ["wd-1"])

    assert "backfill commits not updated: no git" in capsys.readouterr().err
    assert _lifecycle(world, "wd-1")  # timeline still ran


def test_a_corrupt_cursor_in_the_hook_warns_and_the_merged_bead_is_still_updated(
    world, tmp_path, capsys
) -> None:
    world.merge("wd-1")
    world.merge("wd-2")
    path = _cursor_file(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("garbage")

    _hook(world, tmp_path, _continuous(), ["wd-2"])

    assert "corrupt" in capsys.readouterr().err
    assert _lifecycle(world, "wd-2") and _lifecycle(world, "wd-1") == {}  # scoped, never full


def test_a_merge_with_nothing_new_to_say_is_up_to_date(world, tmp_path, capsys) -> None:
    world.merge("wd-1")
    _hook(world, tmp_path, _continuous(), ["wd-1"])
    capsys.readouterr()
    writes = len(world.store.writes)

    _hook(world, tmp_path, _continuous(), ["wd-1"])

    assert capsys.readouterr().out.strip() == "backfill (post-merge): timeline: up to date"
    assert len(world.store.writes) == writes


# -- the commits pass in the hook ---------------------------------------------------------------


def test_the_commits_pass_appends_the_merges_own_commits_from_its_base(
    world, tmp_path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    _init(repo)
    _git(repo, "commit", "-q", "--allow-empty", "-m", "feat: old work (wd-1)")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "feat: the change (wd-2)")
    tip = _git(repo, "rev-parse", "HEAD")
    world.merge("wd-1")
    world.merge("wd-2")

    backfill_continuous.on_work_merged(
        _continuous(passes=["commits"]),
        _entry(),
        repo,
        "main",
        ["wd-2"],
        seams=backfill_cli,
        home=tmp_path,
        since=base,
    )

    assert json.loads(world.store.rows["wd-2"]["metadata"]["git.commits"]) == [tip]
    assert "git.commits" not in world.store.rows["wd-1"].get("metadata", {})  # before the base
    assert not _cursor_file(tmp_path).exists()


def test_the_commits_pass_never_starts_a_full_read_from_a_hook(world, tmp_path, capsys) -> None:
    repo = tmp_path / "repo"
    _init(repo)
    _git(repo, "commit", "-q", "--allow-empty", "-m", "feat: a (wd-1)")
    world.merge("wd-1")

    backfill_continuous.on_work_merged(
        _continuous(passes=["commits"]),
        _entry(),
        repo,
        "main",
        ["wd-1"],
        seams=backfill_cli,
        home=tmp_path,
        since=None,
    )

    assert "no usable cursor" in capsys.readouterr().err
    assert world.store.writes == []


# -- the sync trigger -----------------------------------------------------------------------


def test_the_sync_trigger_follows_the_cursor_and_is_off_unless_configured(
    world, tmp_path, capsys
) -> None:
    world.merge("wd-1")
    _apply_full_since_cursor(world, tmp_path)
    world.merge("wd-2")
    hives = [(_entry(), tmp_path, "main")]

    backfill_continuous.after_sync(
        _continuous(), tmp_path, backfill_cli, hives
    )  # post-merge only: a no-op
    assert _lifecycle(world, "wd-2") == {}

    backfill_continuous.after_sync(_continuous(triggers=["sync"]), tmp_path, backfill_cli, hives)

    assert _lifecycle(world, "wd-2")
    assert capsys.readouterr().out.strip() == "backfill (sync): timeline: 1 bead(s) updated"


def test_the_sync_trigger_without_a_cursor_says_so_and_changes_nothing(
    world, tmp_path, capsys
) -> None:
    world.merge("wd-1")

    backfill_continuous.after_sync(
        _continuous(triggers=["sync"]), tmp_path, backfill_cli, [(_entry(), tmp_path, "main")]
    )

    assert "no usable cursor" in capsys.readouterr().err
    assert world.store.writes == []


def test_the_bh_hive_sync_glue_is_a_noop_unless_enabled(cfg_path, monkeypatch) -> None:
    class Plan:
        pulled = ["github/acme/widgets"]

    monkeypatch.setattr(
        backfill_continuous, "after_sync", lambda *a, **k: pytest.fail("ran while disabled")
    )
    cli._backfill_after_sync(Plan())  # the fixture config has no backfill.continuous

    class Nothing:
        pulled: list[str] = []

    cli._backfill_after_sync(Nothing())


# -- the read cache ----------------------------------------------------------------------------


def test_the_read_cache_serves_reads_until_the_first_write() -> None:
    store = FakeBd([{"id": "wd-1", "title": "t", "status": "open", "issue_type": "task"}])
    port = backfill_continuous.CachedReadPort(store)

    first, second = port.export_rows(), port.export_rows()
    assert first == second and store.reads == 1

    port.apply_changes(
        "wd-1",
        fields={"title": "u"},
        add_labels=[],
        remove_labels=[],
        status="",
        metadata=None,
        force=False,
    )
    assert port.export_rows()[0]["title"] == "u" and store.reads == 2
    assert port.target == "bd:fake"  # everything else passes through
