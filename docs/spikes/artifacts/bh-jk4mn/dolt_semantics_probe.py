"""Dolt rewind-semantics probe for spike bh-jk4mn. Opt-in evidence script, never collected.

Answers, by experiment on the installed Dolt (2.3.5 when written), what a hard reset to a
bootstrap TAG does and does not restore, on one repo served by a private ``dolt sql-server``
and pushed to a ``git+file://`` remote:

* reset to a tag name; monotonic ``BEFORE UPDATE`` trigger vs. reset; versioned triggers;
* what survives the reset: extra branches, tags, remote-tracking refs, untracked tables,
  ``dolt_ignore``'d tables, users/grants, ``SET PERSIST`` globals;
* server-mode visibility of the reset to other sessions, and an open transaction in another
  session committing across the reset;
* ``DOLT_CHECKOUT`` scope (session only) and the per-database default-branch global.

Run (scratch dir is created and removed; prints one JSON document)::

    uv run python docs/spikes/artifacts/bh-jk4mn/dolt_semantics_probe.py [--keep DIR]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Probe:
    def __init__(self, root: Path):
        self.root = root
        self.repo = root / "r"
        self.remote = root / "remote.git"
        home = root / "home"
        (home / ".dolt").mkdir(parents=True)
        (home / ".dolt" / "config_global.json").write_text(
            json.dumps(
                {"user.name": "probe", "user.email": "p@invalid", "metrics.disabled": "true"}
            )
        )
        self.env = {
            "PATH": os.environ["PATH"],
            "HOME": str(home),
            "DOLT_ROOT_PATH": str(home),
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        self.server: subprocess.Popen | None = None
        self.port = _free_port()
        self.out: dict = {}

    # -- plumbing ---------------------------------------------------------------------------
    def sh(self, *argv: str, cwd: Path | None = None, check: bool = True) -> str:
        res = subprocess.run(
            argv, cwd=cwd or self.repo, env=self.env, capture_output=True, text=True, timeout=120
        )
        if check and res.returncode:
            raise AssertionError(f"{argv}: {res.stdout}{res.stderr}")
        return res.stdout + res.stderr

    def cli(self, q: str) -> list[dict]:
        out = self.sh("dolt", "sql", "-r", "json", "-q", q).strip()
        return json.loads(out).get("rows", []) if out else []

    def start(self) -> None:
        log = (self.root / "server.log").open("a")
        self.server = subprocess.Popen(
            ["dolt", "sql-server", "--host", "127.0.0.1", "--port", str(self.port)],
            cwd=self.repo,
            env=self.env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        deadline = time.monotonic() + 30
        while True:
            try:
                socket.create_connection(("127.0.0.1", self.port), 0.2).close()
                return
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)

    def stop(self) -> None:
        if self.server is not None:
            self.server.terminate()
            self.server.wait(20)
            self.server = None

    def conn(self, db: str | None = "r", user: str = "root", password: str = ""):
        import pymysql

        return pymysql.connect(
            host="127.0.0.1",
            port=self.port,
            user=user,
            password=password,
            database=db,
            autocommit=True,
        )

    @staticmethod
    def q(conn, sql: str):
        with conn.cursor() as c:
            c.execute(sql)
            return c.fetchall()

    # -- probe ------------------------------------------------------------------------------
    def bootstrap(self) -> None:
        self.sh("git", "init", "-q", "--bare", "-b", "main", str(self.remote), cwd=self.root)
        seed = self.root / "seed"
        self.sh("git", "init", "-q", "-b", "main", str(seed), cwd=self.root)
        self.sh(
            "git", "-c", "user.name=s", "-c", "user.email=s@i", "commit", "-q",
            "--allow-empty", "-m", "seed", cwd=seed,
        )  # fmt: skip
        self.sh("git", "push", "-q", f"file://{self.remote}", "HEAD:main", cwd=seed)
        self.repo.mkdir()
        self.sh("dolt", "init", "-b", "main")
        ddl = self.root / "boot.sql"
        ddl.write_text(
            "insert into dolt_ignore values ('loc_%', true);\n"
            "create table t (id int primary key, v int);\n"
            "insert into t values (1, 10);\n"
            "create table w (id int primary key, epoch int);\n"
            "insert into w values (1, 5);\n"
            "delimiter //\n"
            "create trigger w_mono before update on w for each row begin\n"
            "  if new.epoch <= old.epoch then\n"
            "    signal sqlstate '45000' set message_text = 'epoch must grow';\n"
            "  end if;\n"
            "end//\n"
            "delimiter ;\n"
            "create table loc_ident (id int primary key, frame varchar(8));\n"
            "insert into loc_ident values (1, 'boot');\n"
        )
        self.sh("dolt", "sql", "--file", str(ddl))
        self.sh("dolt", "add", "-A")
        self.sh("dolt", "commit", "-m", "bootstrap")
        self.sh("dolt", "tag", "boot")
        self.sh("dolt", "remote", "add", "origin", f"git+file://{self.remote}")
        self.sh("dolt", "push", "origin", "main")
        self.boot = self.cli("select hashof('boot') h")[0]["h"]
        self.boot_data = self.sh(
            "git", "--git-dir", str(self.remote), "rev-parse", "refs/dolt/data"
        ).strip()
        self.out["remote_refs_after_bootstrap_push"] = self.sh(
            "git", "--git-dir", str(self.remote), "for-each-ref", "--format=%(refname)"
        ).split()

    def dirty(self, c) -> None:
        for sql in (
            "update w set epoch = 9 where id = 1",
            "insert into t values (2, 20)",
            "create trigger t_extra before insert on t for each row set new.v = new.v",
            "call dolt_add('-A')",
            "call dolt_commit('-m', 'test write')",
            "call dolt_branch('scratch')",
            "call dolt_tag('t-extra')",
            "call dolt_push('origin', 'main')",
            "insert into t values (3, 30)",  # unstaged working-set change
            "create table u (id int primary key)",  # untracked new table
            "update loc_ident set frame = 'dirty'",  # ignored table
            "create user 'probe_u'@'%' identified by 'x'",
            "grant select on r.* to 'probe_u'@'%'",
            "set persist max_connections = 77",
            "set global net_read_timeout = 77",
        ):
            self.q(c, sql)

    def state(self, c) -> dict:
        def rows(sql):
            return [list(r) for r in self.q(c, sql)]

        return {
            "head": rows(f"select hashof('main') = '{self.boot}'")[0][0] == 1,
            "t": rows("select id, v from t order by id"),
            "w_epoch": rows("select epoch from w")[0][0],
            "triggers": sorted(r[0] for r in rows("show triggers")),
            "branches": sorted(r[0] for r in rows("select name from dolt_branches")),
            "tags": sorted(r[0] for r in rows("select tag_name from dolt_tags")),
            "remote_branches": rows("select name, hash from dolt_remote_branches"),
            "tables": sorted(r[0] for r in rows("show tables")),
            "loc_ident": rows("select frame from loc_ident"),
            "status": rows("select table_name, staged, status from dolt_status"),
            "users": sorted(f"{u}@{h}" for u, h in rows("select user, host from mysql.user")),
            "max_connections": rows("select @@global.max_connections")[0][0],
            "net_read_timeout": rows("select @@global.net_read_timeout")[0][0],
        }

    def run(self) -> dict:
        self.bootstrap()
        self.start()
        c = self.conn()
        self.out["before_dirty"] = self.state(c)
        self.dirty(c)
        self.out["dirty"] = self.state(c)
        self.out["remote_data_moved"] = (
            self.sh("git", "--git-dir", str(self.remote), "rev-parse", "refs/dolt/data").strip()
            != self.boot_data
        )

        # (1) another session holds an open transaction with an uncommitted write
        other = self.conn()
        self.q(other, "set autocommit = 0")
        self.q(other, "start transaction")
        self.q(other, "insert into t values (99, 990)")

        t0 = time.perf_counter()
        self.q(c, "call dolt_reset('--hard', 'boot')")
        self.out["sql_reset_to_tag_seconds"] = round(time.perf_counter() - t0, 4)
        fresh = self.conn()
        self.out["after_sql_reset_seen_by_new_session"] = self.state(fresh)
        try:
            self.q(other, "commit")
            self.out["stale_txn_commit"] = "committed"
        except Exception as exc:  # noqa: BLE001 - recording the engine's answer
            self.out["stale_txn_commit"] = f"refused: {exc}"
        self.out["after_stale_txn_commit"] = self.state(self.conn())

        # (2) checkout scope and default branch
        a, b = self.conn(), self.conn()
        self.q(a, "call dolt_checkout('-b', 'per-test', 'boot')")
        self.out["checkout_scope"] = {
            "same_session": self.q(a, "select active_branch()")[0][0],
            "other_session": self.q(b, "select active_branch()")[0][0],
        }
        self.q(c, "set @@global.r_default_branch = 'per-test'")
        self.out["checkout_scope"]["new_session_after_default_branch_global"] = self.q(
            self.conn(), "select active_branch()"
        )[0][0]
        self.q(c, "set @@global.r_default_branch = ''")
        # a per-test branch is visible to the git remote only if pushed; record write routing
        self.q(a, "insert into t values (50, 500)")
        self.out["checkout_scope"]["write_on_per_test_lands_on_main"] = bool(
            self.q(self.conn(), "select count(*) from t where id = 50")[0][0]
        )

        # (2b) restoring globals: multi-assignment SET GLOBAL, RESET PERSIST
        self.q(c, "set global max_connections = 151, net_read_timeout = 28800")
        self.out["set_global_multi_assign"] = self.q(
            self.conn(), "select @@global.max_connections, @@global.net_read_timeout"
        )[0]
        try:
            self.q(c, "reset persist max_connections")
            self.out["reset_persist"] = "supported"
        except Exception as exc:  # noqa: BLE001 - recording the engine's answer
            self.out["reset_persist"] = f"refused: {exc}"
        self.q(c, "set global max_connections = 77")

        # (2c) the remote moves BACKWARDS: does a plain fetch move origin/main back?
        self.sh(
            "git", "--git-dir", str(self.remote), "update-ref", "refs/dolt/data", self.boot_data
        )
        before = self.q(c, "select hash from dolt_remote_branches")[0][0]
        self.q(c, "call dolt_fetch('origin')")
        after = self.q(c, "select hash from dolt_remote_branches")[0][0]
        self.out["fetch_after_remote_rewind"] = {
            "origin_main_moved_back": after == self.boot,
            "unchanged": after == before,
        }

        # (3) restart: which non-versioned settings survive a server restart?
        self.q(c, "call dolt_reset('--hard', 'boot')")
        self.stop()
        self.start()
        self.out["after_restart"] = self.state(self.conn())
        self.out["persisted_config_files"] = {
            str(p.relative_to(self.repo)): p.read_text()
            for p in (self.repo / ".dolt").glob("config.json")
        }
        self.out["doltcfg_files"] = sorted(
            str(p.relative_to(self.repo)) for p in self.repo.rglob(".doltcfg/*")
        )
        self.stop()

        # (4) CLI reset (what embedded/CLI frames run) to the tag, and timing
        self.sh("dolt", "sql", "-q", "insert into t values (4, 40)")
        self.sh("dolt", "add", "-A")
        self.sh("dolt", "commit", "-m", "cli write")
        t0 = time.perf_counter()
        self.sh("dolt", "reset", "--hard", "boot")
        self.out["cli_reset_to_tag_seconds"] = round(time.perf_counter() - t0, 4)
        self.out["after_cli_reset_t"] = self.cli("select id from t order by id")
        self.out["after_cli_reset_monotonic_trigger_epoch"] = self.cli("select epoch from w")
        return self.out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", type=Path)
    args = ap.parse_args()
    root = args.keep or Path(tempfile.mkdtemp(prefix="bh-jk4mn-probe-"))
    root.mkdir(parents=True, exist_ok=True)
    probe = Probe(root)
    try:
        out = probe.run()
        out["dolt_version"] = probe.sh("dolt", "version", cwd=root).splitlines()[0]
        print(json.dumps(out, indent=1, default=str))
    finally:
        probe.stop()
        if args.keep is None:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
