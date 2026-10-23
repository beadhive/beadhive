"""Integration: registering a Dolt remote never leaves ``sync.remote`` in tracked config or Git
history (bh-j75nl).

bd 1.3.0's ``dolt remote add`` writes ``sync.remote`` into a tracked ``.beads/config.yaml`` and
commits it as ``bd: update sync.remote``. Real ``bd`` + real Git in tmp dirs, ``file://`` Dolt
remotes only — no network, no credentials.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from beadhive import config_leak_guard, dolt_remote, engine
from harness.beads import bd, init_embedded, skip_if_no_bd
from harness.world import git

pytestmark = [pytest.mark.integration, skip_if_no_bd]

_SYNC = re.compile(r"^sync\.remote\s*:", re.MULTILINE)


def _hive(tmp_path) -> Path:
    repo = tmp_path / "code"
    repo.mkdir()
    git("init", "-q", "-b", "main", cwd=repo)
    git("config", "user.email", "t@t.dev", cwd=repo)
    git("config", "user.name", "T", cwd=repo)
    (repo / "README.md").write_text("hi\n")
    git("add", ".", cwd=repo)
    git("commit", "-q", "-m", "init", cwd=repo)
    init_embedded(repo, "leak")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "chore: track beads", cwd=repo, check=False)
    assert git("ls-files", "--", ".beads/config.yaml", cwd=repo).stdout.strip()
    return repo


def _remote(tmp_path, name) -> str:
    path = tmp_path / name
    path.mkdir()
    return f"file://{path}"


def _head(repo) -> str:
    return git("rev-parse", "HEAD", cwd=repo).stdout.strip()


def _assert_clean(repo, head):
    assert not _SYNC.search((repo / ".beads/config.yaml").read_text())
    assert _head(repo) == head
    assert git("status", "--porcelain", "--", ".beads/config.yaml", cwd=repo).stdout.strip() == ""
    assert "sync.remote" not in git("log", "--format=%s", cwd=repo).stdout


def test_bd_itself_still_leaks_so_the_guard_is_load_bearing(tmp_path):
    repo = _hive(tmp_path)
    head = _head(repo)
    bd("dolt", "remote", "add", "origin", _remote(tmp_path, "raw"), cwd=repo, capture=True)
    # If a bd release stops doing this the guard becomes a no-op and this test says so.
    assert _head(repo) != head or _SYNC.search((repo / ".beads/config.yaml").read_text())


def test_add_remote_leaves_no_trace(tmp_path):
    repo = _hive(tmp_path)
    head = _head(repo)
    url = _remote(tmp_path, "internal")
    res = engine.get_engine().add_remote(repo, "internal", url)
    assert res.returncode == 0, res.stderr
    assert engine.get_engine().get_remote(repo, "internal").url == url  # registration is real
    _assert_clean(repo, head)


def test_replace_remote_cutover_and_restore_leave_no_trace(tmp_path):
    repo = _hive(tmp_path)
    eng = engine.get_engine()
    first, second = _remote(tmp_path, "one"), _remote(tmp_path, "two")
    assert eng.add_remote(repo, "origin", first).returncode == 0
    head = _head(repo)
    snapshot = eng.read_registry(repo).snapshot

    assert eng.replace_remote(repo, "origin", second).returncode == 0
    assert eng.get_remote(repo, "origin").url == second
    _assert_clean(repo, head)

    restored = eng.restore_registry(repo, snapshot)
    assert restored.ok, restored.error
    assert eng.get_remote(repo, "origin").url == first
    _assert_clean(repo, head)

    third = _remote(tmp_path, "three")
    out = eng.cutover_remote(repo, "origin", third, retain_as="old")
    assert out.ok, out.error
    assert eng.get_remote(repo, "origin").url == third
    assert eng.get_remote(repo, "old").url == first
    _assert_clean(repo, head)


def test_preexisting_sync_remote_hint_is_preserved(tmp_path):
    repo = _hive(tmp_path)
    cfg = repo / ".beads/config.yaml"
    cfg.write_text(cfg.read_text() + 'sync.remote: "git+ssh://git@example.test/acme/widget.git"\n')
    git("commit", "-qam", "chore: git hint", cwd=repo)
    head, before = _head(repo), cfg.read_text()
    assert engine.get_engine().add_remote(repo, "internal", _remote(tmp_path, "x")).returncode == 0
    assert _head(repo) == head
    assert cfg.read_text() == before


def test_untracked_config_is_left_alone(tmp_path):
    repo = _hive(tmp_path)
    git("rm", "-q", "--cached", ".beads/config.yaml", cwd=repo)
    git("commit", "-qm", "chore: untrack", cwd=repo)
    head = _head(repo)
    assert engine.get_engine().add_remote(repo, "internal", _remote(tmp_path, "y")).returncode == 0
    assert _head(repo) == head


def test_scrub_refuses_to_drop_a_foreign_commit(tmp_path):
    repo = _hive(tmp_path)
    snap = config_leak_guard.snapshot(repo)
    (repo / "other.txt").write_text("x\n")
    git("add", ".", cwd=repo)
    git("commit", "-qm", "feat: unrelated", cwd=repo)
    head = _head(repo)
    config_leak_guard.scrub(snap)
    assert _head(repo) == head


def test_leak_finding_detects_planted_sync_remote(tmp_path):
    repo = _hive(tmp_path)
    entry = {"provider": "github", "org": "acme", "repo": "code", "prefix": "leak"}
    assert config_leak_guard.leak_finding(entry, repo) is None
    cfg = repo / ".beads/config.yaml"
    secret = "s3://internal-bucket.corp.example/beads?endpoint=https://s3.corp.example"
    cfg.write_text(cfg.read_text() + f'sync.remote: "{secret}"\n')
    git("commit", "-qam", "oops", cwd=repo)
    finding = config_leak_guard.leak_finding(entry, repo)
    assert finding and "sync.remote" in finding
    assert dolt_remote.redact_url(secret) in finding
    # A hive with a Git-hosted remote and no descriptor is not flagged.
    cfg.write_text(cfg.read_text().replace(secret, "git+ssh://git@example.test/a/b.git"))
    git("commit", "-qam", "git hint", cwd=repo)
    assert config_leak_guard.leak_finding(entry, repo) is None
    # A descriptor-declared (partitioned-style) hive is flagged even for a Git URL.
    entry["dolt_remote"] = {"name": "internal", "url": secret}
    assert config_leak_guard.leak_finding(entry, repo)


def test_every_bd_remote_add_goes_through_the_guard():
    """Source audit: the only ``dolt remote add`` bd invocations are in ``BdEngine._register``
    (plus HQ's first push, wrapped in the guard)."""
    src = Path(engine.__file__).read_text()
    calls = [m.start() for m in re.finditer(r'\["dolt", "remote", "add"', src)]
    assert calls, "expected registration argv in engine.py"
    for pos in calls:
        line = src[:pos].count("\n")
        snippet = "\n".join(src.splitlines()[line - 1 : line + 2])
        assert "_register(" in snippet or "args = " in snippet, snippet
    hq = (Path(engine.__file__).parent / "hq.py").read_text()
    assert "config_leak_guard.guarded" in hq


def test_hive_ready_requires_no_tracked_sync_remote(tmp_path):
    from beadhive import hive_ready

    repo = _hive(tmp_path)
    entry = {"provider": "github", "org": "acme", "repo": "code", "prefix": "leak"}
    assert hive_ready._tracked_sync_remote_check(entry, repo).state == "na"
    cfg = repo / ".beads/config.yaml"
    cfg.write_text(cfg.read_text() + 'sync.remote: "s3://private-bucket/beads"\n')
    git("commit", "-qam", "oops", cwd=repo)
    check = hive_ready._tracked_sync_remote_check(entry, repo)
    assert (check.required, check.state) == (True, "missing")
