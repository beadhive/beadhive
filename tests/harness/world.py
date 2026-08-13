"""Isolated AGF test world: tmp roots, env wiring, ephemeral signing keys + allowed_signers.

A `World` carves out a hermetic sandbox: its own $GIT_WORKSPACE, bh home/config/worktrees,
an empty global git config (so the real ~/.gitconfig never leaks), and a `keys/` dir of
ephemeral ed25519 signing keys with a cumulative allowed_signers file. Identity + signing
config is written **repo-local** by the hive builder (bh's own git calls scrub GIT_* incl.
GIT_CONFIG_GLOBAL, so global config is unreliable for bh-driven ops — repo-local always wins).
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import signal
import socket
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from beadhive import host
from beadhive.run import ps_argv, run


def free_port() -> int:
    """An ephemeral port the kernel says is free right now. Anything that starts a dolt
    sql-server under test binds one of these instead of a literal — a hardcoded port is only
    free until something else (a parallel worker, or a stray server a crashed run or a manual
    spike left behind) takes it, and then that test fails forever on that machine for a reason
    that has nothing to do with what it tests."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


#: How many tests may hold a real dolt sql-server AT THE SAME TIME across the whole run (bh-wa3ch).
#:
#: WHY A CEILING EXISTS AT ALL. `-n auto` is 24 workers for 54 integration tests on the box this
#: was measured on, many of which start a real dolt sql-server — a real process, a real port, real
#: memory. Nothing bounded how many ran at once: MEASURED at 16 concurrent servers in an unbounded
#: fenced run. That is independently wrong however the suite happens to behave, because it makes
#: every measurement on a loaded machine untrustworthy and turns a contention failure into a
#: mystery. It is NOT filed as the fix for bh-njdxk's four unexplained failures (see that bead).
#:
#: WHY 4, AND WHAT IT COST. Measured A/B on this box (24 cores), same fenced `-n auto` integration
#: selection, sampling only the servers the run itself owns:
#:
#:     BH_DOLT_SLOTS=0 (unbounded)   peak 9 servers   wall 141s
#:     BH_DOLT_SLOTS=4 (default)     peak 6 servers   wall 141s
#:
#: So the ceiling is free here: four server tests still overlap, which is enough to keep the slow
#: real-bd tests from draining one at a time. The suite's speed is why `-n auto` exists, so this
#: number is a measurement, not a preference — re-measure before changing it.
#:
#: NOTE the unit: this bounds TESTS holding a slot, not server processes. A slot-holding test may
#: start more than one server (the hq-backup round trip runs a source and a destination store), so
#: the process ceiling is a small multiple of this — which is why the bound is verified by
#: SAMPLING a real run rather than by arithmetic.
#:
#: `BH_DOLT_SLOTS` overrides it, and `BH_DOLT_SLOTS=0` disables the bound entirely. That is not a
#: convenience knob: the acceptance criterion here is a MEASUREMENT ("observed to stay at or under
#: the bound"), and a measurement needs both arms. It is also the lever for a box that is not this
#: one — a 4-core laptop may want 1.
MAX_CONCURRENT_DOLT_SERVER_TESTS = int(os.environ.get("BH_DOLT_SLOTS", "4"))

#: Give up waiting for a slot and run anyway. A test that starts a server takes ~30-90s, so this
#: is many times the worst honest wait. Deliberately non-fatal: a concurrency ceiling that can
#: FAIL a run is a worse trade than the contention it prevents, so a timeout narrates and proceeds.
_SLOT_WAIT_TIMEOUT = 600.0


@contextlib.contextmanager
def dolt_server_slot(slots: int = MAX_CONCURRENT_DOLT_SERVER_TESTS):
    """Hold one of *slots* run-wide permits to start a real dolt sql-server.

    A file lock rather than an xdist group, and the difference matters: `--dist loadgroup` +
    `@pytest.mark.xdist_group` only bounds a run that was invoked with that flag, so a bare
    `pytest -n auto` — or anyone running one file directly — is unbounded again. `flock` bounds
    every invocation, including the fenced land gate, and the kernel releases the lock when the
    holder dies, so a crashed or SIGKILLed worker cannot wedge a slot (the failure mode a
    hand-rolled pidfile semaphore would have).

    The lock files live under TMPDIR, which every xdist worker in one run shares — and which
    inside the bubblewrap fence is the run's own scratch bind, so a fenced run cannot contend with
    an unfenced one or with the operator's other work.

    ``slots <= 0`` is the unbounded arm of the measurement (``BH_DOLT_SLOTS=0``) — no lock at all.
    """
    if slots <= 0:
        yield -1
        return
    slot_dir = Path(tempfile.gettempdir()) / "bh-dolt-server-slots"
    slot_dir.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + _SLOT_WAIT_TIMEOUT
    while True:
        for index in range(slots):
            handle = (slot_dir / f"slot-{index}").open("a+")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                handle.close()
                continue
            try:
                yield index
                return
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()
        if time.monotonic() >= deadline:
            print(
                f"dolt server slot: waited {_SLOT_WAIT_TIMEOUT:.0f}s for one of {slots} — running "
                f"UNBOUNDED rather than failing the test",
                file=sys.stderr,
                flush=True,
            )
            yield -1
            return
        time.sleep(0.25)


def reap_dolt_server(server_dir: Path | str) -> None:
    """Terminate a dolt sql-server started under `server_dir`, via the pidfile bd writes there.
    The companion to :func:`free_port`: that stops a stray server breaking a LATER run, this
    stops one being left behind at all. Idempotent and silent when there is nothing to reap.

    `bd dolt stop` CANNOT do this job, which is why the obvious call is not the one here
    (bh-cbou). It resolves the server from `.beads/metadata.json`'s `dolt_mode`, and the
    `--reinit-local` path deliberately leaves that at "embedded" — the exact drift
    `test_storage_migrate_int` exists to measure — so bd refuses:

        Error: 'bd dolt stop' is not supported in embedded mode (no Dolt server)

    while the server keeps running. The cleanup was defeated by the very bug its test proves,
    and because the call was `check=False` the refusal was swallowed: measured at 16 accumulated
    `dolt sql-server` processes on the operator's Mac, one per suite run since 2026-08-04, each
    holding a port and a tmpdir pytest had already deleted out from under it.

    KEYED ON THE CALLER'S OWN DIR, never a name match. A `pkill -f "dolt sql-server"` would take
    the operator's real `~/.beads/shared-server` with it; a pidfile under a test's own tmp_path
    cannot name anything but that test's server.
    """
    pid_file = Path(server_dir) / "dolt-server.pid"
    if not pid_file.is_file():
        return
    try:
        pid = int(pid_file.read_text().strip())
    except (OSError, ValueError):
        return
    _terminate(pid)


def _terminate(pid: int) -> None:
    """SIGTERM, a grace period, then SIGKILL. Shared by the pidfile reap and the session sweep so
    both close a dolt store the same way: SIGTERM lets dolt flush and close cleanly, and only a
    server still alive after ~5s is killed outright."""
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGTERM)
        for _ in range(50):  # ~5s: SIGTERM lets dolt close its store cleanly
            time.sleep(0.1)
            os.kill(pid, 0)  # raises OSError once it is gone — that is the success exit
        os.kill(pid, signal.SIGKILL)  # still alive after the grace period: stop asking


def orphaned_dolt_servers(tmp_root: Path | str) -> list[tuple[int, str]]:
    """``(pid, config_path)`` for every running dolt sql-server that is UNAMBIGUOUSLY orphaned:
    its ``--config`` path lies under *tmp_root* and no longer exists on disk.

    Both halves of that test are load-bearing, and neither is a heuristic:

    * UNDER *tmp_root* — the pytest tmp tree. The operator's own servers
      (``~/.beads/shared-server``, ``~/.beadhive/cache/<hive>/.beads``) live outside it and can
      never match, so this can never do what ``pkill -f "dolt sql-server"`` would.
    * PATH GONE — the config file's own directory has been deleted. A server whose configuration
      no longer exists cannot be serving anything a live run still needs; it is exactly the state
      observed on the operator's machine (servers pointing at pytest tmp dirs pytest's retention
      policy had already removed). A run currently IN FLIGHT still has its directory, so a
      parallel session's servers are never candidates.

    Reads ``ps`` (POSIX, so this works the same on macOS where there is no fence) rather than
    pgrep's Linux-only ``-a``. Returns [] on any failure — a backstop must never be the thing that
    breaks a run.

    ``-ww`` IS LOAD-BEARING, not tidiness: ``ps`` truncates each command line to ``$COLUMNS`` even
    when its output is a pipe, and pytest sets ``COLUMNS`` in its xdist workers. Without it the
    ``--config <path>`` this whole function keys on was cut off the end of the line, so the sweep
    found NOTHING while reporting success — measured, by these tests passing serially and failing
    under ``-n auto``. A silent-no-op backstop is worse than none. That knowledge sat here in
    prose and failed to transfer TWICE (bh-jwwls), so it now lives in :func:`run.ps_argv`, which
    this calls: prose does not get imported.
    """
    root = str(Path(tmp_root).resolve())
    try:
        res = run(ps_argv("pid=,args="), check=False, capture=True, timeout=30)
    except OSError:
        return []
    found: list[tuple[int, str]] = []
    for line in (res.stdout or "").splitlines():
        parts = line.split()
        # `dolt` anywhere in the command line rather than a basename test on argv[0]: a real
        # server is `<store>/bin/dolt sql-server --config …`, but under a shell wrapper argv[0] is
        # the interpreter. The narrowing that matters is the config path below, not this.
        if len(parts) < 2 or "dolt" not in line or "sql-server" not in parts:
            continue
        if "--config" not in parts:
            continue
        try:
            pid = int(parts[0])
            cfg = parts[parts.index("--config") + 1]
        except (ValueError, IndexError):
            continue
        if pid == os.getpid():
            continue
        if cfg.startswith(root + os.sep) and not Path(cfg).exists():
            found.append((pid, cfg))
    return found


def sweep_orphaned_dolt_servers(tmp_root: Path | str) -> list[tuple[int, str]]:
    """Reap every :func:`orphaned_dolt_servers` candidate under *tmp_root*. Returns what it killed.

    THE BACKSTOP FOR A TEARDOWN THAT NEVER RAN (bh-7wp2y). ``reap_dolt_server`` is a fixture
    finalizer, and a finalizer does not run if the pytest process is SIGKILLed or torn down
    externally mid-suite — which is what happened, repeatedly: six consecutive sessions (pytest
    dirs 887, 890, 893, 895, 899, 901) each left the same slow real-dolt-server tests running.
    A per-fixture helper cannot defend against never being reached, so the backstop has to live
    outside any individual test's lifecycle. Wired in ``tests/conftest.py`` at session start and
    session end.

    WHAT IT CANNOT DO — the limits, stated because a backstop believed to be stronger than it is
    is worse than none:

    * It catches leaks only from ITS OWN PRIOR RUNS, and only once pytest's numbered-dir retention
      (3 sessions by default) has deleted the directory the leaked server points at. A server
      leaked by the last run is still holding its port and its memory until then.
    * It CANNOT catch a run currently in flight — deliberately. A live parallel session's config
      still exists on disk, which is precisely what keeps this sweep from killing it.
    * It cannot see a server started outside *tmp_root*, and must not: that is the operator's.
    * It is not the structural answer. Inside the bubblewrap fence (``scripts/hermetic.sh``)
      ``--unshare-all`` gives the run its own PID NAMESPACE and ``--die-with-parent`` ties bwrap
      and every descendant to the wrapper, so a leak is impossible rather than cleaned up after —
      verified directly: a process backgrounded inside the fence is gone once the fence exits.
      This sweep is for the paths where there IS no fence: macOS (bwrap is Linux-only),
      ``BH_HERMETIC=0``, and a bare ``pytest`` invocation that never went through the script.
    """
    killed = orphaned_dolt_servers(tmp_root)
    for pid, _cfg in killed:
        _terminate(pid)
    return killed


def progress(msg: str):
    """Live, flushed progress to stderr — only when AGF_RENDER is set (so a normal
    `just test-int` stays quiet, but `just render-int` streams what's happening)."""
    if os.environ.get("AGF_RENDER"):
        print(msg, file=sys.stderr, flush=True)


# Keep the harness's own git calls isolated: drop only the dir-pointing GIT_* vars (which would
# override `-C`), but KEEP GIT_CONFIG_GLOBAL/GIT_CONFIG_SYSTEM so no real user config leaks.
_DROP = {"GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE"}


def git_env() -> dict:
    return {k: v for k, v in os.environ.items() if k not in _DROP}


def git(*args, cwd=None, check=True):
    res = run(
        ["git", *args], cwd=str(cwd) if cwd else None, check=False, capture=True, env=git_env()
    )
    if check and res.returncode != 0:
        raise AssertionError(
            f"git {' '.join(map(str, args))} → {res.returncode}\n{res.stdout}\n{res.stderr}"
        )
    return res


@dataclass(frozen=True)
class Identity:
    """An author/committer identity, optionally with an ed25519 signing key (private path)."""

    name: str
    email: str
    key: Path | None = None  # private key path; None → unsigned

    @property
    def pub(self) -> Path | None:
        return Path(str(self.key) + ".pub") if self.key else None


class World:
    def __init__(self, tmp_path: Path, monkeypatch):
        self.tmp = Path(tmp_path)
        self.ws_root = self.tmp / "workspace"  # $GIT_WORKSPACE
        self.wts = self.tmp / "wts"  # $BH_WORKTREES
        self.home = self.tmp / "wshome"  # $BH_HOME
        self.keys = self.tmp / "keys"
        self.remotes = self.tmp / "remotes"
        self.sandboxes = self.tmp / "sandboxes"
        self.cfg_path = self.tmp / "config.yaml"
        self.allowed = self.tmp / "allowed_signers"
        self.gitconfig = self.tmp / "gitconfig"  # $GIT_CONFIG_GLOBAL
        for d in (self.ws_root, self.keys, self.remotes, self.sandboxes):
            d.mkdir(parents=True, exist_ok=True)
        self.allowed.write_text("")
        # core.excludesFile=/dev/null: git falls back to $XDG_CONFIG_HOME/git/ignore for the
        # global excludes file independent of this config's own contents, so a developer's
        # personal ignore rules (e.g. a `.beads/` rule) would otherwise leak into git calls
        # meant to be hermetic. Pin it here rather than relying on GIT_CONFIG_GLOBAL being empty.
        self.gitconfig.write_text("[core]\n\texcludesFile = /dev/null\n")

        for k, v in {
            "GIT_WORKSPACE": str(self.ws_root),
            "BH_WORKTREES": str(self.wts),
            "BH_HOME": str(self.home),
            "BH_CONFIG": str(self.cfg_path),
            "GIT_CONFIG_GLOBAL": str(self.gitconfig),
            "GIT_CONFIG_SYSTEM": os.devnull,
            # Never paginate or prompt: under `pytest -s` the subprocesses inherit the real
            # TTY, and a bd/git pager (less) would block forever waiting for a keypress.
            "PAGER": "cat",
            "GIT_PAGER": "cat",
            "GIT_TERMINAL_PROMPT": "0",
            "BD_NON_INTERACTIVE": "1",
            "NO_COLOR": "1",
        }.items():
            monkeypatch.setenv(k, v)
        # Drop every ambient BEADS_/DOLT_ var so the operator's shell cannot redirect this
        # World's bd at their own state — then IMMEDIATELY re-point it at an isolated shared
        # server (below). The scrub alone used to be the whole story, under the comment "force
        # isolated embedded bd"; that stopped being true in bd v0.8.0 (bh-u5i2r). A scrubbed
        # environment is not an embedded one — `hub.ensure_store` passes `--shared-server`
        # unconditionally, and with the vars gone bd resolves its OWN defaults:
        # `~/.beads/shared-server` on the fixed port 3308, i.e. the host's real fleet server.
        # On any machine where that server is running — which is every machine bh is working
        # correctly on — four `test_host_retire` tests died on "port 3308 is busy", failing
        # BECAUSE the product was healthy. Same class as bh-dfz2 (the literal 3399), same fix:
        # never a literal port, never the real server dir.
        _prefixed = (k for k in os.environ if k.startswith(("BEADS_", "DOLT_")))
        # Inherit conftest's `_sandbox_shared_server` target when it is present so its reaper
        # still owns teardown; mint a World-local one otherwise (the `world` fixture reaps it).
        self.shared_server = Path(
            os.environ.get("BEADS_SHARED_SERVER_DIR") or self.tmp / "shared-server"
        )
        self.shared_port = int(os.environ.get("BEADS_DOLT_SERVER_PORT") or free_port())
        for k in (*_prefixed, "BH_CREW", "BH_DEV", "WS_CREW", "WS_DEV"):
            monkeypatch.delenv(k, raising=False)
        self.shared_server.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("BEADS_SHARED_SERVER_DIR", str(self.shared_server))
        monkeypatch.setenv("BEADS_DOLT_SERVER_PORT", str(self.shared_port))
        self._monkeypatch = monkeypatch

        self.cfg_path.write_text("providers: [github]\nmanaged_repos: []\n")
        # A World stands in for a PROVISIONED machine, so it owes the same two files `bh config
        # init` mints: config.yaml (above) and host.yaml. The latter was implicit until the
        # liveness sweep started resolving `host.host_id()` on the submit path (bh-nikb) — with
        # no host.yaml under this sandbox's $BH_HOME every harness submit died on
        # `FileNotFoundError: bh host identity not found`, which is what took the whole matrix
        # red. Mint it here (not in the modalities) so any future host-keyed code path finds it.
        host.mint_if_needed()
        # The fabricated human (supervised modality) and the merge owner (Refiner).
        self.human = self.identity("Human Dev", "human@fixture", sign=True)
        self.refiner = self.identity("Refiner", "refiner@fixture", sign=True)

    def identity(self, name: str, email: str, sign: bool = True) -> Identity:
        """Make an Identity; when sign=True, generate an ed25519 key and register it as an
        allowed signer for `email` so git verification yields a good ('G') signature."""
        key = None
        if sign:
            key = self.keys / f"{email.replace('@', '_at_')}"
            if not key.exists():
                run(
                    ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", email, "-f", str(key), "-q"],
                    check=True,
                    capture=True,
                )
            pub = Path(str(key) + ".pub").read_text().strip()
            with self.allowed.open("a") as f:
                f.write(f'{email} namespaces="git" {pub}\n')
        return Identity(name, email, key)

    def chdir(self, path: Path):
        self._monkeypatch.chdir(path)
