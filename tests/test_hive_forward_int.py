"""Integration (bh-g7dlo): the option A forward write path on PRIVATE scratch Dolt servers.

Every server here is test-owned: its own port, data dir, ``HOME`` and ``DOLT_ROOT_PATH`` under
``tmp_path``, TLS with a throwaway self-signed certificate where TLS is exercised. Nothing
touches a live HQ or hive server, ``~/.dolt`` or a live global.

* ``test_tls_forwarders_*`` — one TLS-only hive server (``require_secure_transport``) whose
  database a bd-server primary owns, with the product fence installed. Forwarder accounts are
  provisioned by :func:`beadhive.hive_forward.provision` through the primary's own ``bd sql``
  login; bd create, claim and close run through narrow logins over verified TLS; a claim race
  from three forwarder accounts has one winner per round; forwarded marks are stamped at the
  primary's epoch and ``fence_audit`` stays clean; a demoted primary is refused.
* ``test_operator_*`` (bh-7yuny, absorbing bh-hy2hp and bh-0tjiq) — a registered hive checkout
  on an operator frame whose LOCAL embedded store is a fresh, cut-over replica (its data names
  the primary as ``bh_writer``; its ``dolt_ignore``'d ``bh_local_ident`` never arrived).
  Forwarded, the ``bh bd`` passthrough (create, update, comment, dep, update --claim, close) and
  ``bh work claim`` land in the primary's data and leave the replica byte-for-byte unchanged, and
  the claim is granted by the primary, not refused by the forwarder's own writer gate. Not
  forwarded, the write that needs ``bh_local_ident`` provisions it, so the replica's guard
  refuses by name instead of with Error 1146, and ``bh report`` shows bd's whole message.
* ``test_quiesce_*`` — the composed prototype (``tests/harness/composed_fence.py``): a real
  adopt demotes the primary, and the product divert reset kills an in-flight forwarded
  transaction so its ``COMMIT`` is refused instead of acknowledged and dropped (``bh-uhx2r``
  E5).
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pymysql
import pytest
from typer.testing import CliRunner

from beadhive import (
    cli,
    fence_audit,
    fence_data,
    fence_schema,
    hive_forward,
    host,
    report,
    store_locator,
)
from beadhive.writer_adopt import PlacementView
from harness.beads import embedded_env
from harness.hive import make_hive
from harness.world import free_port

pytestmark = [
    pytest.mark.integration,
    pytest.mark.dolt_server,
    pytest.mark.skipif(
        shutil.which("bd") is None or shutil.which("dolt") is None, reason="bd/dolt not installed"
    ),
]

DB = "fx"
PW = "fixture-only-forwarder"
DOLT_CFG = {
    "user.name": "fixture",
    "user.email": "fixture@fixture.invalid",
    "versioncheck.disabled": "true",
    "metrics.disabled": "true",
}


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


class TlsHiveServer:
    """A private, TLS-only Dolt sql-server with a read-only DOLT_ROOT_PATH."""

    def __init__(self, base: Path):
        if shutil.which("openssl") is None:
            pytest.skip("openssl unavailable for the throwaway certificate")
        self.base = base
        self.port = free_port()
        self.data = base / "data"
        self.root = base / "server-root"
        self.home = base / "server-home"
        self.tls = base / "tls"
        for d in (self.data, self.root / ".dolt" / "eventsData", self.home, self.tls):
            d.mkdir(parents=True, exist_ok=True)
        _write(self.root / ".dolt" / "config_global.json", json.dumps(DOLT_CFG))
        (self.root / ".dolt" / "config_global.json").chmod(0o444)
        (self.root / ".dolt").chmod(0o555)
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(self.tls / "server.key"),
                "-out",
                str(self.tls / "server.crt"),
                "-days",
                "1",
                "-subj",
                "/CN=127.0.0.1",
                "-addext",
                "subjectAltName=IP:127.0.0.1",
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )
        self.ca = self.tls / "server.crt"
        _write(
            base / "server.yaml",
            f"log_level: info\nlistener:\n  host: 127.0.0.1\n  port: {self.port}\n"
            "  max_connections: 100\n  require_secure_transport: true\n"
            f"  tls_cert: {self.tls / 'server.crt'}\n  tls_key: {self.tls / 'server.key'}\n"
            f"data_dir: {self.data}\n",
        )
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        env = {**os.environ, "DOLT_ROOT_PATH": str(self.root), "HOME": str(self.home)}
        env.pop("DOLT_CLI_PASSWORD", None)
        log = (self.base / "server.log").open("a")
        self.proc = subprocess.Popen(
            [shutil.which("dolt"), "sql-server", "--config", str(self.base / "server.yaml")],
            cwd=self.base,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        end = time.monotonic() + 30
        while True:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                    return
            except OSError:
                if self.proc.poll() is not None or time.monotonic() > end:
                    raise AssertionError("scratch TLS dolt server failed to start") from None
                time.sleep(0.1)

    def close(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=15)
        (self.root / ".dolt").chmod(0o755)

    def connect(self, user: str = "root", password: str = "", **kw):
        import ssl

        ctx = ssl.create_default_context(cafile=str(self.ca))
        return pymysql.connect(
            host="127.0.0.1",
            port=self.port,
            user=user,
            password=password,
            database=DB,
            ssl=ctx,
            autocommit=True,
            read_timeout=60,
            **kw,
        )

    def endpoint(self, user: str) -> dict:
        return {
            "host": "127.0.0.1",
            "port": self.port,
            "database": DB,
            "user": user,
            "tls_mode": "required",
            "server_name": "127.0.0.1",
            "ca_file": str(self.ca),
            # Never resolved: BH_FORWARD_PASSWORD supplies the fixture password.
            "credential": {"config_path": "/nonexistent/fnox.toml", "profile": "t", "key": "PW"},
        }


def _client_env(base: Path, **extra: str) -> dict[str, str]:
    home = base / "home"
    (home / ".dolt").mkdir(parents=True, exist_ok=True)
    _write(home / ".dolt" / "config_global.json", json.dumps(DOLT_CFG))
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("BEADS_", "BD_", "BH_", "DOLT_")) and k != "SSL_CERT_FILE"
    }
    env.update(
        {
            "HOME": str(home),
            "DOLT_ROOT_PATH": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "BD_NON_INTERACTIVE": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "BEADS_DOLT_AUTO_START": "0",
        }
    )
    env.update(extra)
    return env


def _git_init(path: Path, env: dict) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], env=env, check=True)


def _bd(ws: Path, env: dict, *args: str, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bd", *args], cwd=ws, env=env, capture_output=True, text=True, timeout=timeout
    )


def _forwarder(base: Path, name: str) -> Path:
    ws = base / name
    env = _client_env(base / f"{name}-env")
    _git_init(ws, env)
    (ws / ".beads").mkdir()
    (ws / ".beads").chmod(0o700)
    meta = {"database": "dolt", "backend": "dolt", "dolt_mode": "server", "dolt_database": DB}
    _write(ws / ".beads" / "metadata.json", json.dumps(meta))
    return ws


def _fbd(ws: Path, *args: str, actor: str) -> subprocess.CompletedProcess:
    """A bd the way bh runs it in a forwarded checkout: the product's forward environment."""
    base = _client_env(ws.parent / f"{ws.name}-env", BEADS_ACTOR=actor)
    env = hive_forward.bd_env(ws, base=base)
    assert env is not None
    return _bd(ws, env, *args)


@pytest.fixture
def tls_hive(tmp_path, monkeypatch):
    """A TLS hive server; a bd-server primary ``p`` owning database fx, fence installed at
    epoch 1 and ``p``'s identity provisioned; a file remote for the managed push."""
    monkeypatch.setenv(hive_forward.PASSWORD_ENV, PW)
    server = TlsHiveServer(tmp_path / "server")
    server.start()
    try:
        penv = _client_env(
            tmp_path / "p-env",
            SSL_CERT_FILE=str(server.ca),
            BEADS_DOLT_SERVER_TLS="1",
            BEADS_ACTOR="p",
        )
        ws = tmp_path / "p"
        _git_init(ws, penv)
        init = _bd(
            ws,
            penv,
            "init",
            "--server",
            "--external",
            "--server-host",
            "127.0.0.1",
            "--server-port",
            str(server.port),
            "--server-user",
            "root",
            "-p",
            DB,
            "--quiet",
        )
        assert init.returncode == 0, init.stdout + init.stderr
        node = fence_data.FenceNode(fence_data.BdServerEngine(ws, env=penv))
        remote = tmp_path / "remote"
        remote.mkdir()
        node.engine.execute([f"CALL DOLT_REMOTE('add', 'origin', 'file://{remote}')"])
        node.install("p", 1)
        node.provision_ident("p")
        node.engine.execute(["CALL DOLT_PUSH('origin', 'main')"])
        yield server, node.engine, ws, penv, node
    finally:
        server.close()


def _provision(node, *names: str) -> list[hive_forward.Account]:
    accounts = [hive_forward.Account(n, "127.0.0.1") for n in names]
    for account in accounts:
        report = hive_forward.provision(node, database=DB, account=account, password=PW)
        assert report.created
    return accounts


def test_tls_forwarders_provisioning_conformance_and_refusals(tls_hive):
    server, node, _ws, _penv, _fence = tls_hive
    (fwd,) = _provision(node, "fwd-a")
    # The account is TLS-only and table-scoped; the primary's own login reads it back conformant.
    assert hive_forward.conformance(node, database=DB) == []
    grants = [str(next(iter(r.values()))) for r in node.query(f"SHOW GRANTS FOR {fwd.sql}")]
    assert not any(" ON `fx`.* " in g or " ON *.* " in g and "USAGE" not in g for g in grants)
    assert any("`bh_write_mark`" in g and "INSERT" in g and "UPDATE" not in g for g in grants)
    # A plaintext login is refused by the server; the forwarder's TLS login works.
    with pytest.raises(pymysql.MySQLError):
        pymysql.connect(host="127.0.0.1", port=server.port, user=fwd.user, password=PW)
    login = server.connect(fwd.user, PW)
    probes = {}
    for probe in (
        "UPDATE bh_local_ident SET frame = frame",
        "UPDATE bh_write_mark SET epoch = epoch",
        "CALL DOLT_COMMIT('--allow-empty', '-m', 'probe')",
        "CALL DOLT_RESET('--hard')",
    ):
        try:
            with login.cursor() as cur:
                cur.execute(probe)
            probes[probe] = "ok"
        except pymysql.MySQLError as exc:
            probes[probe] = f"refused: {exc.args[-1]}"[:80]
    login.close()
    assert all(v.startswith("refused") for v in probes.values()), probes

    # Shared, root and unpinned logins are refused by provisioning.
    for bad in ("'root'@'127.0.0.1'", "'fwd-x'@'%'", "'watchdog'@'127.0.0.1'"):
        with pytest.raises(hive_forward.ForwardRefused):
            hive_forward.provision(
                node, database=DB, account=hive_forward.Account.parse(bad), password=PW
            )
    with pytest.raises(hive_forward.ForwardRefused, match="never shared"):
        hive_forward.provision(
            node, database=DB, account=hive_forward.Account("fwd-a", "10.9.9.9"), password=PW
        )

    # A hand-made database-wide account is a conformance finding; re-provisioning narrows it.
    wide = hive_forward.Account("fwd-wide", "127.0.0.1")
    node.execute(
        [
            f"CREATE USER {wide.sql} IDENTIFIED BY 'x'",
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON fx.* TO {wide.sql}",
        ]
    )
    findings = hive_forward.conformance(node, database=DB)
    assert any("fwd-wide" in f and "database-wide" in f for f in findings), findings
    assert any("fwd-wide" in f and "REQUIRE SSL" in f for f in findings), findings
    # Dolt cannot add REQUIRE SSL in place: without a password it refuses, with one it
    # recreates the account TLS-only and table-scoped.
    with pytest.raises(hive_forward.ForwardError, match="cannot add in place"):
        hive_forward.provision(node, database=DB, account=wide)
    report = hive_forward.provision(node, database=DB, account=wide, password=PW)
    assert report.created
    assert hive_forward.conformance(node, database=DB) == []
    # An existing TLS account that was widened by hand is narrowed in place.
    node.execute(
        [f"GRANT ALL ON fx.* TO {fwd.sql}", f"GRANT UPDATE ON fx.bh_local_ident TO {fwd.sql}"]
    )
    widened = hive_forward.conformance(node, database=DB)
    # Dolt shows GRANT ALL expanded to its privilege list, so it reads as database-wide.
    assert any("fwd-a" in f and "database-wide" in f for f in widened), widened
    assert any("fwd-a" in f and "bh_local_ident" in f for f in widened), widened
    narrowed = hive_forward.provision(node, database=DB, account=fwd)
    assert not narrowed.created and narrowed.revoked
    assert hive_forward.conformance(node, database=DB) == []

    # bh doctor's primary report: globals at policy, grants conformant.
    settings = hive_forward.serve_settings({})
    payload = hive_forward.primary_report(
        node, database=DB, settings={**settings, "root_path": str(server.root)}
    )
    assert {g["name"]: g["status"] for g in payload["globals"]} == {
        name: "ok" for name in hive_forward.WATCHED_GLOBALS
    }
    assert payload["conformance"] == []
    if os.geteuid() != 0:
        assert payload["findings"] == []


def test_tls_forwarders_claim_in_one_place_and_a_demoted_primary_fails_closed(tls_hive, tmp_path):
    server, node, _ws, _penv, fence = tls_hive
    names = ("fwd-a", "fwd-b", "fwd-c")
    _provision(node, *names)
    forwarders = {}
    for name in names:
        ws = _forwarder(tmp_path / "frames", name)
        marker = hive_forward.repoint(
            ws,
            ("p", 1),
            prefix=DB,
            self_frame=name,
            endpoints={"p": server.endpoint(name)},
        )
        assert marker is not None and marker.state == "forwarding", marker
        forwarders[name] = ws
    marks_before = int(node.query("SELECT count(*) AS n FROM bh_write_mark")[0]["n"])

    # create / claim / close through the narrow logins, over TLS.
    made = _fbd(
        forwarders["fwd-a"],
        "create",
        "--title",
        "fwd-create",
        "-t",
        "task",
        "--json",
        actor="fwd-a/w0",
    )
    assert made.returncode == 0, made.stderr
    bead = json.loads(made.stdout)["id"]
    claimed = _fbd(forwarders["fwd-b"], "update", bead, "--claim", "--json", actor="fwd-b/w1")
    assert claimed.returncode == 0, claimed.stderr
    closed = _fbd(forwarders["fwd-b"], "close", bead, actor="fwd-b/w1")
    assert closed.returncode == 0, closed.stderr
    row = node.query(f"SELECT status, assignee FROM issues WHERE id = {fence_schema.quote(bead)}")
    assert row and row[0]["status"] == "closed"

    # Claim races: three forwarder accounts, two racers each, one winner per round.
    winners = []
    for n in range(3):
        made = _fbd(
            forwarders["fwd-a"],
            "create",
            "--title",
            f"race-{n}",
            "-t",
            "task",
            "--json",
            actor="fwd-a/w0",
        )
        assert made.returncode == 0, made.stderr
        bead = json.loads(made.stdout)["id"]
        gate = threading.Barrier(6)
        codes: dict[str, int] = {}

        def claim(name: str, actor: str, bead: str = bead, gate=gate, codes=codes) -> None:
            gate.wait()
            codes[actor] = _fbd(forwarders[name], "update", bead, "--claim", actor=actor).returncode

        threads = [
            threading.Thread(target=claim, args=(name, f"{name}/r{i}"))
            for name in names
            for i in range(2)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        won = [a for a, rc in codes.items() if rc == 0]
        winners.append(len(won))
        assignee = node.query(f"SELECT assignee FROM issues WHERE id = {fence_schema.quote(bead)}")
        assert won and assignee[0]["assignee"] == won[0], (codes, assignee)
    assert winners == [1, 1, 1]

    # Every forwarded write was stamped at the primary's epoch; the primary's managed commit and
    # push carry them, and fence_audit on the remote stays clean.
    marks = node.query("SELECT epoch, count(*) AS n FROM bh_write_mark GROUP BY epoch")
    assert [int(r["epoch"]) for r in marks] == [1]
    assert int(marks[0]["n"]) > marks_before
    assert node.commit("bh: commit working set before managed push")
    node.execute(["CALL DOLT_PUSH('origin', 'main')"])
    audit = fence_audit.fence_audit(fence, placement=PlacementView(frame="p", epoch=1))
    assert audit.cut_over and audit.ok, audit.findings()

    # Demotion: the data now names q at epoch 2 (the state after p's divert reset).
    node.execute(["UPDATE bh_writer SET frame = 'q', epoch = 2 WHERE id = 1"])
    ws = forwarders["fwd-c"]
    # Placement still names p (stale cache): the preflight reads p's data and refuses.
    refused = hive_forward.repoint(
        ws, ("p", 2), prefix=DB, self_frame="fwd-c", endpoints={"p": server.endpoint("fwd-c")}
    )
    assert refused.state == "refused" and "demoted primary" in refused.reason, refused
    with pytest.raises(hive_forward.ForwardRefused):
        hive_forward.bd_env(ws)
    # A bd that bypasses bh and reaches p anyway is refused by p's own guard.
    raw_env = _client_env(tmp_path / "raw-env", BEADS_ACTOR="fwd-a/raw")
    raw_env.update(
        {
            "BEADS_DOLT_SERVER_MODE": "1",
            "BEADS_DOLT_SERVER_HOST": "127.0.0.1",
            "BEADS_DOLT_SERVER_PORT": str(server.port),
            "BEADS_DOLT_SERVER_USER": "fwd-a",
            "BEADS_DOLT_SERVER_TLS": "1",
            "BEADS_DOLT_PASSWORD": PW,
            "SSL_CERT_FILE": str(server.ca),
        }
    )
    raw = _bd(forwarders["fwd-a"], raw_env, "create", "--title", "after-demotion", "-t", "task")
    assert raw.returncode != 0 and fence_schema.GUARD_REFUSAL in raw.stdout + raw.stderr


@pytest.fixture
def operator_replica(world, tls_hive, tmp_path, monkeypatch):
    """The operator frame's checkout of the hive: registered, with an embedded LOCAL store that
    is a fresh replica of the cut-over hive. Its data names ``p`` at epoch 1 (what a pull of the
    cut-over hive carries) and it has no ``bh_local_ident``, which is ``dolt_ignore``'d and never
    synced. A ``fwd-op`` forwarder account exists on ``p``'s hive server."""
    server, pnode, _ws, _penv, _fence = tls_hive
    monkeypatch.setenv("BH_BD_PASS_ENABLED", "1")  # the `bh bd` passthrough is opt-in
    _provision(pnode, "fwd-op")
    hive = make_hive(world, prefix=DB)
    db = store_locator.embedded_database_dir(hive.main)
    dolt_home = tmp_path / "op-dolt"
    _write(dolt_home / ".dolt" / "config_global.json", json.dumps(DOLT_CFG))
    local_env = {**embedded_env(), "DOLT_ROOT_PATH": str(dolt_home)}
    local = fence_data.FenceNode(fence_data.DoltCliEngine(db, env=local_env))
    # A replica is the SAME project as the primary (bd refuses a server whose project id is not
    # the checkout's): carry p's id into the local data and metadata.json, as a clone would.
    pid = str(pnode.query("SELECT value FROM metadata WHERE `key` = '_project_id'")[0]["value"])
    local.engine.execute([f"UPDATE metadata SET value = '{pid}' WHERE `key` = '_project_id'"])
    meta_path = hive.main / ".beads" / "metadata.json"
    meta_path.write_text(json.dumps({**json.loads(meta_path.read_text()), "project_id": pid}))
    local.install("p", 1)
    fence_data.reset_probe_cache()
    assert local.writer().frame == "p" and local.ident() is None
    try:
        yield hive, server, pnode, local
    finally:
        fence_data.reset_probe_cache()


def _local_state(local) -> dict:
    """Everything a local write would move: the commit, the working set, the rows bh writes."""
    head = local.query("SELECT hashof('HEAD') AS h")[0]["h"]
    count = lambda table: int(local.query(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"])  # noqa: E731
    return {
        "head": head,
        "working_set": local.query("SELECT table_name FROM dolt_status"),
        "issues": count("issues"),
        "comments": count("comments"),
        "dependencies": count("dependencies"),
        "marks": count("bh_write_mark"),
        "ident": local.ident(),
    }


def _bh(*args: str):
    res = CliRunner().invoke(cli.app, list(args), catch_exceptions=False)
    return res


def _primary_issue(pnode, title: str) -> dict:
    rows = pnode.query(
        f"SELECT id, status, assignee, notes FROM issues WHERE title = {fence_schema.quote(title)}"
    )
    assert len(rows) == 1, rows
    return rows[0]


def test_operator_forwarded_passthrough_and_claim_land_on_the_primary_only(operator_replica):
    """bh-hy2hp + bh-0tjiq: with the checkout forwarded, every authoring write reaches the
    primary's data, and the forwarder's own replica is not written at all."""
    hive, server, pnode, local = operator_replica
    me = host.host_id()
    marker = hive_forward.repoint(
        hive.main, ("p", 1), prefix=DB, self_frame=me, endpoints={"p": server.endpoint("fwd-op")}
    )
    assert marker is not None and marker.state == "forwarding", marker
    before = _local_state(local)
    primary_marks = int(pnode.query("SELECT COUNT(*) AS n FROM bh_write_mark")[0]["n"])

    # create — `bh bd create` used to ignore the marker and write the replica (hive_aware=False).
    for title in ("forwarded one", "forwarded two"):
        res = _bh("bd", "create", title, "-t", "task")
        assert res.exit_code == 0, res.output
    one = _primary_issue(pnode, "forwarded one")["id"]
    two = _primary_issue(pnode, "forwarded two")["id"]

    res = _bh("bd", "update", one, "--notes", "forwarded update")
    assert res.exit_code == 0, res.output
    assert _primary_issue(pnode, "forwarded one")["notes"] == "forwarded update"

    res = _bh("bd", "comments", "add", one, "forwarded comment")
    assert res.exit_code == 0, res.output
    comments = pnode.query(f"SELECT text FROM comments WHERE issue_id = {fence_schema.quote(one)}")
    assert [r["text"] for r in comments] == ["forwarded comment"]

    res = _bh("bd", "dep", "add", one, two)
    assert res.exit_code == 0, res.output
    deps = pnode.query(
        "SELECT depends_on_issue_id AS d FROM dependencies WHERE issue_id = "
        + fence_schema.quote(one)
    )
    assert [r["d"] for r in deps] == [two]

    # claim — the forwarder's local writer gate used to refuse it: its replica names p.
    res = _bh("work", "claim", one, "--as", "dev/op")
    assert res.exit_code == 0, res.output
    claimed = _primary_issue(pnode, "forwarded one")
    assert (claimed["status"], claimed["assignee"]) == ("in_progress", "dev/op")

    res = _bh("bd", "update", two, "--claim", "--actor", "dev/op2")
    assert res.exit_code == 0, res.output
    assert _primary_issue(pnode, "forwarded two")["assignee"] == "dev/op2"
    res = _bh("bd", "close", two, "--actor", "dev/op2")
    assert res.exit_code == 0, res.output
    assert _primary_issue(pnode, "forwarded two")["status"] == "closed"

    # Every write ran under the primary's guard, at its epoch; nothing touched the replica.
    marks = pnode.query("SELECT epoch, COUNT(*) AS n FROM bh_write_mark GROUP BY epoch")
    assert [int(r["epoch"]) for r in marks] == [1]
    assert int(marks[0]["n"]) > primary_marks
    assert _local_state(local) == before
    assert before["issues"] == 0 and before["ident"] is None


def test_operator_refused_forward_fails_closed_before_the_local_gate(operator_replica):
    """A refused forward refuses the claim with the forwarding reason; it never falls back to
    writing this frame's replica."""
    hive, _server, pnode, local = operator_replica
    hive_forward.refuse(
        hive.main, "the placed primary is unreachable", prefix=DB, self_frame="op", endpoints={}
    )
    before = _local_state(local)
    res = _bh("work", "claim", f"{DB}-nope", "--as", "dev/op")
    assert res.exit_code == 1
    assert "forwarding is refused" in res.output and "unreachable" in res.output
    res = _bh("bd", "update", f"{DB}-nope", "--claim")
    assert res.exit_code == 1
    assert _local_state(local) == before


def test_operator_fresh_replica_provisions_its_identity_and_report_shows_the_error(
    operator_replica,
):
    """bh-0tjiq: not forwarded, a write on the fresh replica used to die with Error 1146 on the
    missing ``bh_local_ident`` and ``bh report`` printed only ``bd create failed: {``. The write
    that needs the identity now provisions it (no HQ, no runtime frame), the replica's guard
    refuses by name, and the report surfaces bd's whole message."""
    hive, _server, _pnode, local = operator_replica
    # The defect, reproduced with a raw bd that bypasses bh.
    raw = subprocess.run(
        ["bd", "-C", str(hive.main), "create", "raw on a replica", "-t", "task"],
        capture_output=True,
        text=True,
        env=embedded_env(),
        timeout=120,
    )
    assert raw.returncode != 0 and fence_schema.LOCAL_IDENT_TABLE in raw.stdout + raw.stderr
    head = local.query("SELECT hashof('HEAD') AS h")[0]["h"]

    code, error, new_id = report.file_report(
        hive.repo, "filed from a replica", "bug", "op/reporter"
    )

    assert (code != 0, new_id) == (True, "")
    assert fence_schema.GUARD_REFUSAL in error, error
    assert "1146" not in error and fence_schema.LOCAL_IDENT_TABLE not in error
    assert error != "bd create failed: {"
    # Provisioned as this host's replica identity: never staged, committed or pushed.
    assert local.ident() == (host.host_id(), "replica")
    assert local.staged_local_tables() == []
    assert local.query("SELECT hashof('HEAD') AS h")[0]["h"] == head
    assert int(local.query("SELECT COUNT(*) AS n FROM issues")[0]["n"]) == 0


def test_quiesce_refuses_an_in_flight_forwarded_write_across_the_divert_reset(tmp_path):
    """bh-uhx2r E5 with the product reset: without the kill the forwarder's COMMIT returns ok
    and the row vanishes; with it the COMMIT fails and nothing was acknowledged."""
    from harness import composed_fence as cf
    from harness.writer_fencing import BD_EMBEDDED, BD_SERVER, DOLT_CLI

    frames = [("q", BD_EMBEDDED), ("p", BD_SERVER), ("f", DOLT_CLI)]
    with cf.composed_world(tmp_path, frames) as w:
        p = w["p"]

        def take(name: str) -> None:
            placed = w.hq.place(name)
            assert cf.adopt(w.cluster, w[name], placed.epoch).landed
            w.moves.observe("adopt", name)

        take("p")
        root = pymysql.connect(
            host="127.0.0.1", port=p.port, user="root", database=DB, autocommit=True
        )
        admin = hive_forward.DbapiSql(root)
        fwd = hive_forward.Account("fwd-f", "127.0.0.1")
        hive_forward.provision(admin, database=DB, account=fwd, password=PW, require_tls=False)
        assert hive_forward.conformance(admin, database=DB, require_tls=False) == []

        # A forwarded bd write through the product environment lands under p's guard.
        ws = tmp_path / "fwd-ws"
        _git_init(ws, dict(w["f"].env))
        (ws / ".beads").mkdir(parents=True)
        (ws / ".beads").chmod(0o700)
        meta = {"database": "dolt", "backend": "dolt", "dolt_mode": "server", "dolt_database": DB}
        _write(ws / ".beads" / "metadata.json", json.dumps(meta))
        endpoint = {
            "host": "127.0.0.1",
            "port": p.port,
            "database": DB,
            "user": fwd.user,
            "tls_mode": "disabled",
        }
        hive_forward.point(
            ws,
            hive_forward.ForwardTarget("p", 1, endpoint),
            prefix=DB,
            self_frame="f",
            endpoints={"p": endpoint},
        )
        os.environ[hive_forward.PASSWORD_ENV] = PW
        try:
            env = hive_forward.bd_env(ws, base=dict(w["f"].env, BEADS_ACTOR="f/w0"))
        finally:
            os.environ.pop(hive_forward.PASSWORD_ENV, None)
        made = _bd(ws, env, "create", "--title", "quiesce-landed", "-t", "task", "--json")
        assert made.returncode == 0, made.stderr
        assert cf.managed_push(p) == "pushed"

        # In flight: the forwarder's transaction passed p's guard before q adopted.
        txn = pymysql.connect(
            host="127.0.0.1", port=p.port, user=fwd.user, password=PW, database=DB, read_timeout=60
        )
        with txn.cursor() as cur:
            cur.execute("START TRANSACTION")
            cur.execute(
                "INSERT INTO issues (id, title, description, design, acceptance_criteria, notes)"
                f" VALUES ('{DB}-inflight', 'quiesce-inflight', '', '', '', '')"
            )
        take("q")
        # p's product divert reset: fetch, kill forwarder sessions, DOLT_RESET --hard.
        engine = fence_data.BdServerEngine(p.hive, env=p.env)
        os.environ.pop(hive_forward.QUIESCE_ENV, None)
        engine.reset_to_remote()
        with pytest.raises(pymysql.MySQLError):
            with txn.cursor() as cur:
                cur.execute("COMMIT")
        with contextlib.suppress(Exception):
            txn.close()
        assert not p.query("SELECT id FROM issues WHERE title = 'quiesce-inflight'")
        assert p.query("SELECT id FROM issues WHERE title = 'quiesce-landed'")

        # p is demoted: a forwarder preflight against it refuses, and so does its guard.
        login = hive_forward.DbapiSql(
            pymysql.connect(
                host="127.0.0.1",
                port=p.port,
                user=fwd.user,
                password=PW,
                database=DB,
                autocommit=True,
            )
        )
        with pytest.raises(hive_forward.ForwardRefused, match="demoted primary"):
            hive_forward.preflight(login, hive_forward.ForwardTarget("p", 2, endpoint))
        late = _bd(ws, env, "create", "--title", "after-demotion", "-t", "task")
        assert late.returncode != 0 and fence_schema.GUARD_REFUSAL in late.stdout + late.stderr
        root.close()
        assert cf.check_invariants(w)["ok"]
