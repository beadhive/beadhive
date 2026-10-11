"""Rewindable writer-fencing clusters for tests (bh-i970d, on spike bh-jk4mn's GO verdict).

A fresh composed world costs ~22 s to build (bh-jk4mn Evidence 9); a stopped-cluster template
copy brings it back in ~1.3 s with an empty full-state fingerprint diff (Evidence 14). This
module turns that into a test resource:

* :class:`ClusterPool` builds ONE cluster or composed world per composition (the frame list
  plus build options) per process -- so one per xdist worker -- snapshots it right after the
  build, and restores it from the template before every later use. Its pytest face is the
  ``rewound_cluster`` / ``rewound_world`` fixtures in ``tests/stateful_fixtures.py``.
* The reset protocol is the template's: stop every process (agents, bd's sql-server, HQ), copy
  the template back over the SAME absolute path, restart (rebinding a port another process took
  meanwhile), then clear the Python-side caches (:meth:`Cluster.reset_caches`, a fresh
  ``MoveLog``).
* The leak guard is the spike's fingerprint (:func:`fingerprint`): remote refs; HQ placement,
  liveness rows, users and status; and per frame: branches, tags, remote-tracking refs,
  ``dolt_status``, every local-only (``dolt_ignore``'d or untracked) table's rows, the fence
  writer row, a server frame's users, globals and ``SET PERSIST`` keys, a bd frame's Git refs and
  plain ``.beads`` files, fixture controls, and the processes running under the root. After a
  restore it must equal the bootstrap fingerprint; a diff raises :class:`RewindLeak` and the
  pool rebuilds that composition instead of handing the next test a dirty cluster. It is on in
  this module's own tests and opt-in elsewhere (``BH_CLUSTER_REWIND_GUARD=1``): the template
  copy is complete by construction, so the guard is a debug assertion, not a per-test cost.

Opt-outs: ``BH_CLUSTER_REWIND=0`` forces a fresh build per acquisition (debugging), and
``pytest.mark.cluster_destructive`` on a test makes the pool rebuild after it (for a test that
mutates something outside the template: the Python objects themselves, or a path outside root).

Measured here (Dolt 2.3.5, bd 1.3.0, tmpfs): see ``tests/test_cluster_rewind_int.py``.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import time
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from harness.writer_fencing import BD_SERVER, Cluster, Frame

__all__ = [
    "ClusterPool",
    "RewindLeak",
    "build",
    "cluster_key",
    "custom_key",
    "diff",
    "fingerprint",
    "guard_enabled",
    "rewind_enabled",
    "world_key",
]

#: ``.beads`` subtrees that are Dolt stores or server state, not bd's plain files.
_BEADS_SKIP = {"embeddeddolt", "dolt", "server", "logs"}


def rewind_enabled() -> bool:
    """``BH_CLUSTER_REWIND=0`` turns the pool into a per-acquisition fresh build."""
    return os.environ.get("BH_CLUSTER_REWIND", "1").strip().lower() not in {"0", "false", "no"}


def guard_enabled() -> bool:
    """``BH_CLUSTER_REWIND_GUARD=1`` fingerprints every restore outside the fixture's own
    tests (a debug assertion: it adds a few seconds per test)."""
    return os.environ.get("BH_CLUSTER_REWIND_GUARD", "").strip().lower() in {"1", "true", "yes"}


class RewindLeak(AssertionError):
    """A restore left state the bootstrap fingerprint does not have."""

    def __init__(self, key: object, leftovers: list[str]):
        self.key = key
        self.leftovers = leftovers
        shown = "\n  ".join(leftovers[:40])
        super().__init__(
            f"cluster rewind leaked {len(leftovers)} difference(s) for {key!r}; the pool "
            f"rebuilds this composition:\n  {shown}"
        )


# =============================================================================================
# fingerprint: every piece of state a test can leave behind, versioned or not (bh-jk4mn)
# =============================================================================================


def _git_refs(git_dir: Path) -> dict[str, str]:
    import subprocess

    res = subprocess.run(
        ["git", "--git-dir", str(git_dir), "for-each-ref", "--format=%(objectname) %(refname)"],
        capture_output=True,
        text=True,
    )
    if res.returncode:
        return {"<error>": res.stderr.strip()[:200]}
    return {ref: sha for sha, ref in (line.split() for line in res.stdout.splitlines())}


def _multi(frame: Frame, statements: Sequence[str]) -> list[list[dict]]:
    """Run ``statements`` in ONE engine round trip: one connection for a server frame, one
    ``dolt sql`` process (one JSON document per statement) for a CLI or embedded frame."""
    if frame.kind == BD_SERVER:
        import pymysql
        import pymysql.cursors

        conn = pymysql.connect(
            host="127.0.0.1",
            port=frame.port,
            user="root",
            password="",
            database=frame.cluster.prefix,
            autocommit=True,
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=5,
        )
        try:
            out = []
            with conn.cursor() as cursor:
                for statement in statements:
                    cursor.execute(statement)
                    out.append(list(cursor.fetchall()))
            return out
        finally:
            conn.close()
    res = frame.dolt("sql", "-r", "json", "-q", ";\n".join(statements))
    if res.returncode:
        raise AssertionError(f"{frame.name}: fingerprint read failed: {res.output}")
    decoder, text, pos, out = json.JSONDecoder(), res.stdout, 0, []
    while True:
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if pos >= len(text):
            break
        doc, pos = decoder.raw_decode(text, pos)
        out.append(doc.get("rows", []) if isinstance(doc, dict) else doc)
    if len(out) != len(statements):
        raise AssertionError(f"{frame.name}: {len(statements)} reads, {len(out)} results")
    return out


def _first(row: dict):
    return next(iter(row.values()))


def _stable(row: dict) -> str:
    return json.dumps({k: str(v) for k, v in row.items()}, sort_keys=True)


def beads_files(frame: Frame) -> dict[str, str]:
    """bd's plain files under ``.beads`` (not its Dolt stores, logs or locks): path -> sha1."""
    beads = frame.hive / ".beads"
    if not beads.is_dir():
        return {}
    return {
        str(p.relative_to(beads)): hashlib.sha1(p.read_bytes()).hexdigest()
        for p in sorted(beads.rglob("*"))
        if p.is_file()
        and not (set(p.relative_to(beads).parts) & _BEADS_SKIP)
        and not p.name.endswith((".log", ".lock", ".pid", ".port"))
    }


def frame_print(frame: Frame) -> dict:
    out: dict = {
        "alive": frame.alive,
        "partitioned": frame.partitioned,
        "holds": frame.holds(),
        "arrived": sorted(p.name for p in (frame.ctl / "arrived").iterdir()),
        "gate_log": (frame.ctl / "git.jsonl").exists(),
    }
    if frame.is_bd:
        out["git_refs"] = _git_refs(frame.hive / ".git")
        out["beads_files"] = beads_files(frame)
    if not frame.alive:
        return out
    reads = [
        "select hashof('main') h",
        "select name, hash from dolt_branches",
        "select tag_name from dolt_tags",
        "select name, hash from dolt_remote_branches",
        "select table_name, staged, status from dolt_status",
        "show tables",
        "show tables as of 'HEAD'",
    ]
    server = frame.kind == BD_SERVER
    if server:
        reads += [
            "select user, host from mysql.user",
            "select @@global.max_connections mc, @@global.net_read_timeout nrt, "
            "@@global.wait_timeout wt",
        ]
    rows = _multi(frame, reads)
    out["main"] = rows[0][0]["h"]
    out["branches"] = sorted(f"{r['name']}={r['hash']}" for r in rows[1])
    out["tags"] = sorted(r["tag_name"] for r in rows[2])
    out["remote_branches"] = sorted(f"{r['name']}={r['hash']}" for r in rows[3])
    out["status"] = sorted(f"{r['table_name']}:{r['staged']}:{r['status']}" for r in rows[4])
    tables = {_first(r) for r in rows[5]}
    local = sorted(tables - {_first(r) for r in rows[6]})
    if "bh_writer" in tables:
        local_reads = [*(f"select * from `{t}`" for t in local), "select * from bh_writer"]
    else:
        local_reads = [f"select * from `{t}`" for t in local]
    local_rows = _multi(frame, local_reads) if local_reads else []
    out["local_tables"] = {t: sorted(map(_stable, local_rows[i])) for i, t in enumerate(local)}
    if "bh_writer" in tables:
        out["writer"] = sorted(map(_stable, local_rows[-1]))
    if server:
        out["users"] = sorted(f"{r['user']}@{r['host']}" for r in rows[7])
        out["globals"] = {k: str(v) for k, v in rows[8][0].items()}
        persisted: set[str] = set()
        for cfg in frame.server_dir.rglob(".dolt/config.json"):
            with contextlib.suppress(ValueError):
                persisted |= set(json.loads(cfg.read_text() or "{}"))
        out["persisted"] = sorted(k for k in persisted if not k.endswith("server_uuid"))
    return out


def hq_print(hq) -> dict:
    """SqlHQ: placement, liveness rows, users and status (inventory rows 12, 16, 17). GitHQ:
    its lease refs. The fixture's own JSON HQ: its placement file. Plus the reachability cut."""
    out: dict = {"cut": sorted(getattr(hq, "_cut", ())), "up": getattr(hq, "up", True)}
    mode = getattr(hq, "mode", None)
    if mode == "dolt-server":
        conn = hq.connect(("root", ""))
        try:
            with conn.cursor() as c:
                c.execute("select hive, frame, epoch, revision from bh_placement")
                out["placement"] = sorted(str(list(r)) for r in c.fetchall())
                live = {}
                for f in hq.frames:
                    c.execute(f"select renewed_at from frame_{f}_session")
                    session = c.fetchall()
                    c.execute(f"select status, measured_at from frame_{f}_evidence")
                    evidence = c.fetchall()
                    live[f] = [str(list(r)) for r in (*session, *evidence)]
                out["liveness"] = live
                c.execute("select user, host from mysql.user")
                out["users"] = sorted(f"{u}@{h}" for u, h in c.fetchall())
                c.execute("select table_name, staged, status from dolt_status")
                out["status"] = sorted(str(list(r)) for r in c.fetchall())
                c.execute("select hashof('main')")
                out["head"] = c.fetchall()[0][0]
        finally:
            conn.close()
    elif mode == "git":
        out["refs"] = _git_refs(hq.repo) if hq.repo.exists() else {}
    else:
        state = getattr(hq, "_state", None)
        out["placement"] = state.read_text() if state is not None and state.exists() else None
    return out


def processes_print(root: Path, known: dict[int, str]) -> list[str]:
    """Every process whose working directory or command line is under ``root`` (Linux
    ``/proc``; empty elsewhere), named by role when ``known``. A stray -- a ``Forwarder`` race
    child, a server a test started by hand -- shows as ``stray:<argv0>``.

    ``dolt send-metrics`` is skipped: the Dolt CLI forks it detached after a command (cwd the
    command's, parent init) and it exits on its own a moment later, so whether a print catches
    one is timing, not state -- it made the guard flag a "leak" at random (bh-jvgj6)."""
    proc = Path("/proc")
    if not proc.is_dir():
        return []
    needle = str(root.resolve())
    found = []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
            cwd = os.readlink(entry / "cwd")
            state = (entry / "stat").read_text().rsplit(")", 1)[1].split()[0]
        except OSError:
            continue
        if state == "Z":
            continue
        if needle in cmdline or cwd == needle or cwd.startswith(needle + os.sep):
            argv = cmdline.split()
            argv0 = Path(argv[0]).name if argv else ""
            if argv0 == "dolt" and argv[1:2] == ["send-metrics"]:
                continue
            found.append(known.get(pid, f"stray:{argv0}"))
    return sorted(found)


def _known_pids(cluster: Cluster, hq) -> dict[int, str]:
    known: dict[int, str] = {}
    for frame in cluster.frames.values():
        if frame.kind == BD_SERVER and (pid := frame.server_pid()) is not None:
            known[pid] = f"server:{frame.name}"
    proc = getattr(hq, "proc", None)
    if proc is not None and proc.poll() is None:
        known[proc.pid] = "hq"
    return known


def fingerprint(target, *, processes_root: Path | None = None) -> dict:
    """The full-state fingerprint of a :class:`Cluster` or a composed ``World``. Frames are read
    in parallel (each on its own agent)."""
    cluster: Cluster = getattr(target, "cluster", target)
    hq = getattr(target, "hq", cluster.hq)
    root = processes_root or getattr(target, "root", None) or cluster.root
    with ThreadPoolExecutor(len(cluster.frames) + 1) as pool:
        hq_job = pool.submit(hq_print, hq)
        frames = dict(
            zip(cluster.frames, pool.map(frame_print, cluster.frames.values()), strict=True)
        )
        hq_fp = hq_job.result()
    return {
        "remote_refs": _git_refs(cluster.remote.path),
        "hq": hq_fp,
        "frames": frames,
        "processes": processes_print(root, _known_pids(cluster, hq)),
    }


def diff(base, now, path: str = "") -> list[str]:
    """``/path: before -> after`` lines; empty when the fingerprints match."""
    if isinstance(base, dict) and isinstance(now, dict):
        out: list[str] = []
        for key in sorted(set(base) | set(now)):
            out += diff(base.get(key), now.get(key), f"{path}/{key}")
        return out
    if base == now:
        return []
    if isinstance(base, list) and isinstance(now, list):
        # a multiset diff: two agents where the bootstrap had one is a leak a set diff hides
        b = Counter(json.dumps(x, default=str) for x in base)
        n = Counter(json.dumps(x, default=str) for x in now)
        gone, extra = sorted((b - n).elements()), sorted((n - b).elements())
        if not gone and not extra:  # same items, another order
            gone, extra = list(b.elements()), list(n.elements())
        return [f"{path}: -{gone} +{extra}"[:400]]
    before, after = json.dumps(base, default=str)[:160], json.dumps(now, default=str)[:160]
    return [f"{path}: {before} -> {after}"]


# =============================================================================================
# the pool
# =============================================================================================


@dataclass
class _Entry:
    target: object  # Cluster | World
    close: Callable[[], None]
    root: Path
    boot: dict | None = None
    dirty: bool = False
    parked: float = 0.0
    restores: list[dict[str, float]] = field(default_factory=list)


class ClusterPool:
    """One rewindable cluster (or composed world) per composition, built lazily.

    ``acquire(key)`` returns the target for ``key`` in its bootstrap state: built and snapshot
    the first time, restored from the template on every later call. ``release(key)`` marks it
    used and PARKS it -- every process stopped -- so no sql-server outlives the test (and the
    Dolt slot) that used it; the restore on the next acquisition copies and restarts.
    ``release(key, destructive=True)`` throws it away instead, so the next acquisition rebuilds.
    ``builder`` maps a key and a fresh directory to a context manager yielding the target (a
    :class:`Cluster` or a composed ``World``: anything with ``snapshot`` / ``restore`` /
    ``stop_processes``). Not thread-safe; one pool per process, so one per xdist worker.
    """

    def __init__(
        self,
        base: Path,
        builder: Callable[[object, Path], contextlib.AbstractContextManager],
        *,
        rewind: bool | None = None,
        guard: bool | None = None,
    ):
        self.base = base
        self.builder = builder
        self.rewind = rewind_enabled() if rewind is None else rewind
        self.guard = guard_enabled() if guard is None else guard
        self.builds = 0
        self.build_seconds: list[float] = []
        self._entries: dict[object, _Entry] = {}
        self._seq = 0

    # -- lifecycle --------------------------------------------------------------------------
    def _build(self, key: object, guard: bool) -> _Entry:
        self._seq += 1
        root = self.base / f"c{self._seq}"
        root.mkdir(parents=True)
        stack = contextlib.ExitStack()
        started = time.monotonic()
        try:
            target = stack.enter_context(self.builder(key, root))
            if self.rewind:
                target.snapshot()
        except BaseException:
            stack.close()
            shutil.rmtree(root, ignore_errors=True)
            raise
        self.builds += 1
        self.build_seconds.append(round(time.monotonic() - started, 3))
        entry = _Entry(target, stack.close, root)
        if self.rewind and guard:
            entry.boot = fingerprint(target)
        return entry

    def _drop(self, key: object) -> None:
        entry = self._entries.pop(key, None)
        if entry is None:
            return
        with contextlib.suppress(Exception):
            entry.close()
        template = getattr(entry.target, "_template", None)
        if template is not None:
            template.discard()
        shutil.rmtree(entry.root, ignore_errors=True)

    def acquire(self, key: object, *, guard: bool | None = None):
        """``key``'s target in its bootstrap state. ``guard`` (default: the pool's) runs the
        leak guard after a restore; see :meth:`check`."""
        guard = self.guard if guard is None else guard
        entry = self._entries.get(key)
        if entry is None or not self.rewind:
            self._drop(key)
            entry = self._entries[key] = self._build(key, guard)
            return entry.target
        if entry.dirty:
            timings = entry.target.restore()
            if entry.parked:
                timings["parked_stop"] = entry.parked
                timings["total"] = round(timings["total"] + entry.parked, 3)
            entry.restores.append(timings)
            entry.dirty, entry.parked = False, 0.0
            if guard:
                self.check(key)
        return entry.target

    def check(self, key: object) -> None:
        """The leak guard: fingerprint ``key``'s target against its bootstrap fingerprint. On a
        diff, drop the composition (the next acquisition rebuilds) and raise
        :class:`RewindLeak`. A composition built without the guard has no bootstrap print;
        its first check records one from the freshly restored state instead."""
        entry = self._entries[key]
        now = fingerprint(entry.target)
        if entry.boot is None:
            entry.boot = now
            return
        leftovers = diff(entry.boot, now)
        if leftovers:
            self._drop(key)
            raise RewindLeak(key, leftovers)

    def release(self, key: object, *, destructive: bool = False) -> None:
        entry = self._entries.get(key)
        if entry is None:
            return
        if destructive or not self.rewind:
            self._drop(key)
            return
        started = time.monotonic()
        entry.target.stop_processes()
        entry.parked = round(time.monotonic() - started, 3)
        entry.dirty = True

    def restores(self, key: object) -> list[dict[str, float]]:
        entry = self._entries.get(key)
        return list(entry.restores) if entry is not None else []

    def close(self) -> None:
        for key in list(self._entries):
            self._drop(key)

    @contextlib.contextmanager
    def use(self, key: object, *, destructive: bool = False, guard: bool | None = None):
        target = self.acquire(key, guard=guard)
        try:
            yield target
        finally:
            self.release(key, destructive=destructive)


# =============================================================================================
# builders
# =============================================================================================


def cluster_key(frames: Sequence[tuple[str, str]], **options) -> tuple:
    return ("cluster", tuple(map(tuple, frames)), tuple(sorted(options.items())))


def world_key(frames: Sequence[tuple[str, str]], **options) -> tuple:
    return ("world", tuple(map(tuple, frames)), tuple(sorted(options.items())))


def custom_key(factory: Callable, frames: Sequence[tuple[str, str]], **options) -> tuple:
    """A composition a suite builds itself: ``factory(root, frames, **options)`` is a context
    manager yielding a target with ``snapshot`` / ``restore`` / ``stop_processes`` (bh-jvgj6:
    the composed suite's cut-over product world). ``factory`` is part of the key, so it must be
    one module-level callable, not a fresh closure per test."""
    return ("custom", factory, tuple(map(tuple, frames)), tuple(sorted(options.items())))


@contextlib.contextmanager
def build(key: tuple, root: Path) -> Iterator[object]:
    """The default builder for :func:`cluster_key` / :func:`world_key` / :func:`custom_key`
    keys."""
    if key[0] == "custom":
        _, factory, frames, options = key
        with factory(root, list(frames), **dict(options)) as target:
            yield target
        return
    kind, frames, options = key
    if kind == "cluster":
        with Cluster(root / "cluster", list(frames), **dict(options)) as cluster:
            yield cluster
    elif kind == "world":
        from harness.composed_fence import composed_world

        with composed_world(root, list(frames), **dict(options)) as world:
            yield world
    else:
        raise ValueError(f"unknown pool key kind {kind!r}")
