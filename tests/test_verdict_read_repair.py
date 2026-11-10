"""A verdict pointer lost after a completed-green run heals from the manifest (GitHub #89, #110).

The run manifest is the only authority; ``validation/verdicts/<tree>/<cmd>.json`` is derived from
it. A crash, OOM kill or SIGKILL between ``validation_records.finish_run`` (green) and
``validation_ledger.record`` -> ``_sync_index`` used to throw that green away: ``verdict`` missed
on the absent pointer and the next boundary re-ran the whole gate. Reads now repair the pointer
from a retained completed, qualifying-green, fresh manifest for exactly that tree and command
hash — and from nothing else — and ``bh work validation reindex`` is the operator recovery path.
"""

from __future__ import annotations

import datetime
import json
import subprocess
import time

import pytest
from typer.testing import CliRunner

from beadhive import host, validation_ledger, validation_records, work, worktree

CMD = "just check"
KEY = validation_ledger.cmd_hash(CMD)


def _git(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def store(tmp_path, monkeypatch):
    repo = tmp_path / "ws" / "github" / "org" / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    monkeypatch.setenv("GIT_WORKSPACE", str(tmp_path / "ws"))
    host.mint_if_needed()
    return {"provider": "github", "org": "org", "repo": "repo"}, repo


def _pointer(entry, tree="tree", key=KEY):
    path = validation_ledger._verdict_path(entry, tree, key)
    assert path is not None
    return path


def _finished(repo, *, tree="tree", command=CMD, exit_code=0, reason="command_exit", **finish):
    """A run taken through ``finish_run`` with NO ``record`` after it: the crash window."""
    run = validation_records.begin_run(
        repo,
        bead="bh-x",
        phase="validation",
        branch="main",
        worktree=repo,
        sha=tree,
        tree=tree,
        command_hash=validation_ledger.cmd_hash(command),
        command=command,
    )
    assert run is not None
    done = validation_records.finish_run(
        repo, run["run_id"], exit_code=exit_code, reason=reason, **finish
    )
    assert done is not None
    return done


def _rewrite(repo, run):
    validation_records._atomic_json(
        repo / ".git/bh/validation/runs" / run["run_id"] / "manifest.json", run
    )


# ---- read-repair -------------------------------------------------------------------------------


def test_crash_after_green_manifest_reuses_the_green_without_rerunning(tmp_path, monkeypatch):
    """The #89 shape end to end: the gate ran green, the owner died before the pointer sync, the
    invocation failed — and the next reuse boundary still hits instead of re-running the gate."""
    ws_root = tmp_path / "ws"
    repo = ws_root / "github" / "myorg" / "myrepo"
    repo.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "t@example.com", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    (repo / "f.txt").write_text("hi")
    _git("add", "f.txt", cwd=repo)
    _git("commit", "-qm", "init", cwd=repo)
    monkeypatch.setenv("GIT_WORKSPACE", str(ws_root))
    monkeypatch.setenv("BH_WORKTREES", str(tmp_path / "wts"))
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    host.mint_if_needed()
    entry = {"provider": "github", "org": "myorg", "repo": "myrepo", "prefix": "mr"}
    cfg = {"managed_repos": [entry]}
    log = tmp_path / "runs.log"
    cmd = f"sh -c 'echo ran >> {log}; exit 0'"

    class OwnerKilled(BaseException):
        """Stands in for SIGSEGV / OOM kill / SIGKILL between the two writes."""

    with monkeypatch.context() as crash:
        crash.setattr(
            validation_ledger, "record", lambda *_a, **_k: (_ for _ in ()).throw(OwnerKilled())
        )
        with pytest.raises(OwnerKilled):  # the finish invocation itself still fails
            worktree.clean_checkout(entry, "main", cmd, cfg=cfg)
    assert len(log.read_text().splitlines()) == 1

    tree = validation_ledger.tree_of(entry, "main")
    run = validation_records.latest_run(
        repo, tree=tree, command_hash=validation_ledger.cmd_hash(cmd)
    )
    assert run is not None and validation_records.is_qualifying_green(run)  # recorded green
    pointer = _pointer(entry, tree, validation_ledger.cmd_hash(cmd))
    assert not pointer.exists()  # the crash window: no derived pointer

    assert worktree.clean_checkout(entry, "main", cmd, cfg=cfg, reuse=True) == 0
    assert len(log.read_text().splitlines()) == 1  # reused — the gate did not run again
    assert json.loads(pointer.read_text())["run_id"] == run["run_id"]


def test_missing_pointer_beside_a_green_manifest_is_rebuilt_on_read(store):
    entry, repo = store
    done = _finished(repo)
    assert not _pointer(entry).exists()

    hit = validation_ledger.green_verdict(entry, "tree", CMD)
    assert hit is not None and hit["run_id"] == done["run_id"] and hit["rc"] == 0
    assert json.loads(_pointer(entry).read_text())["run_id"] == done["run_id"]


def test_pointer_left_on_the_previous_green_heals_to_the_newer_green(store):
    entry, repo = store
    validation_ledger.record(entry, "tree", CMD, 0)
    older = json.loads(_pointer(entry).read_text())["run_id"]
    newer = _finished(repo)  # a later green whose own pointer sync never happened
    assert newer["run_id"] != older

    hit = validation_ledger.green_verdict(entry, "tree", CMD)
    assert hit is not None and hit["run_id"] == newer["run_id"]
    assert json.loads(_pointer(entry).read_text())["run_id"] == newer["run_id"]


def _red(repo):
    _finished(repo, exit_code=1)


def _stale(repo):
    run = _finished(repo)
    old = datetime.datetime.fromtimestamp(time.time() - 3 * 24 * 3600, datetime.UTC)
    run["finished_at"] = old.isoformat()
    _rewrite(repo, run)


def _running(repo):
    validation_records.begin_run(
        repo,
        bead="bh-x",
        phase="validation",
        branch="main",
        worktree=repo,
        sha="tree",
        tree="tree",
        command_hash=KEY,
        command=CMD,
    )


def _interrupted(repo):
    _finished(repo, exit_code=None, reason="interrupted")


def _non_qualifying(repo):
    run = _finished(repo)
    run["signal"] = 15  # contradictory: never qualifying green
    _rewrite(repo, run)


def _other_tree(repo):
    _finished(repo, tree="other-tree")


def _other_command(repo):
    _finished(repo, command="just other")


@pytest.mark.parametrize(
    "plant",
    [_red, _stale, _running, _interrupted, _non_qualifying, _other_tree, _other_command],
    ids=["red", "stale", "running", "interrupted", "non-qualifying", "tree", "command"],
)
def test_a_missing_pointer_never_heals_from_anything_but_a_qualifying_green(store, plant):
    entry, repo = store
    plant(repo)

    assert validation_ledger.green_verdict(entry, "tree", CMD) is None
    hit = validation_ledger.verdict(entry, "tree", CMD)
    assert hit is None or not validation_records.is_qualifying_green(hit)
    assert not _pointer(entry).exists()
    for tree, key in (("other-tree", KEY), ("tree", validation_ledger.cmd_hash("just other"))):
        assert not _pointer(entry, tree, key).exists()


def test_release_await_still_tells_red_from_missing(store):
    """``verdict`` stays the red-vs-missing seam ``release await`` polls."""
    entry, repo = store
    assert validation_ledger.verdict(entry, "tree", CMD) is None  # missing: keep waiting
    _finished(repo, exit_code=1)
    red = validation_ledger.verdict(entry, "tree", CMD)
    assert red is not None and red["verdict"] == "red"  # red: refuse now
    assert not _pointer(entry).exists()


# ---- operator recovery: reindex ------------------------------------------------------------------


def _actions(report):
    return {(item.tree, item.command_hash): item.action for item in report}


def test_reindex_rebuilds_only_from_green_reports_refusals_and_is_idempotent(store):
    entry, repo = store
    green = _finished(repo, tree="green-tree")
    _finished(repo, tree="red-tree", exit_code=1)
    _finished(repo, tree="none-tree", exit_code=None, reason="interrupted")
    validation_ledger.record(entry, "revoked-tree", CMD, 0)  # a pointer...
    _finished(repo, tree="revoked-tree", exit_code=1)  # ...a later red no longer supports

    planned = validation_ledger.reindex(entry, dry_run=True)
    assert planned is not None
    expected = {
        ("green-tree", KEY): "rebuilt",
        ("red-tree", KEY): "refused",
        ("none-tree", KEY): "refused",
        ("revoked-tree", KEY): "revoked",
    }
    assert _actions(planned) == expected
    reasons = {item.tree: item.reason for item in planned}
    assert "red" in reasons["red-tree"] and "interrupted" in reasons["none-tree"]
    # A dry run writes and removes nothing.
    assert not _pointer(entry, "green-tree").exists()
    assert _pointer(entry, "revoked-tree").exists()

    applied = validation_ledger.reindex(entry)
    assert applied is not None and _actions(applied) == expected
    assert json.loads(_pointer(entry, "green-tree").read_text())["run_id"] == green["run_id"]
    for tree in ("red-tree", "none-tree", "revoked-tree"):
        assert not _pointer(entry, tree).exists()

    again = validation_ledger.reindex(entry)
    assert again is not None
    assert _actions(again) == {
        **expected,
        ("green-tree", KEY): "unchanged",
        ("revoked-tree", KEY): "refused",
    }


def test_reindex_tree_filter_touches_only_that_tree(store):
    entry, repo = store
    _finished(repo, tree="a-tree")
    _finished(repo, tree="b-tree")
    report = validation_ledger.reindex(entry, tree="a-tree")
    assert report is not None and _actions(report) == {("a-tree", KEY): "rebuilt"}
    assert _pointer(entry, "a-tree").exists() and not _pointer(entry, "b-tree").exists()


def test_reindex_on_a_store_with_no_runs_reports_nothing(store):
    entry, _repo = store
    assert validation_ledger.reindex(entry) == []


def test_validation_reindex_verb_reports_json_and_dry_run(store, monkeypatch):
    entry, repo = store
    green = _finished(repo, tree="green-tree")
    _finished(repo, tree="red-tree", exit_code=1)
    monkeypatch.setattr("beadhive.registry.resolve_hive", lambda _cfg, _hive: entry)
    runner = CliRunner()

    dry = runner.invoke(work.app, ["validation", "reindex", "--hive", "x", "--dry-run", "--json"])
    assert dry.exit_code == 0, dry.output
    doc = json.loads(dry.output)
    assert doc["dry_run"] is True
    assert doc["counts"]["rebuilt"] == 1 and doc["counts"]["refused"] == 1
    assert not _pointer(entry, "green-tree").exists()

    applied = runner.invoke(work.app, ["validation", "reindex", "--hive", "x"])
    assert applied.exit_code == 0, applied.output
    assert "rebuilt" in applied.output and "red" in applied.output
    assert json.loads(_pointer(entry, "green-tree").read_text())["run_id"] == green["run_id"]

    usage = runner.invoke(work.app, ["validation", "reindex", "--rev", "a", "--tree", "b"])
    assert usage.exit_code == 2


@pytest.mark.parametrize(
    "tamper",
    [
        lambda p: {**p, "run_id": "run-missing"},  # names no retained manifest
        lambda p: {**p, "at": p["at"] - 60},  # the right run, a disagreeing timestamp
        lambda p: {**p, "rc": "0"},  # malformed
    ],
    ids=["planted-run", "disagreeing-at", "malformed-rc"],
)
def test_a_present_but_disagreeing_pointer_is_not_repaired(store, tamper):
    """Only the two crash shapes heal (no pointer, or the previous green's); anything else that
    disagrees with the manifest stays a miss and is left for the operator's reindex."""
    entry, _repo = store
    validation_ledger.record(entry, "tree", CMD, 0)
    pointer = _pointer(entry)
    tampered = tamper(json.loads(pointer.read_text()))
    pointer.write_text(json.dumps(tampered))

    assert validation_ledger.green_verdict(entry, "tree", CMD) is None
    assert json.loads(pointer.read_text()) == tampered
