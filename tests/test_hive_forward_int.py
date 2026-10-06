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

from beadhive import fence_audit, fence_data, fence_schema, hive_forward
from beadhive.writer_adopt import PlacementView
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
