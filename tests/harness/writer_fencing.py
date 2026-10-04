"""Multi-frame Dolt fault-injection fixture for writer-fencing scenarios (spike bh-eybn7).

One :class:`Cluster` is N frames (simulated hosts), ONE shared hive remote, and an HQ placement
stand-in, all under a single tmp root and nothing else:

* **Remote** — a bare Git repo used as a ``git+file://`` Dolt remote: the same gitblobstore path
  as production ``git+ssh`` (``refs/dolt/data`` behind ``push --force-with-lease``). It is seeded
  with one commit on ``main`` because Dolt refuses a branchless Git remote.
* **Frames** — each with its own HOME, Git/Dolt identity, ``PATH`` shim and driver:
  ``dolt-cli`` (a ``dolt clone``), ``bd-embedded`` (bd's in-process Dolt) or ``bd-server`` (bd's
  shared-server mode against a frame-private ``dolt sql-server`` on an ephemeral port). Every
  frame command runs inside that frame's own agent process (``harness.processes``, spawn), in a
  fresh session, so killing a frame takes its whole process group with it.
* **HQ** — :class:`HQ`, a flock-guarded JSON placement authority (writer + epoch, CAS-bumped),
  with per-frame reachability.

Fault controls, identical for every driver because all three reach the remote by spawning
``git`` (see :mod:`harness.writer_fencing_gate`): :meth:`Frame.partition` / :meth:`Frame.heal`,
:meth:`Frame.hold` (a barrier parked at a named point such as :data:`CAS`), :meth:`Frame.kill`
/ :meth:`Frame.restart`. :class:`Recorder` snapshots remote head + remote ``bh_writer`` + HQ +
per-frame guard state before and after each step into a JSONL trace.

Used by the writer-fencing molecule (bh-qlgmm): bh-vje85, bh-sieai, bh-jbb6r. Test-only.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from harness.writer_fencing_gate import CAS, FETCH, INFO

__all__ = [
    "BD_EMBEDDED",
    "BD_SERVER",
    "CAS",
    "Checkpoint",
    "DOLT_CLI",
    "FETCH",
    "HQ",
    "INFO",
    "KINDS",
    "Cluster",
    "Frame",
    "HQUnreachable",
    "Hold",
    "Pending",
    "Placement",
    "Recorder",
    "Remote",
    "Run",
    "pid_alive",
]

DOLT_CLI = "dolt-cli"
BD_EMBEDDED = "bd-embedded"
BD_SERVER = "bd-server"
KINDS = (DOLT_CLI, BD_EMBEDDED, BD_SERVER)

DEFAULT_TIMEOUT = 180.0
_GATE = Path(__file__).with_name("writer_fencing_gate.py")
_REMOTE_SUBCOMMANDS = "fetch|push|pull|ls-remote|clone|fetch-pack|send-pack"
_PASSTHROUGH_ENV = ("LANG", "LC_ALL", "TZ", "TMPDIR")


# --------------------------------------------------------------------------------------------
# frame agent: one long-lived spawned process per frame, leader of its own session


def _frame_agent(conn) -> None:
    """Run commands sent by the controller, one at a time, inside this process's own session.

    Module-level so the spawn context can import it. ``setsid`` makes the agent a process-group
    leader: ``killpg`` on it reaches bd/dolt and the parked gate beneath, i.e. a host crash.
    """
    os.setsid()
    while True:
        try:
            msg = conn.recv()
        except (EOFError, OSError):
            return
        if msg is None:
            return
        argv, cwd, env, timeout = msg
        started = time.monotonic()
        try:
            res = subprocess.run(
                argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout
            )
            reply = (res.returncode, res.stdout, res.stderr)
        except subprocess.TimeoutExpired as exc:
            reply = (124, _text(exc.stdout), _text(exc.stderr) + f"\ntimeout after {timeout}s")
        except OSError as exc:
            reply = (127, "", repr(exc))
        try:
            conn.send((*reply, time.monotonic() - started))
        except (BrokenPipeError, OSError):
            return


def _text(value) -> str:
    if value is None:
        return ""
    return value.decode(errors="replace") if isinstance(value, bytes) else value


@dataclass(frozen=True)
class Run:
    """One finished frame command. ``killed`` when the frame died under it."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    seconds: float
    killed: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def output(self) -> str:
        return self.stdout + self.stderr

    def check(self) -> Run:
        if not self.ok:
            raise AssertionError(
                f"{' '.join(self.argv)} -> {self.returncode}\n{self.stdout}\n{self.stderr}"
            )
        return self


class Pending:
    """A command in flight on a frame's agent. At most one per frame at a time."""

    def __init__(self, frame: Frame, argv: tuple[str, ...], timeout: float):
        self._frame = frame
        self.argv = argv
        self._timeout = timeout
        self._result: Run | None = None

    def done(self) -> bool:
        if self._result is not None:
            return True
        conn = self._frame._conn
        try:
            return conn is None or conn.poll(0)
        except (EOFError, OSError):
            return True

    def result(self, timeout: float | None = None) -> Run:
        if self._result is not None:
            return self._result
        wait = self._timeout + 10.0 if timeout is None else timeout
        conn = self._frame._conn
        try:
            if conn is None:
                raise EOFError
            if not conn.poll(wait):
                raise TimeoutError(f"{self._frame.name}: {' '.join(self.argv)} still running")
            code, out, err, seconds = conn.recv()
            self._result = Run(self.argv, code, out, err, seconds)
        except (EOFError, OSError):
            self._result = Run(self.argv, -signal.SIGKILL, "", "frame killed", 0.0, killed=True)
        if self._frame._pending is self:
            self._frame._pending = None
        return self._result


# --------------------------------------------------------------------------------------------
# HQ placement stand-in


class HQUnreachable(RuntimeError):
    """The asking frame is partitioned from HQ."""


@dataclass(frozen=True)
class Placement:
    writer: str | None
    epoch: int


class HQ:
    """Placement authority stand-in: one ``(writer, epoch)`` per hive, CAS-bumped under flock.

    ``asker`` names the frame making the call; a frame partitioned from HQ gets
    :class:`HQUnreachable`. ``asker=None`` is the test/operator, always reachable.
    """

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self._state = root / "placement.json"
        self._lock = root / "placement.lock"
        self._cut: set[str] = set()

    def partition(self, frame: str) -> None:
        self._cut.add(frame)

    def heal(self, frame: str) -> None:
        self._cut.discard(frame)

    def reachable(self, frame: str) -> bool:
        return frame not in self._cut

    @contextlib.contextmanager
    def _locked(self, asker: str | None) -> Iterator[dict]:
        if asker is not None and asker in self._cut:
            raise HQUnreachable(f"{asker} cannot reach HQ")
        with open(self._lock, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = json.loads(self._state.read_text()) if self._state.exists() else {}
            yield state
            self._state.write_text(json.dumps(state, sort_keys=True))

    def placement(self, hive: str = "hive", *, asker: str | None = None) -> Placement:
        with self._locked(asker) as state:
            row = state.get(hive, {})
            return Placement(row.get("writer"), int(row.get("epoch", 0)))

    def place(
        self,
        writer: str,
        hive: str = "hive",
        *,
        expected_epoch: int | None = None,
        asker: str | None = None,
    ) -> Placement:
        """Assign ``writer`` and bump the epoch; refuse if ``expected_epoch`` is stale."""
        with self._locked(asker) as state:
            row = state.get(hive, {"writer": None, "epoch": 0})
            if expected_epoch is not None and row["epoch"] != expected_epoch:
                raise ValueError(f"stale placement: epoch {row['epoch']} != {expected_epoch}")
            row = {"writer": writer, "epoch": int(row["epoch"]) + 1}
            state[hive] = row
            return Placement(row["writer"], row["epoch"])

    def restore(self, placement: Placement, hive: str = "hive") -> None:
        """Operator override (no CAS, no bump): put ``placement`` back, e.g. after a rewind."""
        with self._locked(None) as state:
            state[hive] = {"writer": placement.writer, "epoch": placement.epoch}


# --------------------------------------------------------------------------------------------
# remote


class Remote:
    """The shared hive remote (bare Git repo) plus a CLI observer clone for reading its data."""

    def __init__(self, root: Path):
        self.path = root / "remote.git"
        self.url = f"git+file://{self.path}"
        self.git_url = f"file://{self.path}"
        self._observer = root / "observer"
        self._env: dict[str, str] = {}
        self._cache: tuple[tuple, dict] | None = None

    def head(self) -> str | None:
        """The ``refs/dolt/data`` commit on the remote — changes on every successful data push."""
        res = subprocess.run(
            ["git", "--git-dir", str(self.path), "rev-parse", "-q", "--verify", "refs/dolt/data"],
            capture_output=True,
            text=True,
            env=self._env or None,
        )
        return res.stdout.strip() or None

    def _dolt(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["dolt", *args],
            cwd=self._observer,
            capture_output=True,
            text=True,
            env=self._env,
            timeout=DEFAULT_TIMEOUT,
        )

    def _attach(self, env: dict[str, str]) -> None:
        self._env = env
        res = subprocess.run(
            ["dolt", "clone", self.url, str(self._observer)],
            capture_output=True,
            text=True,
            env=env,
            timeout=DEFAULT_TIMEOUT,
        )
        if res.returncode:
            raise AssertionError(f"observer clone failed: {res.stdout}{res.stderr}")

    def view(self, tables: Sequence[str] = ("bh_writer",)) -> dict:
        """``{"head", "main", "tables": {name: rows | None}}`` of remote ``main``.

        Cached by remote head, so a snapshot of an unchanged remote costs one ``rev-parse``.
        """
        head = self.head()
        key = (head, tuple(tables))
        if self._cache is not None and self._cache[0] == key:
            return self._cache[1]
        fetched = self._dolt("fetch", "origin")
        if fetched.returncode:
            raise AssertionError(f"observer fetch failed: {fetched.stdout}{fetched.stderr}")
        main = _json_rows(self._dolt("sql", "-r", "json", "-q", "select hashof('origin/main') h"))
        present = {
            next(iter(row.values()))
            for row in _json_rows(
                self._dolt("sql", "-r", "json", "-q", "show tables as of 'origin/main'")
            )
        }
        view = {
            "head": head,
            "main": main[0]["h"] if main else None,
            "tables": {
                name: (
                    _json_rows(
                        self._dolt(
                            "sql", "-r", "json", "-q", f"select * from `{name}` as of 'origin/main'"
                        )
                    )
                    if name in present
                    else None
                )
                for name in tables
            },
        }
        self._cache = (key, view)
        return view


def _json_rows(res: subprocess.CompletedProcess | Run) -> list[dict]:
    if res.returncode:
        raise AssertionError(f"sql failed ({res.returncode}): {res.stdout}{res.stderr}")
    text = res.stdout.strip()
    if not text:
        return []
    data = json.loads(text)
    return data.get("rows", []) if isinstance(data, dict) else data


# --------------------------------------------------------------------------------------------
# frames


@dataclass
class Hold:
    """A barrier armed at ``point`` on one frame's remote gate."""

    frame: Frame
    point: str

    @property
    def _arrived(self) -> Path:
        return self.frame.ctl / "arrived" / self.point

    def arrived(self) -> bool:
        return self._arrived.exists()

    def wait(self, timeout: float = 60.0) -> int:
        """Block until the frame is parked at the point; return the parked gate's pid."""
        deadline = time.monotonic() + timeout
        while not self._arrived.exists():
            pending = self.frame._pending
            if pending is not None and pending.done():
                run = pending.result(0)
                raise AssertionError(
                    f"{self.frame.name} finished before reaching {self.point}: {run.output}"
                )
            if time.monotonic() > deadline:
                raise TimeoutError(f"{self.frame.name} never reached {self.point}")
            time.sleep(0.01)
        return int(self._arrived.read_text())

    def release(self) -> None:
        """Let the parked call proceed (or disarm the hold if nothing reached it yet)."""
        for path in (
            self._arrived,
            self.frame.ctl / "held" / self.point,
            self.frame.ctl / "holds" / self.point,
        ):
            path.unlink(missing_ok=True)


@dataclass
class Frame:
    """One simulated host: identity, HOME, PATH shim, driver, agent process and fault state."""

    cluster: Cluster
    name: str
    kind: str
    dir: Path
    port: int | None = None
    env: dict[str, str] = field(default_factory=dict)
    _proc: object | None = None
    _conn: object | None = None
    _pending: Pending | None = None

    # -- layout -------------------------------------------------------------------------------
    @property
    def home(self) -> Path:
        return self.dir / "home"

    @property
    def ctl(self) -> Path:
        return self.dir / "ctl"

    @property
    def hive(self) -> Path:
        """The frame's working checkout: a Git clone for bd frames, a Dolt clone for CLI."""
        return self.dir / "hive"

    @property
    def server_dir(self) -> Path:
        return self.dir / "server"

    @property
    def is_bd(self) -> bool:
        return self.kind in (BD_EMBEDDED, BD_SERVER)

    @property
    def dolt_dir(self) -> Path:
        """Where the Dolt CLI can open this frame's store directly (not for ``bd-server``)."""
        if self.kind == BD_EMBEDDED:
            return self.hive / ".beads" / "embeddeddolt" / self.cluster.prefix
        return self.hive

    def _layout(self) -> None:
        for sub in ("home/.dolt", "home/.config", "ctl/holds", "ctl/held", "ctl/arrived", "bin"):
            (self.dir / sub).mkdir(parents=True, exist_ok=True)
        (self.home / ".gitconfig").write_text(
            f"[user]\n\tname = {self.name}\n\temail = {self.name}@frames.invalid\n"
            "[init]\n\tdefaultBranch = main\n[advice]\n\tdetachedHead = false\n"
        )
        (self.home / ".dolt" / "config_global.json").write_text(
            json.dumps(
                {
                    "user.name": self.name,
                    "user.email": f"{self.name}@frames.invalid",
                    "metrics.disabled": "true",
                }
            )
        )
        real_git = shutil.which("git")
        if real_git is None:
            raise RuntimeError("git not on PATH")
        shim = self.dir / "bin" / "git"
        shim.write_text(
            "#!/bin/sh\n"
            "# writer-fencing fixture shim: local plumbing passes straight through; remote\n"
            "# subcommands go through the frame's gate (partition / hold / log).\n"
            "sub=; skip=\n"
            'for a in "$@"; do\n'
            '  if [ -n "$skip" ]; then skip=; continue; fi\n'
            '  case "$a" in\n'
            "    -C|-c|--git-dir|--work-tree|--namespace|--exec-path|--super-prefix|--config-env)"
            " skip=1 ;;\n"
            "    -*) ;;\n"
            "    *) sub=$a; break ;;\n"
            "  esac\n"
            "done\n"
            f'case "$sub" in {_REMOTE_SUBCOMMANDS})\n'
            f'  exec "{sys.executable}" -I -S "{_GATE}" "{self.ctl}" "{real_git}" "$@" ;;\n'
            "esac\n"
            f'exec "{real_git}" "$@"\n'
        )
        shim.chmod(0o755)
        path = os.pathsep.join([str(self.dir / "bin"), os.environ.get("PATH", "")])
        self.env = {
            "PATH": path,
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "DOLT_ROOT_PATH": str(self.home),
            "GIT_CONFIG_GLOBAL": str(self.home / ".gitconfig"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "BD_NON_INTERACTIVE": "1",
            "BEADS_ACTOR": self.name,
            **{k: os.environ[k] for k in _PASSTHROUGH_ENV if k in os.environ},
        }
        if self.kind == BD_SERVER:
            self.env.update(
                {
                    "BEADS_DOLT_SHARED_SERVER": "1",
                    "BEADS_SHARED_SERVER_DIR": str(self.server_dir),
                    "BEADS_DOLT_SERVER_PORT": str(self.port),
                }
            )

    # -- agent --------------------------------------------------------------------------------
    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.is_alive()

    def _start_agent(self) -> None:
        from harness.processes import process_context

        ctx = process_context()
        parent, child = ctx.Pipe()
        proc = ctx.Process(target=_frame_agent, args=(child,), name=f"frame-{self.name}")
        proc.daemon = True
        proc.start()
        child.close()
        self._proc, self._conn = proc, parent

    def spawn(self, *argv: str, cwd: Path | None = None, timeout: float = DEFAULT_TIMEOUT):
        """Start ``argv`` on this frame's agent without waiting; see :class:`Pending`."""
        if not self.alive:
            raise RuntimeError(f"frame {self.name} is down (kill() without restart())")
        if self._pending is not None and not self._pending.done():
            raise RuntimeError(f"frame {self.name} already runs {self._pending.argv}")
        cmd = tuple(str(a) for a in argv)
        self._conn.send((list(cmd), str(cwd or self.hive), self.env, timeout))
        self._pending = Pending(self, cmd, timeout)
        return self._pending

    def run(self, *argv: str, cwd: Path | None = None, timeout: float = DEFAULT_TIMEOUT) -> Run:
        return self.spawn(*argv, cwd=cwd, timeout=timeout).result()

    @property
    def busy(self) -> bool:
        return self._pending is not None and not self._pending.done()

    # -- driver ops ---------------------------------------------------------------------------
    def bd(self, *args: str, timeout: float = DEFAULT_TIMEOUT) -> Run:
        return self.run("bd", *args, timeout=timeout)

    def dolt(self, *args: str, timeout: float = DEFAULT_TIMEOUT) -> Run:
        return self.run("dolt", *args, cwd=self.dolt_dir, timeout=timeout)

    def sql(self, statement: str) -> Run:
        """Run SQL through the frame's own engine (no commit).

        ``bd-server`` goes through ``bd sql`` (the server bd manages). ``bd-embedded`` has no
        ``bd sql`` ("not yet supported in embedded mode", bd 1.3), so it opens the embedded store
        with the Dolt CLI — which is also what any bh-side SQL on an embedded hive would have
        to do. Do not call it while a bd command is in flight on the same frame.
        """
        if self.kind == BD_SERVER:
            return self.bd("sql", "--json", statement)
        return self.dolt("sql", "-r", "json", "-q", statement)

    def query(self, statement: str) -> list[dict]:
        return _json_rows(self.sql(statement))

    def tables(self) -> set[str]:
        return {next(iter(row.values())) for row in self.query("show tables")}

    def commit(self, message: str) -> Run:
        if self.kind == BD_SERVER:
            return self.bd("dolt", "commit", "-m", message)
        self.dolt("add", "-A").check()
        return self.dolt("commit", "-m", message)

    def create_issue(self, title: str) -> Run:
        """A canonical tracker write: ``bd create`` on bd frames, an INSERT + commit on CLI."""
        if self.is_bd:
            return self.bd("create", "--title", title, "-t", "task", "-p", "2", "--json")
        ident = f"{self.cluster.prefix}-{self.name}-{hashlib.sha1(title.encode()).hexdigest()[:6]}"
        self.sql(
            "insert into issues (id, title, description, design, acceptance_criteria, notes) "
            f"values ('{ident}', '{title}', '', '', '', '')"
        ).check()
        return self.commit(f"cli: {title}")

    def issue_titles(self) -> set[str]:
        return {row["title"] for row in self.query("select title from issues")}

    def push(self, timeout: float = DEFAULT_TIMEOUT) -> Run:
        return self.push_async(timeout).result()

    def push_async(self, timeout: float = DEFAULT_TIMEOUT) -> Pending:
        if self.is_bd:
            return self.spawn("bd", "dolt", "push", timeout=timeout)
        return self.spawn("dolt", "push", "origin", "main", cwd=self.dolt_dir, timeout=timeout)

    def pull(self, timeout: float = DEFAULT_TIMEOUT) -> Run:
        return self.pull_async(timeout).result()

    def pull_async(self, timeout: float = DEFAULT_TIMEOUT) -> Pending:
        if self.is_bd:
            return self.spawn("bd", "dolt", "pull", timeout=timeout)
        return self.spawn("dolt", "pull", "origin", "main", cwd=self.dolt_dir, timeout=timeout)

    def local_main(self) -> str:
        return self.query("select hashof('main') h")[0]["h"]

    # -- faults -------------------------------------------------------------------------------
    @property
    def partitioned(self) -> bool:
        return (self.ctl / "partitioned").exists()

    def partition(self) -> None:
        """Make the shared remote unreachable from this frame only (fails like a dead host)."""
        (self.ctl / "partitioned").touch()

    def heal(self) -> None:
        (self.ctl / "partitioned").unlink(missing_ok=True)

    def hold(self, point: str = CAS, *, skip: int = 0) -> Hold:
        """Arm a one-shot barrier: the next ``point`` call (after ``skip`` more) parks."""
        for stale in (self.ctl / "arrived" / point, self.ctl / "held" / point):
            stale.unlink(missing_ok=True)
        (self.ctl / "holds" / point).write_text(str(skip))
        return Hold(self, point)

    def holds(self) -> dict[str, list[str]]:
        return {
            "armed": sorted(p.name for p in (self.ctl / "holds").iterdir()),
            "held": sorted(p.name for p in (self.ctl / "held").iterdir()),
        }

    def gate_log(self) -> list[dict]:
        path = self.ctl / "git.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def server_pid(self) -> int | None:
        try:
            return int((self.server_dir / "dolt-server.pid").read_text().strip())
        except (OSError, ValueError):
            return None

    def kill(self) -> list[int]:
        """SIGKILL the frame like a host crash: its agent's whole process group (bd, dolt, a
        parked gate) and, for ``bd-server``, its sql-server and that server's children. Returns
        the pids it killed once every one of them is gone."""
        victims: list[int] = []
        for arrived in (self.ctl / "arrived").iterdir():  # parked gates (server-side ones too)
            with contextlib.suppress(OSError, ValueError):
                victims.append(int(arrived.read_text()))
        if self._proc is not None and self._proc.pid:
            victims.append(self._proc.pid)
            for kill in (os.killpg, os.kill):  # os.kill covers an agent not yet in its session
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    kill(self._proc.pid, signal.SIGKILL)
            self._proc.join(10)
        if self.kind == BD_SERVER:
            pid = self.server_pid()
            if pid is not None and pid_alive(pid):
                victims.append(pid)
                _kill_tree(pid)
        for pid in victims:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
        deadline = time.monotonic() + 10
        while any(pid_alive(pid) for pid in victims) and time.monotonic() < deadline:
            time.sleep(0.02)
        if self._conn is not None:
            self._conn.close()
        self._proc = self._conn = None
        if self._pending is not None and self._pending._result is None:
            self._pending._result = Run(
                self._pending.argv, -signal.SIGKILL, "", "frame killed", 0.0, killed=True
            )
        self._pending = None
        return victims

    def restart(self) -> Run | None:
        """Bring a killed frame back: new agent and, for ``bd-server``, a restarted server."""
        for sub in ("holds", "held", "arrived"):
            for path in (self.ctl / sub).iterdir():
                path.unlink(missing_ok=True)
        self._start_agent()
        if self.kind == BD_SERVER:
            return self.bd("dolt", "start").check()
        return None

    def stop(self) -> None:
        if self.kind == BD_SERVER and self.alive:
            with contextlib.suppress(Exception):
                self.bd("dolt", "stop", timeout=30)
        if self._conn is not None:
            with contextlib.suppress(OSError):
                self._conn.send(None)
        if self._proc is not None:
            self._proc.join(5)
            if self._proc.is_alive():
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(self._proc.pid, signal.SIGKILL)
                self._proc.join(5)
        if self.kind == BD_SERVER:
            from harness.world import reap_dolt_server

            reap_dolt_server(self.server_dir)
        self._proc = self._conn = None

    def guard(self, tables: Sequence[str] = ()) -> dict:
        """Fixture-side fault state, plus local ``tables`` rows when the frame is idle."""
        state = {
            "alive": self.alive,
            "busy": self.busy,
            "partitioned": self.partitioned,
            "hq_reachable": self.cluster.hq.reachable(self.name),
            **self.holds(),
        }
        if tables and self.alive and not self.busy:
            present = self.tables()
            state["tables"] = {
                t: self.query(f"select * from `{t}`") if t in present else None for t in tables
            }
        return state


def pid_alive(pid: int) -> bool:
    """Whether ``pid`` is a live process (a zombie awaiting reaping counts as dead)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    with contextlib.suppress(OSError):
        stat = Path(f"/proc/{pid}/stat").read_text()
        return stat.rsplit(")", 1)[1].split()[0] != "Z"
    return True


def _kill_tree(pid: int) -> None:
    """SIGKILL ``pid``'s process group when it leads one, else ``pid`` and its direct children."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        if os.getpgid(pid) == pid:
            os.killpg(pid, signal.SIGKILL)
            return
    children = subprocess.run(
        ["pgrep", "-P", str(pid)], capture_output=True, text=True
    ).stdout.split()
    for victim in [pid, *map(int, children)]:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(victim, signal.SIGKILL)


# --------------------------------------------------------------------------------------------
# cluster


class Cluster:
    """N frames + one ``git+file://`` hive remote + an HQ stand-in under ``root``.

    ``frames`` is a sequence of ``(name, kind)``. The founder (first bd frame, else the first
    frame) mints the Dolt store and publishes it; every other frame joins from the remote the
    way a new host would (``bd init`` clone-from-origin, or ``dolt clone``), in parallel.
    """

    def __init__(
        self,
        root: Path,
        frames: Sequence[tuple[str, str]],
        *,
        prefix: str = "fx",
        schema_sql: str = "create table issues (id varchar(64) primary key, title text, "
        "description text, design text, acceptance_criteria text, notes text)",
    ):
        if len(frames) < 1:
            raise ValueError("a cluster needs at least one frame")
        for _, kind in frames:
            if kind not in KINDS:
                raise ValueError(f"unknown frame kind {kind!r}")
        self.root = root
        self.prefix = prefix
        self.schema_sql = schema_sql
        self.remote = Remote(root)
        self.hq = HQ(root / "hq")
        self.timings: dict[str, float] = {}
        self.frames: dict[str, Frame] = {}
        for name, kind in frames:
            port = _free_port() if kind == BD_SERVER else None
            self.frames[name] = Frame(self, name, kind, root / "frames" / name, port=port)

    def __getitem__(self, name: str) -> Frame:
        return self.frames[name]

    def __enter__(self) -> Cluster:
        try:
            self.start()
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @contextlib.contextmanager
    def _timed(self, label: str) -> Iterator[None]:
        started = time.monotonic()
        yield
        self.timings[label] = round(time.monotonic() - started, 3)

    def start(self) -> None:
        with self._timed("remote"):
            self._seed_remote()
        for frame in self.frames.values():
            frame._layout()
            frame._start_agent()
        founder = next((f for f in self.frames.values() if f.is_bd), None) or next(
            iter(self.frames.values())
        )
        with self._timed(f"found:{founder.name}"):
            self._found(founder)
        joiners = [f for f in self.frames.values() if f is not founder]
        started = time.monotonic()
        # Two parallel phases (each frame has its own agent): fetch the hive, then attach.
        clones = [(f, self._clone_async(f)) for f in joiners]
        for _, pending in clones:
            pending.result().check()
        inits = [(f, self._attach_async(f)) for f in joiners]
        for frame, pending in inits:
            if pending is not None:
                pending.result().check()
            self.timings[f"join:{frame.name}"] = round(time.monotonic() - started, 3)
        with self._timed("observer"):
            self.remote._attach(self._observer_env())

    def _observer_env(self) -> dict[str, str]:
        """The recorder's own unshimmed identity: it must see the remote while frames cannot."""
        home = self.root / "observer-home"
        (home / ".dolt").mkdir(parents=True, exist_ok=True)
        (home / ".dolt" / "config_global.json").write_text(
            json.dumps(
                {
                    "user.name": "observer",
                    "user.email": "observer@frames.invalid",
                    "metrics.disabled": "true",
                }
            )
        )
        return {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(home),
            "DOLT_ROOT_PATH": str(home),
            "GIT_CONFIG_NOSYSTEM": "1",
        }

    def _seed_remote(self) -> None:
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(self.root),
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        seed = self.root / "seed"
        for args in (
            ["git", "init", "-q", "--bare", "-b", "main", str(self.remote.path)],
            ["git", "init", "-q", "-b", "main", str(seed)],
            [
                "git",
                "-C",
                str(seed),
                "-c",
                "user.name=seed",
                "-c",
                "user.email=seed@invalid",
                "commit",
                "-q",
                "--allow-empty",
                "-m",
                "seed",
            ],
            ["git", "-C", str(seed), "push", "-q", self.remote.git_url, "HEAD:main"],
        ):
            subprocess.run(args, check=True, capture_output=True, env=env)
        shutil.rmtree(seed)
        self.remote._env = env

    def _found(self, frame: Frame) -> None:
        if frame.is_bd:
            self._clone_async(frame).result().check()
            frame.bd(*self._init_args(frame)).check()
        else:
            frame.hive.mkdir(parents=True)
            frame.dolt("init", "-b", "main").check()
            frame.sql(self.schema_sql).check()
            frame.commit("seed schema").check()
            frame.dolt("remote", "add", "origin", self.remote.url).check()
        frame.push().check()

    def _init_args(self, frame: Frame) -> list[str]:
        args = [
            "init",
            "--prefix",
            self.prefix,
            "--non-interactive",
            "--skip-agents",
            "--skip-hooks",
        ]
        if frame.kind == BD_SERVER:
            args.insert(1, "--shared-server")
        return args

    def _clone_async(self, frame: Frame) -> Pending:
        """A new host fetching the hive: a Git clone for bd frames, a Dolt clone for CLI."""
        if frame.is_bd:
            return frame.spawn(
                "git", "clone", "-q", self.remote.git_url, str(frame.hive), cwd=frame.dir
            )
        return frame.spawn("dolt", "clone", self.remote.url, str(frame.hive), cwd=frame.dir)

    def _attach_async(self, frame: Frame) -> Pending | None:
        """bd's join: ``bd init`` in a clone whose origin carries ``refs/dolt/data`` clones the
        published store instead of minting one (``--shared-server`` starts the frame's server)."""
        if not frame.is_bd:
            return None
        return frame.spawn("bd", *self._init_args(frame))

    def converge(self) -> None:
        """Pull every live, unpartitioned frame to the remote head."""
        for frame in self.frames.values():
            if frame.alive and not frame.partitioned:
                frame.pull().check()

    def checkpoint(self) -> Checkpoint:
        """Record the remote ``refs/dolt/data`` and every frame's local ``main`` for
        :meth:`rewind`. Take it with every frame idle."""
        return Checkpoint(
            remote=self.remote.head(),
            frames={name: f.local_main() for name, f in self.frames.items()},
        )

    def rewind(self, checkpoint: Checkpoint) -> None:
        """Return the cluster to ``checkpoint`` without rebuilding it — the cheap reset a long
        randomized interleaving run needs (a fresh cluster costs ~10-20s). Heals every
        partition, disarms every hold, restarts killed frames, moves the remote ref back and
        hard-resets every frame's ``main``. HQ placement is the caller's to restore."""
        for frame in self.frames.values():
            frame.heal()
            self.hq.heal(frame.name)
            if not frame.alive:
                frame.restart()
            for sub in ("holds", "held", "arrived"):
                for path in (frame.ctl / sub).iterdir():
                    path.unlink(missing_ok=True)
        subprocess.run(
            ["git", "--git-dir", str(self.remote.path), "update-ref", "refs/dolt/data"]
            + [checkpoint.remote],
            check=True,
            capture_output=True,
            env=self.remote._env,
        )
        for name, frame in self.frames.items():
            target = checkpoint.frames[name]
            if frame.kind == BD_SERVER:
                frame.sql(f"call dolt_reset('--hard', '{target}')").check()
            else:
                frame.dolt("reset", "--hard", target).check()

    def close(self) -> None:
        for frame in self.frames.values():
            for sub in ("holds", "held"):
                directory = frame.ctl / sub
                if directory.is_dir():
                    for path in directory.iterdir():
                        path.unlink(missing_ok=True)
            frame.heal()
        for frame in self.frames.values():
            with contextlib.suppress(Exception):
                if frame._pending is not None:
                    frame._pending.result(30)
            frame.stop()


@dataclass(frozen=True)
class Checkpoint:
    """A rewind target: remote ``refs/dolt/data`` commit + each frame's local ``main`` hash."""

    remote: str | None
    frames: dict[str, str]


def _free_port() -> int:
    from harness.world import free_port

    return free_port()


# --------------------------------------------------------------------------------------------
# recorder


class Recorder:
    """Before/after snapshots of every step, appended to a JSONL trace.

    A snapshot is ``{seq, step, frame, phase, t, remote: {head, main, tables}, hq: {writer,
    epoch}, frames: {name: guard}}``. ``remote_tables`` are read from remote ``main`` through the
    observer clone (cached per remote head); ``guard_tables`` are read from each idle frame's
    local store (costly on bd frames — leave empty for large randomized runs). ``guard_probe``
    replaces the per-frame guard read entirely (bh-sieai's write-guard state).
    """

    def __init__(
        self,
        cluster: Cluster,
        trace: Path | None = None,
        *,
        hive: str = "hive",
        remote_tables: Sequence[str] = ("bh_writer",),
        guard_tables: Sequence[str] = (),
        guard_probe: Callable[[Frame], dict] | None = None,
    ):
        self.cluster = cluster
        self.trace = trace or cluster.root / "trace.jsonl"
        self.hive = hive
        self.remote_tables = tuple(remote_tables)
        self.guard_tables = tuple(guard_tables)
        self.guard_probe = guard_probe
        self.entries: list[dict] = []
        self._t0 = time.monotonic()

    def snapshot(self, step: str, *, frame: str | None = None, phase: str = "at") -> dict:
        placement = self.cluster.hq.placement(self.hive)
        entry = {
            "seq": len(self.entries),
            "step": step,
            "frame": frame,
            "phase": phase,
            "t": round(time.monotonic() - self._t0, 3),
            "remote": self.cluster.remote.view(self.remote_tables),
            "hq": {"writer": placement.writer, "epoch": placement.epoch},
            "frames": {
                name: (self.guard_probe(f) if self.guard_probe else f.guard(self.guard_tables))
                for name, f in self.cluster.frames.items()
            },
        }
        self.entries.append(entry)
        with self.trace.open("a") as fh:
            fh.write(json.dumps(entry, sort_keys=True, default=str) + "\n")
        return entry

    @contextlib.contextmanager
    def step(self, step: str, frame: str | None = None) -> Iterator[dict]:
        """Snapshot before, run the body, snapshot after; ``after["seconds"]`` is the body's."""
        before = self.snapshot(step, frame=frame, phase="before")
        started = time.monotonic()
        info: dict = {"before": before}
        yield info
        after = self.snapshot(step, frame=frame, phase="after")
        after["seconds"] = round(time.monotonic() - started, 3)
        info["after"] = after
