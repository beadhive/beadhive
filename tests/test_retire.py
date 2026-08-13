"""Tests for ws.retire — worktree teardown helper for the retire flow.

Each test provisions a real temporary git repo + managed worktrees (the same pattern used
by test_worktree.py's _ensure_hive helper), monkeypatches config.load so that teardown
helpers resolve the right hive, then exercises teardown_worktrees under three scenarios:

  - clean worktree  → removed, appears in result.removed, parent dirs reclaimed
  - dirty worktree  → skipped, appears in result.dirty, dir still exists
  - dry_run=True    → appears in result.removed but nothing is actually removed
"""

from __future__ import annotations

import os
from pathlib import Path

from beadhive import config, worktree
from beadhive.retire import TeardownResult, teardown_worktrees
from beadhive.run import run

_CLEAN_ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _git(*args, cwd):
    run(["git", *args], cwd=str(cwd), check=True, capture=True, env=_CLEAN_ENV)


def _store_answers(monkeypatch, status="closed"):
    """Make this hive's bead store answer.

    These fixtures stand up a real git repo and NO bead store, which after bh-167s0 is an
    UNKNOWN row — `worktree.remove` refuses it rather than deleting a worktree whose contents
    bh cannot describe, and teardown records that as `failed`. That path has its own test at the
    bottom of this file; the three scenarios these fixtures exist for are clean/dirty/dry-run.
    """

    def fake_json(args, cwd, **kw):
        if args[:1] == ["list"]:
            return [{"id": "seed"}]
        if args[:1] == ["show"]:
            return {"id": args[1], "status": status, "close_reason": "merged"}
        return None

    monkeypatch.setattr(worktree.bd, "json", fake_json)


def _retire_hive(tmp_path, monkeypatch):
    """A real one-commit hive clone with isolated HOME + monkeypatched config.load.

    Returns (cfg, entry, repo_path) — cfg is the same dict that config.load() will return
    so worktree.ensure and teardown_worktrees see a consistent view.
    """
    ws_root = tmp_path / "ws"
    repo = ws_root / "github" / "myorg" / "myrepo"
    repo.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "t@example.com", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    (repo / "f.txt").write_text("hi")
    _git("add", "f.txt", cwd=repo)
    _git("commit", "-qm", "init", cwd=repo)

    wts_root = tmp_path / "wts"
    monkeypatch.setenv("GIT_WORKSPACE", str(ws_root))
    monkeypatch.setenv("WS_WORKTREES", str(wts_root))

    # Isolate HOME so global ~/.gitconfig doesn't interfere with git ops.
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    entry = {"provider": "github", "org": "myorg", "repo": "myrepo", "prefix": "mr"}
    cfg = {"managed_repos": [entry]}

    # Patch config.load so teardown_worktrees (and worktree.remove inside it) resolves
    # the hive correctly without needing an actual ~/.ws/config.yaml on disk.
    monkeypatch.setattr("beadhive.config.load", lambda: cfg)

    return cfg, entry, repo


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_teardown_clean_worktree_removes_it(tmp_path, monkeypatch):
    """A clean managed worktree is removed and its path appears in result.removed."""
    cfg, _entry, _repo = _retire_hive(tmp_path, monkeypatch)
    _, target, _ = worktree.ensure(cfg, "mr", "retire-test")
    _store_answers(monkeypatch)

    result = teardown_worktrees("mr")

    assert isinstance(result, TeardownResult)
    assert str(target) in result.removed
    assert not target.exists()
    assert result.dirty == []


def test_teardown_clean_worktree_reclaims_empty_dirs(tmp_path, monkeypatch):
    """After removing the last worktree, empty triplet dirs under the shadow root are
    reclaimed and reported in result.reclaimed_dirs."""
    cfg, _entry, _repo = _retire_hive(tmp_path, monkeypatch)
    _, target, _ = worktree.ensure(cfg, "mr", "retire-test")
    _store_answers(monkeypatch)

    # Confirm the shadow root exists before teardown.
    wts_root = config.worktrees_root().resolve()
    assert (wts_root / "github").exists()

    result = teardown_worktrees("mr")

    # At least one parent dir should have been reclaimed.
    assert result.reclaimed_dirs, "expected at least one empty dir to be reclaimed"
    # All reclaimed dirs must no longer exist.
    for d in result.reclaimed_dirs:
        assert not Path(d).exists(), f"{d} should have been removed"


def test_teardown_dirty_worktree_is_skipped_and_flagged(tmp_path, monkeypatch):
    """A worktree with uncommitted changes is not removed; it appears in result.dirty."""
    cfg, _entry, _repo = _retire_hive(tmp_path, monkeypatch)
    _, target, _ = worktree.ensure(cfg, "mr", "retire-test")

    # Create an untracked file to make the worktree dirty.
    (target / "unsaved.txt").write_text("work in progress")

    result = teardown_worktrees("mr")

    assert str(target) in result.dirty
    assert target.exists()  # not removed
    assert result.removed == []


def test_teardown_dry_run_previews_without_removing(tmp_path, monkeypatch):
    """dry_run=True populates result.removed with what would be removed but leaves
    the worktree dir untouched."""
    cfg, _entry, _repo = _retire_hive(tmp_path, monkeypatch)
    _, target, _ = worktree.ensure(cfg, "mr", "retire-test")

    result = teardown_worktrees("mr", dry_run=True)

    assert str(target) in result.removed
    assert target.exists()  # dry_run: nothing actually removed
    assert result.dirty == []
    assert result.reclaimed_dirs == []  # reclaimed_dirs only populated on real removal


# ---------------------------------------------------------------------------
# Generic plugin notify loop (bead .7) — WARN-ONLY, dry-run does not record
# ---------------------------------------------------------------------------

from types import SimpleNamespace  # noqa: E402

from beadhive import config as _config  # noqa: E402
from beadhive import plugins, registry, retire, safety  # noqa: E402
from beadhive.safety import RetireVerdict  # noqa: E402


def _retire_plugin_setup(tmp_path, monkeypatch):
    """A SAFE, worktree-free hive wired so ``retire_hive`` reaches the plugin notify loop."""
    cfg, entry, repo = _retire_hive(tmp_path, monkeypatch)
    monkeypatch.setattr(registry, "resolve_hive", lambda c, hive: entry)
    monkeypatch.setattr(
        safety,
        "assess_retire",
        lambda p: SimpleNamespace(verdict=RetireVerdict.SAFE, reasons=[]),
    )
    monkeypatch.setattr(registry, "unregister", lambda *a, **k: None)
    return cfg, entry, repo


def test_plugins_notified_includes_orca_when_enabled(tmp_path, monkeypatch):
    _retire_plugin_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(_config, "orca_enabled", lambda c, e=None: True)

    plan = retire.retire_hive("mr")

    assert plan.plugins_notified == ["orca"]


def test_plugins_not_notified_when_orca_disabled(tmp_path, monkeypatch):
    _retire_plugin_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(_config, "orca_enabled", lambda c, e=None: False)

    plan = retire.retire_hive("mr")

    assert plan.plugins_notified == []


def test_dry_run_does_not_append_to_plugins_notified(tmp_path, monkeypatch):
    _retire_plugin_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(_config, "orca_enabled", lambda c, e=None: True)

    plan = retire.retire_hive("mr", dry_run=True)

    assert plan.plugins_notified == []


def test_retire_never_writes_orca_data(tmp_path, monkeypatch):
    """orca has no de-registration verb: retire is WARN-ONLY and never touches orca-data.json."""
    _retire_plugin_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(_config, "orca_enabled", lambda c, e=None: True)
    data = tmp_path / "orca-data.json"
    data.write_text('{"repos": [{"path": "/x"}]}')
    monkeypatch.setattr(_config, "orca_data_path", lambda c=None: data)
    before = data.read_text()

    retire.retire_hive("mr")

    assert data.read_text() == before  # file untouched under any flag combination


def test_raising_on_retire_hook_is_fenced(tmp_path, monkeypatch):
    _retire_plugin_setup(tmp_path, monkeypatch)
    import typer

    def boom(clone_path, cfg, entry):
        raise RuntimeError("plugin exploded")

    fake = plugins.Plugin(
        name="boom",
        cli=typer.Typer(),
        enabled=lambda cfg, entry: True,
        on_retire=boom,
    )
    monkeypatch.setattr(plugins, "registry", lambda: [fake])

    plan = retire.retire_hive("mr")

    # Fenced: retire completed, the failing plugin is not recorded as notified.
    assert plan.unregistered is True
    assert plan.plugins_notified == []


def test_teardown_records_a_worktree_whose_bead_could_not_be_resolved_as_failed(
    tmp_path, monkeypatch
):
    """bh-167s0 reaching retire, and it lands in the right channel rather than being smoothed
    over. Retiring a hive DELETES the clone; if its bead store cannot be read, bh cannot say
    whether these worktrees hold unmerged work, so `remove` refuses and teardown records it in
    `failed` — the field whose whole purpose is letting the orchestrator gate "instead of
    silently proceeding to delete a clone a live worktree references"."""
    cfg, _entry, _repo = _retire_hive(tmp_path, monkeypatch)
    _, target, _ = worktree.ensure(cfg, "mr", "retire-unknown")
    monkeypatch.setattr(worktree.bd, "json", lambda args, cwd, **kw: None)

    result = teardown_worktrees("mr")

    assert str(target) in result.failed
    assert result.removed == []
    assert target.exists()


# ---- the hive-level fact is asked once, not once per worktree (bh-ioub2) --------------------


def test_teardown_probes_the_store_once_for_the_hive_not_once_per_worktree(tmp_path, monkeypatch):
    """Measured by COUNTING bd invocations, as the bead asks, rather than by reading the code.

    `worktree.remove`'s UNKNOWN preflight (bh-167s0) asks whether the hive's bead store can be
    read, and `teardown_worktrees` calls `remove` once per worktree — so that one hive-level
    fact was re-probed N times. 28 times for the agentguides/runtime hive that motivated
    bh-167s0, through `bd.json`, which has NO timeout, against a store that is by that bead's own
    premise slow or refusing. An unbounded bd call in a loop is the exact shape bh-toitp exists
    to eliminate, reintroduced by the fix next door.
    """
    cfg, _entry, _repo = _retire_hive(tmp_path, monkeypatch)
    targets = [worktree.ensure(cfg, "mr", f"retire-{i}")[1] for i in range(4)]

    probes: list[str] = []
    shows: list[str] = []

    def fake_json(args, cwd, **kw):
        if args[:1] == ["list"]:
            probes.append(str(cwd))
            return [{"id": "seed"}]
        if args[:1] == ["show"]:
            shows.append(args[1])
            return {"id": args[1], "status": "closed", "close_reason": "merged"}
        return None

    monkeypatch.setattr(worktree.bd, "json", fake_json)

    result = teardown_worktrees("mr")

    assert len(result.removed) == len(targets)
    # THE criterion: O(1) store probes for the hive, not O(N).
    assert len(probes) == 1, f"{len(probes)} store probes for {len(targets)} worktrees: {probes}"
    # The per-BEAD lookups are genuinely per row and stay O(N) — each row asks about a DIFFERENT
    # bead, so there is no hive-level fact to hoist. Bounded here rather than pinned exactly:
    # each row currently costs TWO `bd show`s of the same id, because `bead_and_parent` resolves
    # the parent link (`_parent_link_base`) independently of `_bead_statuses_for_entry`. That
    # duplication is real and outside this bead's scope (which is store PROBES); the bound stops
    # it silently becoming three, or becoming quadratic.
    assert len(shows) <= 2 * len(targets), shows
    assert len(shows) >= len(targets)


def test_the_refusal_is_unchanged_by_the_cache(tmp_path, monkeypatch):
    """A cost fix, not a policy change: an unreadable store must still refuse every removal,
    and the memo must not turn one probe into one permission."""
    cfg, _entry, _repo = _retire_hive(tmp_path, monkeypatch)
    targets = [worktree.ensure(cfg, "mr", f"refuse-{i}")[1] for i in range(3)]
    monkeypatch.setattr(worktree.bd, "json", lambda args, cwd, **kw: None)

    result = teardown_worktrees("mr")

    assert result.removed == []
    assert len(result.failed) == len(targets)
    assert all(t.exists() for t in targets)


def test_the_store_probe_cache_does_not_outlive_the_command(tmp_path, monkeypatch):
    """A CONTEXT, not a process-lifetime memo. `worktree status`'s own help promises "the
    pre-flight never uses stale data"; a memo that outlived the command would break exactly that
    inside a long-lived process — `bh mcp serve` holds one for days."""
    probes: list[str] = []
    monkeypatch.setattr(worktree, "_probe_store", lambda main: probes.append(str(main)) or "")

    worktree._store_readable(tmp_path)
    worktree._store_readable(tmp_path)
    assert len(probes) == 2  # uncached outside a block

    with worktree.store_probe_cache():
        worktree._store_readable(tmp_path)
        worktree._store_readable(tmp_path)
    assert len(probes) == 3  # one more, shared for the whole block

    worktree._store_readable(tmp_path)
    assert len(probes) == 4  # …and the block's answer did not survive it


def test_the_cache_is_keyed_per_hive(tmp_path, monkeypatch):
    """Two hives are two facts. A memo that collapsed them would report one hive's readability
    for another's — the same class of confident wrong answer this batch exists to remove."""
    calls: list[str] = []
    monkeypatch.setattr(worktree, "_probe_store", lambda main: calls.append(str(main)) or "")

    with worktree.store_probe_cache():
        worktree._store_readable(tmp_path / "a")
        worktree._store_readable(tmp_path / "b")
        worktree._store_readable(tmp_path / "a")

    assert calls == [str(tmp_path / "a"), str(tmp_path / "b")]
