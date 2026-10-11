"""Cluster-rewind benchmark for spike bh-jk4mn. Opt-in evidence script, never collected.

Builds ONE composed writer-fencing world (``harness.composed_fence.composed_world``: SqlHQ +
bd-embedded ``a`` + bd-server ``b`` + Dolt CLI ``c``, fence and guard installed), takes a
bootstrap checkpoint, then repeatedly dirties it the way a fencing test does (:func:`dirty`) and
measures each rewind against a full-state fingerprint of the bootstrap (:func:`fingerprint`):

* ``naive`` -- what ``rewind_world`` does today (remote ``refs/dolt/data``, hard reset of each
  frame's ``main``, HQ placement restore, remote-branch prune). Its leftover diff IS the
  non-versioned-state inventory, measured rather than guessed.
* Mechanism A, ``DOLT_RESET --hard fx-boot`` plus one explicit step per non-versioned item, in
  two implementations: :func:`full_rewind` (one ``bd sql`` / ``dolt`` process per statement,
  serial; run once as the naive round's cleanup) and :func:`fast_rewind` (``tag-fast`` rounds:
  one direct SQL connection per server, one read + one script per CLI/embedded store, all
  frames and HQ in parallel).
* Mechanism B probe (:func:`branch_probe`): a per-test branch via ``DOLT_CHECKOUT`` and the
  per-database default-branch global; records where bd's writes land and whether the write
  guard fires off ``main``.
* Mechanism C (``template``): stop every process, ``cp -a --reflink=auto`` a stopped-cluster
  template over the root (same absolute path), restart.

Run (one world at a time; wait for no full gate first)::

    uv run python docs/spikes/artifacts/bh-jk4mn/cluster_rewind_bench.py --root DIR \
        [--rounds 3] [--copy-probe /data/...]

Prints one JSON document (timings in seconds, fingerprint diffs) on stdout.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve()
ROOT = HERE.parents[4]
sys.path[:0] = [str(ROOT / "tests"), str(ROOT / "src")]

from harness import composed_fence as cf  # noqa: E402
from harness import write_guard as wg  # noqa: E402
from harness.writer_fencing import BD_EMBEDDED, BD_SERVER, CAS, DOLT_CLI, Frame  # noqa: E402

TAG = "fx-boot"
PIN = "refs/fixture/boot/"
FRAMES = [("a", BD_EMBEDDED), ("b", BD_SERVER), ("c", DOLT_CLI)]
_BEADS_SKIP = {"embeddeddolt", "dolt", "server", "logs"}


def _git(*args: str, cwd: Path | None = None, check: bool = True) -> str:
    res = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and res.returncode:
        raise AssertionError(f"git {args}: {res.stderr}")
    return res.stdout


def _refs(git_dir: Path) -> dict[str, str]:
    out = _git("--git-dir", str(git_dir), "for-each-ref", "--format=%(objectname) %(refname)")
    return {r: s for s, r in (line.split() for line in out.splitlines()) if not r.startswith(PIN)}


def _lit(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, dict | list):
        value = json.dumps(value)
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


@contextlib.contextmanager
def timed(into: dict, key: str):
    t0 = time.perf_counter()
    yield
    into[key] = round(time.perf_counter() - t0, 3)


# ---------------------------------------------------------------------------------------------
# fingerprint: every piece of state a test can leave behind, versioned or not


def local_only_tables(frame: Frame) -> list[str]:
    """Tables present in the working set but not in HEAD: ``dolt_ignore``'d or untracked."""
    now = {next(iter(r.values())) for r in frame.query("show tables")}
    head = {next(iter(r.values())) for r in frame.query("show tables as of 'HEAD'")}
    return sorted(now - head)


def frame_print(frame: Frame) -> dict:
    out: dict = {
        "alive": frame.alive,
        "partitioned": frame.partitioned,
        "holds": frame.holds(),
    }
    if not frame.alive:
        return out
    q = frame.query
    out["main"] = q("select hashof('main') h")[0]["h"]
    out["branches"] = sorted(
        (r["name"], r["hash"]) for r in q("select name, hash from dolt_branches")
    )
    out["tags"] = sorted(r["tag_name"] for r in q("select tag_name from dolt_tags"))
    out["remote_branches"] = sorted(
        (r["name"], r["hash"]) for r in q("select name, hash from dolt_remote_branches")
    )
    out["status"] = sorted(
        (r["table_name"], str(r["staged"]), r["status"]) for r in q("select * from dolt_status")
    )
    local = {}
    for table in local_only_tables(frame):
        rows = q(f"select * from `{table}`")
        local[table] = sorted(json.dumps(r, sort_keys=True, default=str) for r in rows)
    out["local_tables"] = local
    out["writer"] = q("select frame, epoch from bh_writer")
    if frame.kind == BD_SERVER:
        out["users"] = sorted(
            f"{r['user']}@{r['host']}" for r in q("select user, host from mysql.user")
        )
        out["globals"] = q("select @@global.max_connections mc, @@global.net_read_timeout nrt")
        persisted: set[str] = set()
        for cfg in frame.server_dir.rglob(".dolt/config.json"):
            persisted |= set(json.loads(cfg.read_text() or "{}"))
        out["persisted"] = sorted(persisted - {"sqlserver.global.server_uuid"})
    if frame.is_bd:
        out["git_refs"] = _refs(frame.hive / ".git")
        out["beads_files"] = beads_files(frame)
    return out


def hq_print(hq: cf.SqlHQ) -> dict:
    root = ("root", "")
    _, place = hq._exec(root, "select hive, frame, epoch from bh_placement")
    sessions = {}
    for f in hq.frames:
        _, s = hq._exec(root, f"select renewed_at from frame_{f}_session")
        _, e = hq._exec(root, f"select status, measured_at from frame_{f}_evidence")
        sessions[f] = [str(s[0][0]), str(e[0][0]), str(e[0][1])]
    _, users = hq._exec(root, "select user, host from mysql.user")
    _, status = hq._exec(root, "select table_name, staged, status from dolt_status")
    _, head = hq._exec(root, "select hashof('main')")
    return {
        "placement": [list(r) for r in place],
        "liveness": sessions,
        "users": sorted(f"{u}@{h}" for u, h in users),
        "status": sorted(str(list(r)) for r in status),
        "head": head[0][0],
        "cut": sorted(hq._cut),
    }


def fingerprint(world: cf.World) -> dict:
    cl = world.cluster
    return {
        "remote_refs": _refs(cl.remote.path),
        "hq": hq_print(world.hq),
        "frames": {n: frame_print(f) for n, f in cl.frames.items()},
    }


def diff(base: dict, now: dict, path: str = "") -> list[str]:
    if isinstance(base, dict) and isinstance(now, dict):
        out = []
        for k in sorted(set(base) | set(now)):
            out += diff(base.get(k), now.get(k), f"{path}/{k}")
        return out
    if base == now:
        return []
    if isinstance(base, list) and isinstance(now, list):
        b = {json.dumps(x, default=str) for x in base}
        n = {json.dumps(x, default=str) for x in now}
        return [f"{path}: -{sorted(b - n)} +{sorted(n - b)}"[:400]]
    return [
        f"{path}: {json.dumps(base, default=str)[:160]} -> {json.dumps(now, default=str)[:160]}"
    ]


# ---------------------------------------------------------------------------------------------
# bootstrap checkpoint


def checkpoint(world: cf.World) -> dict:
    cl, hq = world.cluster, world.hq
    for frame in cl.frames.values():
        frame.sql(f"call dolt_tag('{TAG}')").check()
    hq._exec(("root", ""), "call dolt_add('-A')")
    hq._exec(("root", ""), "call dolt_commit('--allow-empty', '-m', 'fixture: bootstrap')")
    hq._exec(("root", ""), f"call dolt_tag('{TAG}')")
    refs = _refs(cl.remote.path)
    stdin = "".join(f"update {PIN}{i} {sha}\n" for i, sha in enumerate(sorted(set(refs.values()))))
    subprocess.run(
        ["git", "--git-dir", str(cl.remote.path), "update-ref", "--stdin"],
        input=stdin, text=True, check=True,
    )  # fmt: skip
    boot = fingerprint(world)
    local, tables = {}, {}
    for name, frame in cl.frames.items():
        if frame.is_bd:
            for rel in boot["frames"][name]["beads_files"]:
                dst = frame.dir / "boot-beads" / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(frame.hive / ".beads" / rel, dst)
        local[name] = {t: frame.query(f"select * from `{t}`") for t in local_only_tables(frame)}
        tables[name] = sorted(next(iter(r.values())) for r in frame.query("show tables"))
    return {"fp": boot, "local_rows": local, "tables": tables, "placement": hq.placement()}


# ---------------------------------------------------------------------------------------------
# a representative fencing test's footprint


def dirty(world: cf.World, n: int, *, kill: bool) -> dict:
    cl, hq = world.cluster, world.hq
    a, b, c = cl["a"], cl["b"], cl["c"]
    t: dict = {}
    with timed(t, "dirty"):
        cf.write(a, f"r{n}-a").check()
        a.push().check()
        epoch = hq.place("b").epoch
        res = cf.adopt(cl, b, epoch)
        assert res.landed, res.outcome
        cf.write(b, f"r{n}-b").check()
        b.push().check()
        c.sql(f"call dolt_branch('scratch-{n}')").check()
        c.sql(f"call dolt_tag('t-{n}')").check()
        c.sql("create table scratch_untracked (id int primary key)").check()
        wg.provision(c, role="branch")
        b.sql(f"create user 'u{n}'@'%' identified by 'x'").check()
        b.sql("set persist max_connections = 77").check()
        b.sql("set global net_read_timeout = 77").check()
        remote = str(cl.remote.path)
        head = cl.remote.head()
        _git("--git-dir", remote, "update-ref", "refs/bh/epoch", head)
        _git("--git-dir", remote, "update-ref", f"refs/bh/backup/fx-{n}/c", head)
        own = _git("-C", str(a.hive), "rev-parse", "HEAD").strip()
        _git("-C", str(a.hive), "update-ref", "refs/bh/lease/fx", own)
        hq.renew("a")
        hq.publish_evidence("b")
        c.partition()
        hq.partition("c")
        a.hold(CAS)
        if kill:
            b.kill()
    return t


# ---------------------------------------------------------------------------------------------
# rewinds


def naive_rewind(world: cf.World, cp: dict) -> None:
    """Today's ``rewind_world`` behaviour, reproduced: versioned state + placement only."""
    cl = world.cluster
    boot = cp["fp"]
    for frame in cl.frames.values():
        frame.heal()
        cl.hq.heal(frame.name)
        if not frame.alive:
            frame.restart()
        for sub in ("holds", "held", "arrived"):
            for p in (frame.ctl / sub).iterdir():
                p.unlink()
    _git("--git-dir", str(cl.remote.path), "update-ref", "refs/dolt/data",
         boot["remote_refs"]["refs/dolt/data"])  # fmt: skip
    for name, frame in cl.frames.items():
        target = boot["frames"][name]["main"]
        if frame.kind == BD_SERVER:
            frame.sql(f"call dolt_reset('--hard', '{target}')").check()
        else:
            frame.dolt("reset", "--hard", target).check()
    world.hq.restore(cp["placement"])
    for frame in cl.frames.values():
        cf.prune_remote_branches(frame)


def full_rewind(world: cf.World, cp: dict) -> dict:
    """Mechanism A, complete: one step per state class, in dependency order."""
    cl, hq = world.cluster, world.hq
    boot = cp["fp"]
    t: dict = {}
    with timed(t, "faults_and_processes"):
        for frame in cl.frames.values():
            frame.heal()
            hq.heal(frame.name)
            if not frame.alive:
                frame.restart()
            for sub in ("holds", "held", "arrived"):
                for p in (frame.ctl / sub).iterdir():
                    p.unlink()
            (frame.ctl / "git.jsonl").unlink(missing_ok=True)
        if not hq.up:
            hq.resume()
    with timed(t, "remote_refs"):
        now = _refs(cl.remote.path)
        want = boot["remote_refs"]
        stdin = "".join(f"delete {r}\n" for r in now if r not in want)
        stdin += "".join(f"update {r} {s}\n" for r, s in want.items() if now.get(r) != s)
        if stdin:
            subprocess.run(
                ["git", "--git-dir", str(cl.remote.path), "update-ref", "--stdin"],
                input=stdin, text=True, check=True,
            )  # fmt: skip
        cl.remote._cache = None
    with timed(t, "hq"):
        _rewind_hq(hq, cp)
    for name, frame in cl.frames.items():
        fb = boot["frames"][name]
        with timed(t, f"frame_{name}"):
            if frame.kind != BD_SERVER:
                frame.dolt("merge", "--abort")
            frame.sql(f"call dolt_reset('--hard', '{TAG}')").check()
            cur = frame_print(frame)
            keep = {b for b, _ in fb["branches"]}
            for b, _ in cur["branches"]:
                if b not in keep:
                    frame.sql(f"call dolt_branch('-D', '{b}')").check()
            for tag in cur["tags"]:
                if tag not in fb["tags"]:
                    frame.sql(f"call dolt_tag('-d', '{tag}')").check()
            for table in cur["local_tables"]:
                if table not in fb["local_tables"]:
                    frame.sql(f"drop table `{table}`").check()
                elif cur["local_tables"][table] != fb["local_tables"][table]:
                    frame.sql(f"delete from `{table}`").check()
                    for row in cp["local_rows"][name][table]:
                        cols = ", ".join(f"`{k}`" for k in row)
                        vals = ", ".join(_lit(v) for v in row.values())
                        frame.sql(f"insert into `{table}` ({cols}) values ({vals})").check()
            if frame.kind == BD_SERVER:
                for user in cur["users"]:
                    if user not in fb["users"]:
                        u, h = user.rsplit("@", 1)
                        frame.sql(f"drop user '{u}'@'{h}'").check()
                # Dolt 2.3.5 has no RESET PERSIST (syntax error): drop the key from the
                # server's .dolt/config.json (read at start) and SET GLOBAL the live value.
                extra = set(cur["persisted"]) - set(fb["persisted"])
                for cfg in frame.server_dir.rglob(".dolt/config.json"):
                    data = json.loads(cfg.read_text() or "{}")
                    if extra & set(data):
                        cfg.write_text(
                            json.dumps({k: v for k, v in data.items() if k not in extra})
                        )
                g = fb["globals"][0]
                frame.sql(
                    f"set global max_connections = {g['mc']}, net_read_timeout = {g['nrt']}"
                ).check()
            _rewind_files(frame, cp)
            cf.prune_remote_branches(frame)
    world.moves = cf.MoveLog(cl)
    return t


def _restore_local_sql(cp: dict, name: str, current_tables: set[str]) -> list[str]:
    """Drop tables that did not exist at bootstrap; refill every local-only table from its
    bootstrap rows (unconditional: cheaper than reading them first)."""
    out = [f"drop table `{t}`" for t in sorted(current_tables - set(cp["tables"][name]))]
    for table, rows in cp["local_rows"][name].items():
        out.append(f"delete from `{table}`")
        for row in rows:
            cols = ", ".join(f"`{k}`" for k in row)
            out.append(
                f"insert into `{table}` ({cols}) values ({', '.join(map(_lit, row.values()))})"
            )
    return out


_READ = (
    "select 'branch' k, name v from dolt_branches union all "
    "select 'tag', tag_name from dolt_tags union all "
    "select 'remote', name from dolt_remote_branches union all "
    "select 'table', table_name from information_schema.tables "
    "where table_schema = database() and table_type = 'BASE TABLE'"
)


def _plan(cp: dict, name: str, rows) -> list[str]:
    """The write half of a frame rewind, from one read of its refs and tables."""
    fb = cp["fp"]["frames"][name]
    have: dict[str, set[str]] = {"branch": set(), "tag": set(), "remote": set(), "table": set()}
    for k, v in rows:
        have[k].add(v)
    keep_branches = {b for b, _ in fb["branches"]}
    stmts = [f"call dolt_reset('--hard', '{TAG}')"]
    stmts += [f"call dolt_branch('-D', '{b}')" for b in sorted(have["branch"] - keep_branches)]
    stmts += [f"call dolt_tag('-d', '{t}')" for t in sorted(have["tag"] - set(fb["tags"]))]
    stmts += _restore_local_sql(cp, name, have["table"])
    stmts += [
        f"call dolt_branch('-d', '-r', '-f', '{r.removeprefix('remotes/')}')"
        for r in sorted(have["remote"])
    ]
    stmts.append("call dolt_fetch('origin')")
    return stmts


def _rewind_cli_frame(frame: Frame, cp: dict) -> None:
    rows = [(r["k"], r["v"]) for r in frame.query(_READ)]
    script = frame.dir / "rewind.sql"
    script.write_text("".join(f"{s};\n" for s in _plan(cp, frame.name, rows)))
    frame.dolt("sql", "--file", str(script)).check()


def _server_conn(frame: Frame, db: str | None):
    import pymysql

    return pymysql.connect(
        host="127.0.0.1", port=frame.port, user="root", password="", database=db, autocommit=True
    )


def _rewind_server_frame(frame: Frame, cp: dict) -> None:
    fb = cp["fp"]["frames"][frame.name]
    conn = _server_conn(frame, frame.cluster.prefix)
    try:
        with conn.cursor() as c:
            c.execute("select id from information_schema.processlist where id <> connection_id()")
            for (pid,) in c.fetchall():
                with contextlib.suppress(Exception):
                    c.execute(f"kill {int(pid)}")
            c.execute(_READ)
            for stmt in _plan(cp, frame.name, c.fetchall()):
                c.execute(stmt)
            c.execute("select user, host from mysql.user")
            for u, h in c.fetchall():
                if f"{u}@{h}" not in fb["users"]:
                    c.execute(f"drop user '{u}'@'{h}'")
            g = fb["globals"][0]
            c.execute(f"set global max_connections = {g['mc']}")
            c.execute(f"set global net_read_timeout = {g['nrt']}")
    finally:
        conn.close()
    for cfg in frame.server_dir.rglob(".dolt/config.json"):
        data = json.loads(cfg.read_text() or "{}")
        keep = {k: v for k, v in data.items() if k in fb["persisted"] or k.endswith("server_uuid")}
        if keep != data:
            cfg.write_text(json.dumps(keep))


def _rewind_files(frame: Frame, cp: dict) -> None:
    fb = cp["fp"]["frames"][frame.name]
    if not frame.is_bd:
        return
    for ref in _refs(frame.hive / ".git"):
        if ref.startswith("refs/bh/") and ref not in fb["git_refs"]:
            _git("-C", str(frame.hive), "update-ref", "-d", ref)
    beads, saved = frame.hive / ".beads", frame.dir / "boot-beads"
    now = beads_files(frame)
    for rel in set(now) - set(fb["beads_files"]):
        (beads / rel).unlink()
    for rel, digest in fb["beads_files"].items():
        if now.get(rel) != digest:
            (beads / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(saved / rel, beads / rel)


def beads_files(frame: Frame) -> dict[str, str]:
    """bd's plain files under ``.beads`` (not its Dolt stores, logs or locks): path -> sha1."""
    beads = frame.hive / ".beads"
    return {
        str(p.relative_to(beads)): hashlib.sha1(p.read_bytes()).hexdigest()
        for p in sorted(beads.rglob("*"))
        if p.is_file() and not (set(p.relative_to(beads).parts) & _BEADS_SKIP)
        and not p.name.endswith((".log", ".lock", ".pid", ".port"))
    }  # fmt: skip


def _rewind_hq(hq: cf.SqlHQ, cp: dict) -> None:
    boot = cp["fp"]["hq"]
    conn = hq.connect(("root", ""))
    try:
        with conn.cursor() as c:
            c.execute("select id from information_schema.processlist where id <> connection_id()")
            for (pid,) in c.fetchall():
                with contextlib.suppress(Exception):
                    c.execute(f"kill {int(pid)}")
            # Server-stamped liveness rows are dolt_ignore'd (reset leaves them) and their
            # BEFORE UPDATE triggers overwrite any value DML supplies: lift the (versioned)
            # trigger, write the bootstrap value, and let the hard reset put the trigger back.
            for f, (renewed, status, measured) in boot["liveness"].items():
                c.execute(f"drop trigger frame_{f}_session_upd")
                c.execute(f"update frame_{f}_session set renewed_at = '{renewed}'")
                c.execute(f"drop trigger frame_{f}_evidence_upd")
                c.execute(
                    f"update frame_{f}_evidence set status = '{status}', release_digest = 'none',"
                    f" profile = 'none', measured_at = '{measured}'"
                )
            c.execute(f"call dolt_reset('--hard', '{TAG}')")
            c.execute("select user, host from mysql.user")
            for u, h in c.fetchall():
                if f"{u}@{h}" not in boot["users"]:
                    c.execute(f"drop user '{u}'@'{h}'")
    finally:
        conn.close()
    hq._last = cp["placement"]


def fast_rewind(world: cf.World, cp: dict) -> dict:
    """Mechanism A done fast: one SQL connection (server) or one read + one script (CLI and
    embedded) per frame, every frame in parallel on its own agent."""
    from concurrent.futures import ThreadPoolExecutor

    cl, hq = world.cluster, world.hq
    boot = cp["fp"]
    t: dict = {}
    with timed(t, "faults_and_processes"):
        for frame in cl.frames.values():
            frame.heal()
            hq.heal(frame.name)
            for sub in ("holds", "held", "arrived"):
                for p in (frame.ctl / sub).iterdir():
                    p.unlink()
            (frame.ctl / "git.jsonl").unlink(missing_ok=True)
        dead = [f for f in cl.frames.values() if not f.alive]
        with ThreadPoolExecutor(len(dead) or 1) as pool:
            list(pool.map(lambda f: f.restart(), dead))
        if not hq.up:
            hq.resume()
    with timed(t, "remote_refs"):
        now = _refs(cl.remote.path)
        want = boot["remote_refs"]
        stdin = "".join(f"delete {r}\n" for r in now if r not in want)
        stdin += "".join(f"update {r} {s}\n" for r, s in want.items() if now.get(r) != s)
        if stdin:
            subprocess.run(
                ["git", "--git-dir", str(cl.remote.path), "update-ref", "--stdin"],
                input=stdin, text=True, check=True,
            )  # fmt: skip
        cl.remote._cache = None

    def one(frame: Frame) -> tuple[str, float]:
        t0 = time.perf_counter()
        if frame.kind == BD_SERVER:
            _rewind_server_frame(frame, cp)
        else:
            _rewind_cli_frame(frame, cp)
        _rewind_files(frame, cp)
        return frame.name, round(time.perf_counter() - t0, 3)

    with timed(t, "hq_and_frames_parallel"):
        with ThreadPoolExecutor(len(cl.frames) + 1) as pool:
            hq_job = pool.submit(lambda: _rewind_hq(hq, cp))
            for name, secs in pool.map(one, cl.frames.values()):
                t[f"frame_{name}"] = secs
            hq_job.result()
    world.moves = cf.MoveLog(cl)
    return t


def _json_rows_obs(cl, q: str) -> list[dict]:
    res = cl.remote._dolt("sql", "-r", "json", "-q", q)
    return json.loads(res.stdout or "{}").get("rows", [])


def branch_probe(world: cf.World) -> dict:
    """Mechanism B: a per-test branch. Where do bd writes land, and does the guard fire?"""
    cl = world.cluster
    a, b = cl["a"], cl["b"]
    out: dict = {}
    marks = lambda f: f.query("select count(*) n from bh_write_mark")[0]["n"]  # noqa: E731
    with timed(out, "create_branches"):
        for frame in cl.frames.values():
            frame.sql(f"call dolt_branch('per-test', '{TAG}')").check()
    # embedded/CLI: the repo's checked-out branch (repo_state) is what a new process opens
    with timed(out, "checkout_embedded"):
        a.dolt("checkout", "per-test").check()
    m0 = marks(a)
    res = a.bd("create", "--title", "on-branch-a", "-t", "task", "-p", "2", "--json")
    out["embedded_bd_create_ok"] = res.ok
    out["embedded_cli_active_branch"] = a.query("select active_branch() b")[0]["b"]
    out["embedded_write_on_main_working"] = bool(
        a.query("select count(*) n from `fx/main`.issues where title = 'on-branch-a'")[0]["n"]
    )
    out["embedded_write_on_per_test"] = bool(
        a.query("select count(*) n from issues where title = 'on-branch-a'")[0]["n"]
    )
    out["embedded_guard_marks_on_per_test"] = marks(a) - m0
    co = a.dolt("checkout", "main")
    out["embedded_checkout_back"] = co.ok or co.output.strip().splitlines()[0][:160]
    if not co.ok:
        a.dolt("checkout", "-f", "main").check()
    # server: DOLT_CHECKOUT is per session; bd opens a new connection per command
    with timed(out, "checkout_server_session"):
        b.sql("call dolt_checkout('per-test')").check()
    out["server_active_branch_next_command"] = b.query("select active_branch() b")[0]["b"]
    with timed(out, "server_default_branch_global"):
        b.sql("set global fx_default_branch = 'per-test'").check()
    out["server_active_branch_after_global"] = b.query("select active_branch() b")[0]["b"]
    m0 = marks(b)
    remote_main = cl.remote.view(())["main"]
    res = b.bd("create", "--title", "on-branch-b", "-t", "task", "-p", "2", "--json")
    out["server_bd_create_ok"] = res.ok
    out["server_write_on_per_test"] = bool(
        b.query("select count(*) n from issues as of 'per-test' where title = 'on-branch-b'")[0][
            "n"
        ]
    )
    out["server_write_on_main"] = bool(
        b.query("select count(*) n from issues as of 'main' where title = 'on-branch-b'")[0]["n"]
    )
    out["server_guard_marks_on_per_test"] = marks(b) - m0
    per_test = b.query("select hashof('per-test') h")[0]["h"]
    pushed = b.push()
    out["server_push_ok"] = pushed.ok
    after = cl.remote.view(())["main"]
    out["server_push_moved_remote_main"] = after != remote_main
    out["remote_branches_after_push"] = [
        r["name"] for r in _json_rows_obs(cl, "select name from dolt_remote_branches")
    ]
    out["per_test_hash"] = per_test
    out["remote_main_before_after"] = [remote_main, after]
    b.sql("set global fx_default_branch = ''").check()
    for frame in cl.frames.values():
        if frame is a:
            frame.dolt("branch", "-D", "per-test").check()
        else:
            frame.sql("call dolt_branch('-D', 'per-test')").check()
    return out


# ---------------------------------------------------------------------------------------------
# Mechanism C: stopped-cluster template copy


def stop_world(world: cf.World) -> None:
    for frame in world.cluster.frames.values():
        frame.stop()
    world.hq.down()


def start_world(world: cf.World) -> None:
    cl = world.cluster
    for frame in cl.frames.values():
        for sub in ("holds", "held", "arrived"):
            for p in (frame.ctl / sub).iterdir():
                p.unlink()
        frame._start_agent()
        if frame.kind == BD_SERVER:
            frame.bd("dolt", "start").check()
    cl.hq._cut.clear()
    world.hq.start()
    cl.remote._cache = None
    world.moves = cf.MoveLog(cl)


def cp_tree(src: Path, dst: Path, reflink: str = "auto") -> subprocess.CompletedProcess:
    return subprocess.run(
        ["cp", "-a", f"--reflink={reflink}", str(src), str(dst)], capture_output=True, text=True
    )


def du(path: Path) -> str:
    return subprocess.run(["du", "-sh", str(path)], capture_output=True, text=True).stdout.split()[
        0
    ]


# ---------------------------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--copy-probe", type=Path, help="a dir on another filesystem to time cp on")
    args = ap.parse_args()
    root = args.root
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    out: dict = {"rounds": []}
    t_build = time.perf_counter()
    with cf.composed_world(root / "w", FRAMES) as world:
        out["build_seconds"] = round(time.perf_counter() - t_build, 3)
        out["build_timings"] = world.cluster.timings
        with timed(out, "checkpoint_seconds"):
            cp = checkpoint(world)
        out["boot_fingerprint"] = cp["fp"]

        # Mechanism C capture first, so the template IS the bootstrap state
        tpl = root / "template"
        c: dict = {}
        with timed(c, "template_stop"):
            stop_world(world)
        with timed(c, "template_capture_copy"):
            res = cp_tree(root / "w", tpl)
            assert res.returncode == 0, res.stderr
        c["tree_size"] = du(tpl)
        always = cp_tree(root / "w", root / "reflink-always", "always")
        c["reflink_always"] = {"rc": always.returncode, "stderr": always.stderr.strip()[:200]}
        shutil.rmtree(root / "reflink-always", ignore_errors=True)
        with timed(c, "template_start"):
            start_world(world)
        c["leftovers_after_capture"] = diff(cp["fp"], fingerprint(world))

        # naive rewind once: the leftovers are the inventory
        r: dict = {"kind": "naive"}
        r.update(dirty(world, 0, kill=False))
        with timed(r, "rewind"):
            naive_rewind(world, cp)
        r["leftovers"] = diff(cp["fp"], fingerprint(world))
        print(json.dumps(r, indent=1), file=sys.stderr, flush=True)
        with timed(r, "cleanup_per_command"):
            r["cleanup_steps"] = full_rewind(world, cp)
        r["leftovers_after_cleanup"] = diff(cp["fp"], fingerprint(world))
        out["rounds"].append(r)

        for n in range(1, args.rounds + 1):
            r = {"kind": "tag-fast", "kill": n % 2 == 0}
            r.update(dirty(world, n, kill=r["kill"]))
            with timed(r, "rewind"):
                r["steps"] = fast_rewind(world, cp)
            r["leftovers"] = diff(cp["fp"], fingerprint(world))
            print(json.dumps(r, indent=1), file=sys.stderr, flush=True)
            out["rounds"].append(r)

        out["branch_probe"] = branch_probe(world)
        print(json.dumps(out["branch_probe"], indent=1), file=sys.stderr, flush=True)
        with timed(out["branch_probe"], "rewind_after"):
            fast_rewind(world, cp)
        out["branch_probe"]["leftovers_after_rewind"] = diff(cp["fp"], fingerprint(world))

        c["rounds"] = []
        for n in range(args.rounds + 1, 2 * args.rounds + 1):
            r = {"kill": n % 2 == 0}
            r.update(dirty(world, n, kill=r["kill"]))
            with timed(r, "rewind"):
                with timed(r, "stop"):
                    stop_world(world)
                with timed(r, "rm"):
                    shutil.rmtree(root / "w")
                with timed(r, "copy"):
                    res = cp_tree(tpl, root / "w")
                    assert res.returncode == 0, res.stderr
                with timed(r, "start"):
                    start_world(world)
            r["leftovers"] = diff(cp["fp"], fingerprint(world))
            print(json.dumps(r, indent=1), file=sys.stderr, flush=True)
            c["rounds"].append(r)
        if args.copy_probe:
            probe = args.copy_probe / "tpl-copy"
            shutil.rmtree(probe, ignore_errors=True)
            args.copy_probe.mkdir(parents=True, exist_ok=True)
            seed = args.copy_probe / "tpl-src"
            shutil.rmtree(seed, ignore_errors=True)
            cp_tree(tpl, seed)
            subprocess.run(["sync"])
            t0 = time.perf_counter()
            cp_tree(seed, probe)
            c["other_fs_copy_seconds"] = round(time.perf_counter() - t0, 3)
            c["other_fs"] = subprocess.run(
                ["findmnt", "-n", "-o", "FSTYPE", "-T", str(args.copy_probe)],
                capture_output=True, text=True,
            ).stdout.strip()  # fmt: skip
            shutil.rmtree(probe, ignore_errors=True)
            shutil.rmtree(seed, ignore_errors=True)
        out["template"] = c
        shutil.rmtree(tpl, ignore_errors=True)
    print(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    main()
