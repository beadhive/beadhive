"""The pre-push fence hook + its CLI decision (bh-ytbb.12).

Two things under test:

  * `prepush.install_for_hive` — the hook SCRIPT lands in both possible locations
    (`.git/hooks/` and any existing bd-embedded transport bare repo's `hooks/`),
    non-destructively (a foreign hook is never clobbered), idempotently, and — the
    spec-review point this bead turns on — REGARDLESS of the hive's declared furnish
    footprint (independent of the furnish axis, not gated behind it).
  * `prepush.check_fence` — the decision the hook's `bh hive check-push-fence` shells out to:
    reuses `guard.primary_state`'s cached-lease read (bh-ytbb.9), so it agrees with
    `guard_primary` about who is primary, entirely from local state (no HQ round trip).

The HQ/lease fixtures mirror `test_guard_primary.py`'s: a scratch HQ clone under `tmp_path`
with `BH_HQ` pointed at it — the operator's real HQ is never read or written.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from beadhive import gitref, host, host_fence, host_lease, prepush, registry
from beadhive.cli import app

PREFIX = "tt"
THIS_HOST = "11111111-1111-4111-8111-111111111111"
OTHER_HOST = "22222222-2222-4222-8222-222222222222"
T0 = 1_800_000_000.0

runner = CliRunner()


def _git(args, cwd):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False
    )


def _init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q"], path)
    _git(["config", "user.email", "t@example.invalid"], path)
    _git(["config", "user.name", "t"], path)
    return path


# ---- install_for_hive: hook-script placement ---------------------------------------------


@pytest.fixture
def hive_dir(tmp_path):
    return _init_repo(tmp_path / "hive")


def test_installs_into_main_checkout_git_hooks(hive_dir):
    statuses = prepush.install_for_hive(hive_dir)
    hook = hive_dir / ".git" / "hooks" / "pre-push"
    assert hook.exists()
    assert any("installed" in s for s in statuses)
    assert str(hive_dir) in hook.read_text()  # the hive dir is baked in
    assert host_fence.DATA_REF in hook.read_text()


def test_installed_hook_is_executable(hive_dir):
    prepush.install_for_hive(hive_dir)
    hook = hive_dir / ".git" / "hooks" / "pre-push"
    assert hook.stat().st_mode & 0o111


def test_a_relative_hive_dir_is_baked_in_as_absolute(hive_dir, monkeypatch):
    """Regression: `onboard.Ctx.base` is `Path(".")` whenever `cwd` was never threaded
    explicitly (the real `bh hive init` CLI path, confirmed against a live scratch fixture) —
    a relative path baked in verbatim would resolve against the WRONG directory once the hook
    runs from a completely different cwd (the bd-embedded transport bare repo)."""
    monkeypatch.chdir(hive_dir)
    statuses = prepush.install_for_hive(Path("."))
    hook = hive_dir / ".git" / "hooks" / "pre-push"
    content = hook.read_text()
    assert str(hive_dir) in content
    assert "hive_dir='.'" not in content
    assert any(str(hive_dir) in s for s in statuses)


def test_no_transport_repo_yet_only_installs_the_one_location(hive_dir):
    """A freshly-inited hive has no bd-embedded transport repo yet (bd creates it lazily on
    the first `bd dolt push`) — see prepush.py's module docstring for why that gap is
    harmless. Only the main-checkout hook is installed."""
    statuses = prepush.install_for_hive(hive_dir)
    assert len(statuses) == 1


def test_also_installs_into_an_existing_transport_bare_repo(hive_dir):
    """Mirrors the second-host bootstrap case: the transport repo already exists (a real
    `bd bootstrap`/`bd dolt push` would have created it) by the time `bh hive init` runs."""
    transport = (
        hive_dir / ".beads" / "embeddeddolt" / "zz" / ".dolt"
        / "git-remote-cache" / "deadbeef" / "repo.git"
    )
    _git(["init", "-q", "--bare", str(transport)], hive_dir)

    statuses = prepush.install_for_hive(hive_dir)

    assert len(statuses) == 2
    hook = transport / "hooks" / "pre-push"
    assert hook.exists()
    assert str(hive_dir) in hook.read_text()


def test_reinstall_is_idempotent(hive_dir):
    first = prepush.install_for_hive(hive_dir)
    second = prepush.install_for_hive(hive_dir)
    assert any("installed" in s for s in first)
    assert any("unchanged" in s for s in second)


def test_reinstall_refreshes_stale_marked_content(hive_dir):
    hook = hive_dir / ".git" / "hooks" / "pre-push"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(f"#!/bin/sh\n{prepush._MARKER}\necho OUTDATED_CONTENT_MARKER\n")
    hook.chmod(0o755)

    statuses = prepush.install_for_hive(hive_dir)

    assert any("refreshed" in s for s in statuses)
    assert "OUTDATED_CONTENT_MARKER" not in hook.read_text()


def test_a_foreign_hook_is_never_clobbered(hive_dir):
    """A safety mechanism must not destroy a repo's own pre-existing tooling."""
    hook = hive_dir / ".git" / "hooks" / "pre-push"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\necho my own custom hook\n")
    hook.chmod(0o755)

    statuses = prepush.install_for_hive(hive_dir)

    assert any("skipped" in s for s in statuses)
    assert hook.read_text() == "#!/bin/sh\necho my own custom hook\n"


def test_onboard_prepush_hook_step_is_enabled_unconditionally():
    """The AC's spec-review point: `furnish: none` hives must still get the hook, so the
    onboard step's `enabled` gate must be the Step default (always-true) — never conditioned
    on `ctx.furnish`, unlike the `claude`/`agents`/`skills`/... installer steps beside it. The
    fuller end-to-end version — driving the real DAG with furnish declared none — lives in
    test_onboard_dag.py."""
    from beadhive import onboard

    ctx = onboard.Ctx(hive="github/o/r", target="/x", furnish=False)
    step = next(s for s in onboard.build_steps(ctx) if s.id == "prepush-hook")
    assert step.enabled(ctx) is True


# ---- check_fence: the decision --------------------------------------------------------


@pytest.fixture
def hq(tmp_path, monkeypatch):
    path = tmp_path / "hq"
    path.mkdir()
    _git(["init", "-q"], path)
    _git(["config", "user.email", "t@example.invalid"], path)
    _git(["config", "user.name", "t"], path)
    monkeypatch.setenv("BH_HQ", str(path))
    return path


@pytest.fixture
def this_host(monkeypatch):
    monkeypatch.setattr(host, "host_id", lambda: THIS_HOST)
    return THIS_HOST


@pytest.fixture
def registered(monkeypatch):
    """`registry.entry_for_dir` resolves ANY directory to one fixed hive entry — mirrors
    test_guard_primary.py's `hive` fixture; `check_fence` always passes an explicit
    `hive_dir=`, so `hive_dir_for` (the cwd-based resolver) is never consulted."""
    entry = {"provider": "github", "org": "o", "repo": "r", "prefix": PREFIX}
    monkeypatch.setattr(registry, "entry_for_dir", lambda _cfg, _dir: entry)
    return entry


def _record_lease(hq_dir, lease):
    sha = gitref.write_object(lease.to_record(), cwd=hq_dir)
    gitref.set_local(host_lease.lease_ref(PREFIX), sha, cwd=hq_dir)


def _lease(host_id, *, epoch=1, ttl=600.0, label="deskmac", adopted_at=None):
    return host_lease.HostLease(
        host_id=host_id,
        label=label,
        epoch=epoch,
        adopted_at=adopted_at if adopted_at is not None else host_lease.now_stamp(T0),
        expires_at=host_lease.now_stamp(T0 + ttl),
    )


def test_allows_when_multi_host_never_adopted(tmp_path, this_host, registered):
    ok, detail = prepush.check_fence(tmp_path / "hive", cfg={})
    assert ok is True
    assert detail == ""


def test_allows_when_no_hq_clone_on_this_host(tmp_path, this_host, registered, monkeypatch):
    monkeypatch.setenv("BH_HQ", str(tmp_path / "absent-hq"))
    ok, _detail = prepush.check_fence(tmp_path / "hive", cfg={})
    assert ok is True


def test_allows_when_this_host_holds_the_lease(hq, this_host, registered, monkeypatch, tmp_path):
    monkeypatch.setattr(host_lease.time, "time", lambda: T0 + 1)
    _record_lease(hq, _lease(THIS_HOST))
    ok, detail = prepush.check_fence(tmp_path / "hive", cfg={})
    assert ok is True
    assert detail == ""


def test_refuses_when_a_foreign_host_holds_the_lease(
    hq, this_host, registered, monkeypatch, tmp_path
):
    monkeypatch.setattr(host_lease.time, "time", lambda: T0 + 1)
    _record_lease(hq, _lease(OTHER_HOST))
    ok, detail = prepush.check_fence(tmp_path / "hive", cfg={})
    assert ok is False
    assert prepush.PREPUSH_FENCE_REFUSAL_MARKER in detail
    assert PREFIX in detail
    assert OTHER_HOST in detail


def test_refuses_when_this_hosts_lease_has_lapsed(hq, this_host, registered, monkeypatch, tmp_path):
    """Fail-closed, mirroring guard_primary's own lapsed-lease behavior."""
    monkeypatch.setattr(host_lease.time, "time", lambda: T0 + 9999)
    _record_lease(hq, _lease(THIS_HOST, ttl=600.0))
    ok, _detail = prepush.check_fence(tmp_path / "hive", cfg={})
    assert ok is False


def test_refusal_names_no_verify_and_the_real_backstop(
    hq, this_host, registered, monkeypatch, tmp_path
):
    """AC: documented as bypassable (--no-verify), with the push fence named as the real
    backstop — the refusal text is the one place an operator actually reads this."""
    monkeypatch.setattr(host_lease.time, "time", lambda: T0 + 1)
    _record_lease(hq, _lease(OTHER_HOST))
    _ok, detail = prepush.check_fence(tmp_path / "hive", cfg={})
    assert "--no-verify" in detail
    assert host_fence.EPOCH_REF in detail
    assert "--force-with-lease" in detail


def test_refuses_on_a_released_tombstone(hq, this_host, registered, monkeypatch, tmp_path):
    monkeypatch.setattr(host_lease.time, "time", lambda: T0 + 1)
    _record_lease(hq, host_lease.HostLease("", "", 3, "t", host_lease.now_stamp(T0)))
    ok, detail = prepush.check_fence(tmp_path / "hive", cfg={})
    assert ok is False
    assert "nobody currently holds it" in detail


def test_check_fence_never_calls_hive_dir_for_cwd_resolution(tmp_path, this_host, monkeypatch):
    """`check_fence` always passes `hive_dir=` explicitly (the hook bakes it in at install
    time) — the cwd-based `hive_dir_for` resolver must never be consulted."""
    entry = {"provider": "github", "org": "o", "repo": "r", "prefix": PREFIX}

    def _boom(*_a, **_k):
        raise AssertionError("hive_dir_for must not be called when hive_dir= is passed")

    monkeypatch.setattr(registry, "hive_dir_for", _boom)
    monkeypatch.setattr(registry, "entry_for_dir", lambda _cfg, _dir: entry)
    ok, _detail = prepush.check_fence(tmp_path / "hive", cfg={})
    assert ok is True  # never adopted -> allow; the point is _boom never fired


# ---- the CLI wrapper -------------------------------------------------------------------


def test_cli_exits_zero_on_allow(monkeypatch, tmp_path):
    monkeypatch.setattr(prepush, "check_fence", lambda _hive_dir, **_k: (True, ""))
    result = runner.invoke(app, ["hive", "check-push-fence", "--hive-dir", str(tmp_path)])
    assert result.exit_code == 0


def test_cli_exits_nonzero_and_prints_detail_on_refuse(monkeypatch, tmp_path):
    monkeypatch.setattr(
        prepush, "check_fence", lambda _hive_dir, **_k: (False, "computed refusal detail")
    )
    result = runner.invoke(app, ["hive", "check-push-fence", "--hive-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "computed refusal detail" in result.output


def test_cli_command_is_hidden():
    """A hook-invoked internal verb, not an operator-facing one — mirrors `hive context`."""
    from beadhive.cli import hive_app

    cmd = next(c for c in hive_app.registered_commands if c.name == "check-push-fence")
    assert cmd.hidden is True
