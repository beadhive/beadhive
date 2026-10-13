"""The clean-checkout key runner and the shared checkout its keys run in (bh-qhrl6).

Real git and the real validation records for the shared-checkout run (the verdict a key records
must be exactly the one a sequential run records); the broker is the package's in-memory fake
and every setup command goes through a fake :data:`SetupRunner`, so nothing here runs ``uv``.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from beadhive import host, validation_admission, validation_ledger, worktree, worktree_verify
from beadhive.validation_broker_engine import ENGINE_ENV
from beadhive.validation_key_runner import CheckoutKeyRunner, key_environ
from beadhive.validation_side_by_side import GroupKey
from beadhive_validation_broker import Capacity, Grant, InMemoryBroker, Reservation
from beadhive_validation_broker.contract import ManualClock

GENEROUS = 10.0
_CLEAN_ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


@pytest.fixture(autouse=True)
def _host():
    host.mint_if_needed()


def _git(*args, cwd) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=_CLEAN_ENV
    ).stdout.strip()


@pytest.fixture
def hive(tmp_path, monkeypatch):
    repo = tmp_path / "ws" / "github" / "myorg" / "myrepo"
    repo.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "t@example.com", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    _git("config", "commit.gpgsign", "false", cwd=repo)
    (repo / "f.txt").write_text("hi")
    _git("add", "f.txt", cwd=repo)
    _git("commit", "-qm", "init", cwd=repo)
    monkeypatch.setenv("GIT_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("BH_WORKTREES", str(tmp_path / "wts"))
    monkeypatch.delenv(ENGINE_ENV, raising=False)
    entry = {"provider": "github", "org": "myorg", "repo": "myrepo", "prefix": "mr"}
    return SimpleNamespace(
        repo=repo,
        entry=entry,
        cfg={"managed_repos": [entry]},
        sha=_git("rev-parse", "HEAD", cwd=repo),
    )


def _grant(cpus: int = 3, root: str | None = None) -> Grant:
    return Grant("lease-1", "gate/a", Capacity(cpus, 1 << 30, {"dolt-servers": 2}), root=root)


# ---- clean_checkout(shared=...) -------------------------------------------------------------


def test_a_shared_checkout_key_records_exactly_the_verdict_a_sequential_run_does(
    hive, tmp_path, monkeypatch
) -> None:
    shared_dir = tmp_path / "shared"
    _git("worktree", "add", "--detach", str(shared_dir), "main", cwd=hive.repo)
    out = tmp_path / "out.txt"
    cmd = 'sh -c \'pwd > "$OUT"; echo "$BH_GRANT_CPUS" >> "$OUT"; test ! -e flagged.marker\''
    env = {**key_environ(_grant(cpus=3)), "OUT": str(out)}
    cfg = {**hive.cfg, "worktrees": {"init": [{"run": "touch flagged.marker", "verify": True}]}}
    monkeypatch.setattr(
        validation_admission,
        "host_slot",
        lambda *_a, **_k: pytest.fail("a shared-checkout key is admitted by its grant"),
    )

    rc = worktree.clean_checkout(
        hive.entry,
        "main",
        cmd,
        cfg=cfg,
        reuse=True,
        bead="mr-1",
        phase="merge",
        shared=worktree_verify.SharedCheckout(shared_dir, env),
    )

    assert rc == 0
    ran_in, cpus = out.read_text().split()
    assert Path(ran_in).resolve() == shared_dir.resolve()  # the shared checkout, no verify dir
    assert cpus == "3"  # the grant environment reached the key
    assert shared_dir.is_dir()  # its owner, not the key, removes it
    assert not (shared_dir / "flagged.marker").exists()  # init rules ran once, in setup
    runs = [
        json.loads(p.read_text())
        for p in (hive.repo / ".git/bh/validation/runs").glob("*/manifest.json")
    ]
    assert len(runs) == 1
    run = runs[0]
    assert (run["bead"], run["phase"], run["branch"], run["verdict"]) == (
        "mr-1",
        "merge",
        "main",
        "green",
    )
    assert run["command_hash"] == validation_ledger.cmd_hash(cmd)  # the grant is never hashed
    assert validation_ledger.green_verdict(hive.entry, "main", cmd, cfg=cfg) is not None
    parent = worktree.wt_dir(hive.entry, "x").parent
    assert not list(parent.glob(f"{worktree.VERIFY_LEAF_PREFIX}*"))


def _spawned_env(hive, monkeypatch, cfg) -> dict:
    seen = []

    class _Done:
        returncode = 0

    def fake_run(cmd, **kw):
        seen.append((list(cmd), kw))
        return _Done()

    monkeypatch.setattr(worktree, "run", fake_run)
    assert worktree.clean_checkout(hive.entry, "main", "just check-all-native", cfg=cfg) == 0
    (validation,) = [kw for cmd, kw in seen if cmd[:1] != ["git"]]
    return validation["env"]


def test_engine_local_in_config_reaches_the_validation_command(hive, monkeypatch) -> None:
    cfg = {**hive.cfg, "work": {"validation_broker": {"engine": "local"}}}
    assert _spawned_env(hive, monkeypatch, cfg)[ENGINE_ENV] == "local"


@pytest.mark.parametrize(
    "broker", [None, {"engine": "slots"}, {"engine": "bogus"}], ids=["unset", "slots", "invalid"]
)
def test_engine_slots_leaves_the_validation_environment_unchanged(
    hive, monkeypatch, broker
) -> None:
    cfg = {**hive.cfg, **({"work": {"validation_broker": broker}} if broker else {})}
    assert ENGINE_ENV not in _spawned_env(hive, monkeypatch, cfg)


# ---- CheckoutKeyRunner: the drop-in runner ---------------------------------------------------


class _RecordingWorktree:
    def __init__(self, rc=0, script=None):
        self.calls: list[tuple] = []
        self.rc = rc
        self.script = script
        self.lock = threading.Lock()

    def clean_checkout(self, entry, ref, cmd, **kwargs):
        with self.lock:
            self.calls.append((entry, ref, cmd, kwargs))
        if self.script is not None:
            return self.script(cmd, kwargs)
        return self.rc


def test_calling_the_runner_is_the_lambda_it_replaces() -> None:
    fake = _RecordingWorktree(rc=3)
    runner = CheckoutKeyRunner(fake, {"repo": "r"}, "main", cfg={"c": 1}, reuse=True, phase="merge")
    assert runner("just attest-unit") == 3
    assert fake.calls == [
        (
            {"repo": "r"},
            "main",
            "just attest-unit",
            {"cfg": {"c": 1}, "reuse": True, "phase": "merge"},
        )
    ]


def test_without_caller_kwargs_none_are_invented() -> None:
    fake = _RecordingWorktree()
    CheckoutKeyRunner(fake, {}, "sha", reuse=True)("just x")
    assert fake.calls[0][3] == {"reuse": True}


def test_a_refused_command_answers_one_without_running() -> None:
    fake = _RecordingWorktree()
    runner = CheckoutKeyRunner(fake, {}, "rev", refuse="just check-all-native", reuse=True)
    assert runner(" just check-all-native ") == 1
    assert runner("just attest-unit") == 0
    assert [call[2] for call in fake.calls] == ["just attest-unit"]


# ---- CheckoutKeyRunner.run_group: one shared checkout, one setup job -------------------------


def _keys(*names, cpu=2):
    return [
        GroupKey(name, f"just attest-{name}", Reservation(cpu, cpu, 1 << 20), 1.0) for name in names
    ]


def _run_group(runner, keys, broker=None):
    reported = []
    warnings: list[str] = []
    result = runner.run_group(
        broker or InMemoryBroker(Capacity(64, 1 << 40, {"dolt-servers": 12}), ManualClock()),
        keys,
        report=reported.append,
        warn=warnings.append,
    )
    return result, reported, warnings


class _RootedBroker(InMemoryBroker):
    """The in-memory fake, placing every grant on one execution root (as the local engine does)."""

    def __init__(self, root: str):
        super().__init__(Capacity(64, 1 << 40), ManualClock())
        self.root = root

    def wait_grant(self, handle, timeout=None):
        grant = super().wait_grant(handle, timeout)
        return Grant(grant.lease_id, grant.job_id, grant.amounts, root=self.root)


def test_keys_share_one_checkout_in_the_granted_root_after_one_setup_job(
    hive, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "leaked-venv"))
    root = tmp_path / "root"
    root.mkdir()
    events: list[str] = []
    rendezvous = threading.Barrier(3, timeout=GENEROUS)

    def script(cmd, kwargs):
        events.append(f"key {cmd}")
        rendezvous.wait()  # all three keys run at once
        return 1 if cmd == "just attest-b" else 0

    def setup(command, cwd, env):
        events.append(f"setup {command}")
        assert (Path(cwd) / "f.txt").read_text() == "hi"  # the exact tree is already there
        return 0

    fake = _RecordingWorktree(script=script)
    runner = CheckoutKeyRunner(
        fake, hive.entry, "main", setup_runner=setup, cfg=hive.cfg, reuse=True, phase="merge"
    )
    result, reported, warnings = _run_group(runner, _keys("a", "b", "c"), _RootedBroker(str(root)))

    assert {o.name: o.rc for o in result.outcomes.values()} == {"a": 0, "b": 1, "c": 0}
    assert events[0] == "setup uv sync --all-packages --inexact"  # the default setup, once
    assert sorted(events[1:]) == ["key just attest-a", "key just attest-b", "key just attest-c"]
    checkouts = {call[3]["shared"].path for call in fake.calls}
    assert len(checkouts) == 1  # ONE shared checkout
    (checkout,) = checkouts
    assert root.resolve() in checkout.parents  # inside the granted root
    for _entry, ref, cmd, kwargs in fake.calls:
        assert ref == "main" and cmd.startswith("just attest-")  # the key's own command
        assert {k: kwargs[k] for k in ("cfg", "reuse", "phase")} == {
            "cfg": hive.cfg,
            "reuse": True,
            "phase": "merge",
        }
        env = kwargs["shared"].env
        assert env["BH_GRANT_CPUS"] == "2" and env["BH_GRANT_ROOT"] == str(root)
        assert Path(env["TMPDIR"]).resolve().is_relative_to(root.resolve())
        assert env["NO_COLOR"] == "1" and env["OTEL_SDK_DISABLED"] == "true"
        assert "VIRTUAL_ENV" not in env
    assert not checkout.exists()  # removed when the group finished
    assert warnings == []


def test_without_roots_the_keys_share_one_checkout_in_todays_location(
    hive, tmp_path, monkeypatch
) -> None:
    made: list[tuple] = []

    @contextmanager
    def shared_verify(entry, branch, label):
        path = tmp_path / "verify-main-abc123"
        path.mkdir()
        made.append((branch, label))
        yield path

    monkeypatch.setattr(worktree_verify, "impl_shared_verify_checkout", shared_verify)
    fake = _RecordingWorktree()
    runner = CheckoutKeyRunner(fake, hive.entry, "main", setup_runner=lambda *_a: 0, cfg=hive.cfg)
    result, _, _ = _run_group(runner, _keys("a", "b"))

    assert {o.rc for o in result.outcomes.values()} == {0}
    assert len(made) == 1 and made[0][0] == "main"
    assert {call[3]["shared"].path for call in fake.calls} == {tmp_path / "verify-main-abc123"}


def test_a_failing_setup_command_sends_every_key_to_the_slots_path(
    hive, tmp_path, monkeypatch
) -> None:
    @contextmanager
    def shared_verify(entry, branch, label):
        yield tmp_path

    monkeypatch.setattr(worktree_verify, "impl_shared_verify_checkout", shared_verify)
    fake = _RecordingWorktree()
    runner = CheckoutKeyRunner(fake, hive.entry, "main", setup_runner=lambda *_a: 2, cfg=hive.cfg)
    result, reported, warnings = _run_group(runner, _keys("a", "b"))

    assert list(result.fallback) == ["a", "b"]
    assert "setup `uv sync --all-packages --inexact` exited 2" in result.fallback["a"]
    assert fake.calls == [] and reported == []
    assert len(warnings) == 1


def test_configured_setup_commands_replace_the_default(hive, tmp_path, monkeypatch) -> None:
    @contextmanager
    def shared_verify(entry, branch, label):
        yield tmp_path

    monkeypatch.setattr(worktree_verify, "impl_shared_verify_checkout", shared_verify)
    ran: list[str] = []
    cfg = {**hive.cfg, "work": {"validation_broker": {"setup": ["mise trust", "uv sync"]}}}
    runner = CheckoutKeyRunner(
        _RecordingWorktree(),
        hive.entry,
        "main",
        setup_runner=lambda c, *_a: ran.append(c) or 0,
        cfg=cfg,
    )
    _run_group(runner, _keys("a"))
    assert ran == ["mise trust", "uv sync"]


def test_a_key_sees_the_parents_bh_git_and_ssh_settings_but_only_its_own_grant(
    monkeypatch,
) -> None:
    monkeypatch.setenv("BH_DOLT_SLOTS", "12")
    monkeypatch.setenv("BH_TEST_TIMEOUT_SECONDS", "2400")
    monkeypatch.setenv("GIT_WORKSPACE", "/srv/ws")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/run/agent.sock")
    monkeypatch.setenv("BH_GRANT_CPUS", "99")  # stale, from an enclosing grant
    monkeypatch.setenv("BH_GRANT_DOLT_SERVERS", "99")
    monkeypatch.setenv("BH_VALIDATION_LEASE", "stale-lease")
    monkeypatch.setenv("VIRTUAL_ENV", "/leaked/venv")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "nope")

    env = key_environ(_grant(cpus=3))

    assert env["BH_DOLT_SLOTS"] == "12" and env["BH_TEST_TIMEOUT_SECONDS"] == "2400"
    assert env["GIT_WORKSPACE"] == "/srv/ws" and env["SSH_AUTH_SOCK"] == "/run/agent.sock"
    assert env["BH_GRANT_CPUS"] == "3" and env["BH_GRANT_DOLT_SERVERS"] == "2"
    assert env["BH_VALIDATION_LEASE"] == "lease-1"
    assert "VIRTUAL_ENV" not in env and "AWS_SECRET_ACCESS_KEY" not in env
