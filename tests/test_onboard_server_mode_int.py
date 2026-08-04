"""Integration: server mode on first install (bh-areg.7), against a REAL bd binary — not a
mock of `hive.run`. Proves the three things a hermetic test can't:

  1. `_act_bd_init`'s three paths really land a fresh hive on bd's shared server with
     `dolt_mode` durably persisted in `.beads/metadata.json` (constraint 1), with NO
     pre-existing server required to start it — bd's own `--shared-server` flag /
     `BEADS_DOLT_SHARED_SERVER` env var auto-starts one transparently. This is the empirical
     basis for shipping this as the DEFAULT rather than opt-in (see the bead's own report):
     a brand-new user never has to know a server exists, let alone start one.
  2. `backup.enabled` really lands True when a git remote is present (constraint 4), and is
     left at bd's own default otherwise — never manufacturing a durability difference that
     was never real.
  3. An EXISTING embedded hive is genuinely untouched by re-running onboard (constraint 2):
     install, "upgrade" (re-run onboard), verify `dolt_mode` is unchanged on disk and every
     verb still works.
  4. A BUSY dolt-server port (something else already bound to it before onboarding starts —
     the review's own reproduction) exits with a legible top-level error, never a raw
     `subprocess.CalledProcessError` plus bh's generic-handler structlog blob, and leaves
     NOTHING behind: no `.beads/`, no stray root `.gitignore`, a clean `git status`. The
     immediate retry (port now free) needs no `--skip-check` and produces a REAL working
     store — never a hive reported ready that has no store (the review's worse finding).
  5. An init KILLED mid-flight (bh-areg.7's review, round 4 — SIGKILL against a real
     `bd init --shared-server`, the instant `.beads/metadata.json` first carries a persisted
     `dolt_mode`) leaves a `.beads/` that looks real by every static signal rounds 1-3 ever
     checked, yet does not open. `hive.store_opens()` must call that correctly, and
     re-running onboard against it must never report the hive ready — either it refuses
     legibly or it reclaims and re-mints for real, but it never skips as "already
     initialized" and never lets an unhandled exception through.

Runs against an ISOLATED shared-server instance (its own `BEADS_SHARED_SERVER_DIR` + a free
TCP port), never the operator's real `~/.beads/shared-server/` or any registered hive — the
same scratch discipline `test_storage_migrate_int.py` follows.

Marked `integration` (slower — spins up a real Dolt sql-server) + self-skips without a `bd`
binary on PATH.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import time

import pytest
import typer

from beadhive import hive, hub, onboard, store_locator
from harness.beads import bd_json, create, skip_if_no_bd
from harness.world import git

pytestmark = [pytest.mark.integration, skip_if_no_bd]

_TIMEOUT = 60


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def _occupy_port(port: int):
    """Bind and hold *port* on 127.0.0.1 for the duration of the block, so bd's own attempt to
    start a shared server there hits a REAL busy-port failure — the review's own reproduction,
    not a simulated one. A bare `listen()` with nothing ever calling `accept()` reproduces both
    of bd's observed failure shapes (an immediate "in use by a non-dolt process" refusal, or a
    slower "started but not accepting connections" timeout), depending on bd's own probe
    strategy — this test doesn't depend on which one fires, only on the outcome."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    sock.listen(1)
    try:
        yield
    finally:
        sock.close()


@pytest.fixture
def isolated_shared_server(tmp_path, monkeypatch):
    """This test's OWN shared-server instance, at its own data dir and a free port — never the
    operator's real `~/.beads/shared-server/`."""
    monkeypatch.setenv("BEADS_SHARED_SERVER_DIR", str(tmp_path / "shared-server"))
    monkeypatch.setenv("BEADS_DOLT_SERVER_PORT", str(_free_port()))


def _stop_shared_server_best_effort(path):
    """The shared server DETACHES from the spawning CLI process — stop it explicitly so the
    suite doesn't accumulate orphaned `dolt sql-server` processes across runs."""
    from beadhive.run import run

    run(["bd", "-C", str(path), "dolt", "stop"], check=False, capture=True, timeout=30)


def _repo(path, *, remote=None):
    path.mkdir(parents=True, exist_ok=True)
    git("init", "-q", "-b", "main", cwd=path)
    git("config", "user.email", "t@t.dev", cwd=path)
    git("config", "user.name", "T", cwd=path)
    if remote is not None:
        git("remote", "add", "origin", str(remote), cwd=path)
    (path / "README.md").write_text("hi\n")
    git("add", ".", cwd=path)
    git("commit", "-q", "-m", "init", cwd=path)
    if remote is not None:
        git("push", "-q", "-u", "origin", "main", cwd=path)
    return path


def _ctx(target, *, furnish: bool) -> onboard.Ctx:
    ctx = onboard.Ctx(
        hive="github/acme/widget",
        target=str(target),
        provider="github",
        org="acme",
        repo="widget",
        cwd=str(target),
        prefix="widget",
        furnish=furnish,
    )
    ctx._derived = True  # skip real registry/classify lookups — irrelevant to this bead
    return ctx


# ---------------------------------------------------------------------------
# Furnished path: real round trip
# ---------------------------------------------------------------------------


def test_furnished_path_lands_on_server_mode_with_backup_on(tmp_path, isolated_shared_server):
    remote = tmp_path / "origin.git"
    remote.mkdir()
    git("init", "-q", "--bare", "-b", "main", cwd=remote)
    target = _repo(tmp_path / "hive1", remote=remote)
    try:
        onboard._act_bd_init(_ctx(target, furnish=True))

        assert store_locator.dolt_mode(target) == "server"
        metadata = json.loads((target / ".beads" / "metadata.json").read_text())
        assert metadata["dolt_mode"] == "server"
        assert bd_json("config", "get", "dolt.shared-server", cwd=target).get("value") == "true"
        # constraint 4: a git remote is present, so backup.enabled must land True — a hive
        # minted straight onto server mode must not be born less durable than embedded.
        assert bd_json("config", "get", "backup.enabled", cwd=target).get("value") is True
        # bd verbs work against the freshly-minted store (not just "the command exited 0").
        assert create(target, "smoke test issue")
    finally:
        _stop_shared_server_best_effort(target)


# ---------------------------------------------------------------------------
# Zero-footprint path: real round trip, stays zero-footprint
# ---------------------------------------------------------------------------


def test_zero_footprint_path_lands_on_server_mode_and_stays_zero_footprint(
    tmp_path, isolated_shared_server
):
    target = _repo(tmp_path / "hive2")  # no git remote — no backup.enabled expectation
    try:
        onboard._act_bd_init(_ctx(target, furnish=False))

        assert store_locator.dolt_mode(target) == "server"
        # Zero commits, zero tracked changes — the discipline this path exists to protect.
        assert git("status", "--porcelain", cwd=target).stdout.strip() == ""
        assert git("rev-list", "--count", "HEAD", cwd=target).stdout.strip() == "1"  # only "init"
        exclude = (target / ".git" / "info" / "exclude").read_text()
        assert ".beads/" in exclude
        # No git remote — backup.enabled is left at bd's own default (unset), matching what
        # embedded mode would ALSO have defaulted to without a remote (never true here).
        assert bd_json("config", "get", "backup.enabled", cwd=target).get("value") is not True
        assert create(target, "smoke test issue")
    finally:
        _stop_shared_server_best_effort(target)


# ---------------------------------------------------------------------------
# Bootstrap (second-host) path: real round trip
# ---------------------------------------------------------------------------


def test_bootstrap_path_second_host_lands_on_server_mode(tmp_path, isolated_shared_server):
    remote = tmp_path / "origin.git"
    remote.mkdir()
    git("init", "-q", "--bare", "-b", "main", cwd=remote)
    host1 = _repo(tmp_path / "host1", remote=remote)
    try:
        onboard._act_bd_init(_ctx(host1, furnish=False))  # mints zero-footprint, server mode
        assert store_locator.dolt_mode(host1) == "server"
        from harness.beads import bd

        bd("dolt", "push", cwd=host1, capture=True, timeout=_TIMEOUT)
        assert git("ls-remote", "origin", "refs/dolt/data", cwd=host1).stdout.strip()

        host2 = tmp_path / "host2"
        git("clone", "-q", str(remote), str(host2))
        assert not (host2 / ".beads").exists()  # zero-footprint: nothing was ever committed

        onboard._act_bd_init(_ctx(host2, furnish=False))

        assert store_locator.dolt_mode(host2) == "server"
        # dolt_status must be clean — the GH#2455 bug never applies to a clone-based path
        # (bh-areg.2), re-confirmed here under server mode specifically for this bead.
        status = bd_json("sql", "--json", "SELECT * FROM dolt_status", cwd=host2)
        assert status == []
    finally:
        _stop_shared_server_best_effort(host1)


# ---------------------------------------------------------------------------
# Existing embedded hive genuinely untouched by re-running onboard (constraint 2)
# ---------------------------------------------------------------------------


def test_existing_embedded_hive_untouched_by_upgrade(tmp_path, isolated_shared_server):
    """Install (embedded — simulating a pre-upgrade hive on 0.7.x), "upgrade" (re-run onboard
    against the SAME hive — the code path an upgraded bh binary now runs), verify `dolt_mode`
    is UNCHANGED on disk and every verb still works."""
    from beadhive.run import run

    target = _repo(tmp_path / "existing")
    init = run(
        ["bd", "init", "--prefix", "widget", "--non-interactive", "--skip-agents", "--skip-hooks"],
        cwd=str(target),
        check=True,
        capture=True,
        timeout=_TIMEOUT,
    )
    assert init.returncode == 0
    assert store_locator.dolt_mode(target) == "embedded"
    create(target, "pre-existing issue")

    # "Upgrading" changes nothing about this call — it is the SAME idempotent onboard re-run
    # a bh upgrade puts an operator through; this bead's own point is that the new
    # server-mode-by-default wiring must never fire for it.
    onboard._act_bd_init(_ctx(target, furnish=False))

    assert store_locator.dolt_mode(target) == "embedded"  # unchanged, byte for byte
    titles = {row["title"] for row in bd_json("list", cwd=target) or []}
    assert "pre-existing issue" in titles  # every verb still works
    assert create(target, "post-upgrade issue")  # writes still work too


def test_rerunning_onboard_on_a_server_mode_hive_is_a_no_op(tmp_path, isolated_shared_server):
    """Acceptance: re-running onboard on an existing hive is still a no-op and never
    misreports the mode — pinned down for a hive that is ALREADY server mode (not just the
    embedded case above)."""
    target = _repo(tmp_path / "hive3")
    try:
        onboard._act_bd_init(_ctx(target, furnish=False))
        assert store_locator.dolt_mode(target) == "server"
        metadata_before = (target / ".beads" / "metadata.json").read_text()

        onboard._act_bd_init(_ctx(target, furnish=False))  # re-run: idempotent skip path

        assert store_locator.dolt_mode(target) == "server"
        assert (target / ".beads" / "metadata.json").read_text() == metadata_before
    finally:
        _stop_shared_server_best_effort(target)


# ---------------------------------------------------------------------------
# Busy port (the review's own reproduction): legible failure, nothing left behind,
# a clean retry that needs no --skip-check and produces a REAL store.
# ---------------------------------------------------------------------------


def test_furnished_path_busy_port_fails_legibly_and_leaves_nothing_behind(
    tmp_path, isolated_shared_server, capfd
):
    port = int(os.environ["BEADS_DOLT_SERVER_PORT"])
    target = _repo(tmp_path / "hive_busy")
    with _occupy_port(port), pytest.raises(typer.Exit) as exc:
        onboard._act_bd_init(_ctx(target, furnish=True))
    assert exc.value.exit_code == 1

    out = capfd.readouterr()
    combined = out.out + out.err
    # bd's own actionable message streamed through (never paraphrased away) …
    assert "port" in combined.lower()
    # … and bh's own wrapper never let a raw exception/traceback reach the terminal.
    assert "CalledProcessError" not in combined
    assert "Traceback (most recent call last)" not in combined

    # Nothing left behind: no .beads/, no stray root .gitignore, clean git status.
    assert not (target / ".beads").exists()
    assert not (target / ".gitignore").exists()
    assert git("status", "--porcelain", cwd=target).stdout.strip() == ""

    # The retry — port now free — needs NO --skip-check and produces a REAL store, not a hive
    # reported ready with no store (the review's worse finding).
    try:
        onboard._act_bd_init(_ctx(target, furnish=True))
        assert store_locator.dolt_mode(target) == "server"
        assert create(target, "post-retry issue")
    finally:
        _stop_shared_server_best_effort(target)


def test_zero_footprint_path_busy_port_fails_legibly_and_leaves_nothing_behind(
    tmp_path, isolated_shared_server
):
    port = int(os.environ["BEADS_DOLT_SERVER_PORT"])
    target = _repo(tmp_path / "hive_busy_zf")
    with _occupy_port(port), pytest.raises(typer.Exit):
        onboard._act_bd_init(_ctx(target, furnish=False))

    assert not (target / ".beads").exists()
    assert not (target / ".gitignore").exists()
    assert git("status", "--porcelain", cwd=target).stdout.strip() == ""

    try:
        onboard._act_bd_init(_ctx(target, furnish=False))  # retry, no --skip-check needed
        assert store_locator.dolt_mode(target) == "server"
    finally:
        _stop_shared_server_best_effort(target)


def test_hub_ensure_store_busy_port_fails_legibly_and_leaves_nothing_behind(
    tmp_path, isolated_shared_server
):
    """`hub.ensure_store` (`bh hq init` / the legacy hub) shares the same failure shape and
    the same fix — covered here since it's a distinct call site from `_act_bd_init`.
    `hub._bd_ni_env()` reads `os.environ` fresh on every call (bh-areg.7's review, round 3 —
    it used to be a module-level snapshot frozen at import time), so this test's
    `isolated_shared_server` overrides reach the `bd init` subprocess with no extra wiring."""
    port = int(os.environ["BEADS_DOLT_SERVER_PORT"])
    store = tmp_path / "hub-store"
    with _occupy_port(port), pytest.raises(typer.Exit):
        hub.ensure_store(store, "hub")

    assert not (store / ".beads").exists()

    try:
        hub.ensure_store(store, "hub")  # retry, port now free
        assert store_locator.dolt_mode(store) == "server"
    finally:
        _stop_shared_server_best_effort(store)


# ---------------------------------------------------------------------------
# Interrupted mid-init (the reviewer's own SIGKILL reproduction, round 4): a real
# `bd init --shared-server` killed partway through. The review measured `metadata.json`
# carrying a persisted `dolt_mode` ~4.5s into ITS machine's run, sharply before the store was
# actually openable — a delay tied to one machine's own timing, so this scans a range of
# kill-points (as fractions of a fresh, uninterrupted control run on THIS machine) rather than
# hard-coding that figure, and stops at the first one that reproduces the exact shape: on
# disk and dolt_mode-tagged, but not open.
# ---------------------------------------------------------------------------


def _bd_init_argv():
    return [
        "bd",
        "init",
        "--prefix",
        "widget",
        "--shared-server",
        "--non-interactive",
        "--skip-agents",
        "--skip-hooks",
    ]


def _attempt_env(shared_dir, port):
    return dict(
        os.environ,
        BD_NON_INTERACTIVE="1",
        BEADS_SHARED_SERVER_DIR=str(shared_dir),
        BEADS_DOLT_SERVER_PORT=str(port),
    )


def _kill_server(shared_dir):
    """Stop the shared-server instance rooted at *shared_dir* by its OWN pid file, never via
    `bd -C <target> dolt stop` — that depends on the LOCAL store's own metadata being
    complete, which is exactly what a killed-mid-init target does not have (measured: it
    silently no-ops, leaking the dolt sql-server). Kills by exact PID, verified against the
    process's own command line naming this scratch `shared_dir` first, so this can never
    touch anything but a server this test itself started (same discipline as the operator's
    real `~/.beads/shared-server/` staying untouched)."""
    import signal

    pid_file = shared_dir / "dolt-server.pid"
    if not pid_file.exists():
        return
    try:
        pid = int(pid_file.read_text().strip())
    except (ValueError, OSError):
        return
    try:
        cmdline = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return
    if str(shared_dir) not in cmdline:
        return  # can't verify it's ours — never touch it
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGKILL)


def test_interrupted_mid_init_never_reported_ready(tmp_path):
    # Control run: an UNINTERRUPTED init's own duration, to calibrate kill-points to this
    # machine rather than a figure measured on the reviewer's.
    control_shared = tmp_path / "shared-control"
    control = _repo(tmp_path / "control")
    control_env = _attempt_env(control_shared, _free_port())
    t0 = time.monotonic()
    subprocess.run(
        _bd_init_argv(),
        cwd=str(control),
        env=control_env,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=_TIMEOUT,
    )
    full_duration = time.monotonic() - t0
    _kill_server(control_shared)

    found = None
    try:
        for frac in (0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95):
            target = _repo(tmp_path / f"scan-{frac}")
            shared_dir = tmp_path / f"shared-{frac}"
            env = _attempt_env(shared_dir, _free_port())
            proc = subprocess.Popen(
                _bd_init_argv(),
                cwd=str(target),
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            delay = full_duration * frac
            t0 = time.monotonic()
            exited_naturally = False
            while time.monotonic() - t0 < delay:
                if proc.poll() is not None:
                    exited_naturally = True
                    break
                time.sleep(0.02)
            if not exited_naturally:
                proc.kill()
            proc.wait(timeout=15)

            metadata = target / ".beads" / "metadata.json"
            with contextlib.suppress(json.JSONDecodeError, OSError):
                has_mode = metadata.exists() and json.loads(metadata.read_text()).get("dolt_mode")
                if not exited_naturally and has_mode and not hive.store_opens(target):
                    found = (target, shared_dir)
                    break
            _kill_server(shared_dir)

        if found is None:
            pytest.skip(
                f"could not reproduce the interrupted-mid-init window on this machine "
                f"(control run={full_duration:.2f}s) — the invariant is still enforced by the "
                "hermetic branch tests, just not exercised by this scan this time"
            )
        target, shared_dir = found

        # The exact shape the review found: on disk, dolt_mode-tagged, but does not open.
        assert (target / ".beads").exists()
        assert store_locator.dolt_mode(target) == "server"
        assert hive.store_opens(target) is False

        # Re-running onboard against this exact wreckage must never report it ready: either a
        # legible refusal, or a real re-mint — never a silent "already initialized" skip.
        try:
            onboard._act_bd_init(_ctx(target, furnish=False))
        except typer.Exit as exc:
            assert exc.exit_code == 1
        else:
            # Didn't refuse — must have produced a REAL working store, not a false-ready one.
            assert store_locator.dolt_mode(target) == "server"
            assert hive.store_opens(target) is True
            assert create(target, "post-recovery issue")
    finally:
        if found is not None:
            _kill_server(found[1])
