"""Executable evidence for the bh-uhx2r spike (M13); not a product contract test.

Questions (``docs/spikes/bh-uhx2r-forward-rpc-option-b.md``):

1. Does a forwarder's ``SET GLOBAL dolt_force_transaction_commit = 1`` (or another watched
   global) on the primary's hive ``dolt sql-server`` let a stale-epoch write land on remote
   ``main`` past FK epoch retirement and the write guard?
2. If not the global, what DOES a forwarder's Dolt login let it land, and can the option A grant
   shape close it?
3. Can a per-hive bh RPC service on the primary carry forwarded create / claim / close so the
   executor holds no Dolt login at all (option B)?

Everything runs on :mod:`harness.composed_fence` (the bh-jbb6r composed prototype): ``p`` is a
bd-server primary on its own owned Dolt 2.3.5 ``sql-server`` (frame-private port, data dir,
``HOME`` and ``DOLT_ROOT_PATH`` under ``tmp_path``), ``q`` the adopter, ``f`` the forwarding
host. No live HQ or hive server is touched; the ``SET GLOBAL`` / ``SET PERSIST`` probes land in
the owned server only.

``BH_UHX2R_EVIDENCE=<file>`` appends each test's measured table as JSON lines.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import statistics
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pymysql
import pytest

from harness import composed_fence as cf
from harness import epoch_fence as ef
from harness import write_guard as wg
from harness.writer_fencing import BD_EMBEDDED, BD_SERVER, DOLT_CLI, Run

pytestmark = [
    pytest.mark.integration,
    pytest.mark.dolt_server,
    pytest.mark.skipif(
        shutil.which("bd") is None or shutil.which("dolt") is None, reason="bd/dolt not installed"
    ),
]

FRAMES = [("q", BD_EMBEDDED), ("p", BD_SERVER), ("f", DOLT_CLI)]
DB = "fx"  # the fixture cluster's prefix == bd's server database
PW = "fixture-only"

#: The globals condition 16's watchdog names that a login CAN set globally on 2.3.5.
#: ``dolt_allow_commit_conflicts`` is session-only (E1 measures the refusal).
GLOBALS = {
    "none": [],
    "force": ["SET GLOBAL dolt_force_transaction_commit = 1"],
    "force+txn_commit": [
        "SET GLOBAL dolt_force_transaction_commit = 1",
        "SET GLOBAL dolt_transaction_commit = 1",
    ],
}
RESET_GLOBALS = [
    "SET GLOBAL dolt_force_transaction_commit = 0",
    "SET GLOBAL dolt_transaction_commit = 0",
]
#: An honest bh-side SQL session that publishes without ``--force`` (what a managed path would do).
SQL_MERGE = [
    "CALL DOLT_COMMIT('-Am', 'commit working set')",
    "CALL DOLT_FETCH('origin')",
    "CALL DOLT_MERGE('origin/main')",
    "CALL DOLT_COMMIT('-Am', 'merge origin/main')",
    "CALL DOLT_PUSH('origin', 'main')",
]
#: Probes for the login capability matrix (E4).
PROBES = [
    "SET GLOBAL dolt_force_transaction_commit = 1",
    "SET GLOBAL dolt_force_transaction_commit = 0",
    "SET PERSIST max_connections = 777",
    "UPDATE bh_local_ident SET frame = frame",
    "UPDATE bh_write_mark SET epoch = epoch",
    "DELETE FROM dolt_constraint_violations_bh_write_mark",
    "UPDATE issues SET notes = notes",
    "CALL DOLT_COMMIT('--allow-empty', '-m', 'probe')",
    "CALL DOLT_MERGE('main')",
    "CALL DOLT_RESET('--hard')",
    "CALL DOLT_FETCH('origin')",
    "CALL DOLT_PUSH('--force', 'origin', 'main')",
]
#: Fence tables a forwarder may read; ``bh_write_mark`` also takes the guard trigger's INSERT
#: (trigger DML is invoker-checked on 2.3.5, bh-wtsrc E3).
FENCE_READ_ONLY = ("bh_writer", "bh_epoch_live", "bh_local_ident")


def _evidence(name: str, data: object) -> None:
    line = json.dumps({"test": name, "data": data}, sort_keys=True, default=str)
    print(f"BH_UHX2R {line}")
    target = os.environ.get("BH_UHX2R_EVIDENCE")
    if target:
        with open(target, "a") as fh:
            fh.write(line + "\n")


def _connect(p, user: str = "root", password: str = "", *, autocommit: bool = True):
    return pymysql.connect(
        host="127.0.0.1",
        port=p.port,
        user=user,
        password=password,
        database=DB,
        autocommit=autocommit,
        read_timeout=60,
    )


def _try(conn, statement: str) -> str:
    """``ok`` or the server's refusal, first 110 chars."""
    try:
        with conn.cursor() as cur:
            cur.execute(statement)
            cur.fetchall()
        return "ok"
    except pymysql.MySQLError as exc:
        return "refused: " + str(exc.args[-1]).splitlines()[0][:110]


def _last(run: Run | subprocess.CompletedProcess) -> str:
    out = (run.stdout or "") + (run.stderr or "")
    lines = [ln for ln in out.strip().splitlines() if "deprecated" not in ln and ln.strip()]
    return f"rc={run.returncode} {lines[-1].strip()[:110] if lines else ''}"


def _take(w: cf.World, name: str) -> None:
    placed = w.hq.place(name)
    result = cf.adopt(w.cluster, w[name], placed.epoch)
    w.moves.observe("adopt", name)
    assert result.landed, result


def _landed(w: cf.World, title: str) -> bool:
    return title in ef.remote_fence(w.cluster)["titles"]


def _stale_forwarded_write(w: cf.World, fwd: cf.Forwarder, title: str) -> None:
    """``p`` is primary; a forwarded ``bd create`` executes on it; then ``q`` adopts, so ``p``
    is a demoted primary holding one unpublished forwarded write at a retired epoch."""
    for frame in w.cluster.frames.values():
        (frame.hive / ".beads" / "push-state.json").unlink(missing_ok=True)
    _take(w, "p")
    made = fwd.bd("create", "--title", title, "-t", "task", "--json", actor="f")
    assert made.returncode == 0, made.stderr
    _take(w, "q")


def _recover(w: cf.World) -> None:
    w["p"].bd("sql", "CALL DOLT_MERGE('--abort')")
    cf.divert(w["p"])


def _user(root, name: str, grants: list[str]) -> None:
    assert _try(root, f"CREATE USER '{name}'@'127.0.0.1' IDENTIFIED BY '{PW}'") == "ok"
    for grant in grants:
        assert _try(root, grant.format(user=f"'{name}'@'127.0.0.1'")) == "ok", grant


def _narrow_grants(root) -> list[str]:
    """Option A, narrowed: DML on bd's own tables and ``dolt_ignore`` (bd seeds it on open),
    SELECT on the fence, SELECT + INSERT on ``bh_write_mark``. No database-wide grant."""
    with root.cursor() as cur:
        cur.execute("SHOW FULL TABLES WHERE Table_type = 'BASE TABLE'")
        tables = [row[0] for row in cur.fetchall()]
    grants = []
    for table in [*tables, "dolt_ignore"]:
        if table in FENCE_READ_ONLY:
            priv = "SELECT"
        elif table == "bh_write_mark":
            priv = "SELECT, INSERT"
        else:
            priv = "SELECT, INSERT, UPDATE, DELETE"
        grants.append(f"GRANT {priv} ON {DB}.`{table}` TO {{user}}")
    return grants


GRANT_SHAPES = {
    "db_all": lambda root: [f"GRANT ALL ON {DB}.* TO {{user}}"],
    "db_dml": lambda root: [f"GRANT SELECT, INSERT, UPDATE, DELETE ON {DB}.* TO {{user}}"],
    "narrow": _narrow_grants,
}


def _forwarder_as(f, p, user: str) -> cf.Forwarder:
    fwd = cf.Forwarder(f, p, name=f"fwd-{user}")
    fwd.env.update(BEADS_DOLT_SERVER_USER=user, BEADS_DOLT_PASSWORD=PW)
    return fwd


# =============================================================================================
# E1 -- a forced global on the primary's hive server, then every honest publish path
# =============================================================================================

PATHS = ("bd-pull-then-push", "bd-sync", "bd-vc-merge-then-push", "sql-merge-no-force", "managed")


def test_e1_forced_globals_land_no_stale_write_on_any_honest_path(tmp_path):
    with cf.composed_world(tmp_path, FRAMES) as w:
        p, f = w["p"], w["f"]
        fwd = cf.Forwarder(f, p)  # the default forward path: bd's own (root) login
        root = _connect(p)
        session_only = _try(root, "SET GLOBAL dolt_allow_commit_conflicts = 1")
        assert "SESSION variable" in session_only, session_only
        table = [{"probe": "SET GLOBAL dolt_allow_commit_conflicts = 1", "result": session_only}]
        for glob, statements in GLOBALS.items():
            for path in PATHS:
                title = f"e1-{glob}-{path}"
                _stale_forwarded_write(w, fwd, title)
                forwarder = _connect(p)  # the forwarder's login sets the global(s)
                for statement in statements:
                    assert _try(forwarder, statement) == "ok", statement
                forwarder.close()
                seen = _connect(p)  # a NEW session inherits them
                with seen.cursor() as cur:
                    cur.execute("SELECT @@dolt_force_transaction_commit, @@dolt_transaction_commit")
                    inherited = list(cur.fetchone())
                seen.close()
                steps: dict[str, str] = {}
                if path == "bd-pull-then-push":
                    steps["pull"] = _last(p.pull())
                    steps["push"] = _last(p.push())
                elif path == "bd-sync":
                    steps["sync"] = _last(p.bd("sync"))
                elif path == "bd-vc-merge-then-push":
                    p.bd("sql", "CALL DOLT_FETCH('origin')").check()
                    steps["merge"] = _last(p.bd("vc", "merge", "origin/main"))
                    steps["push"] = _last(p.push())
                elif path == "sql-merge-no-force":
                    honest = _connect(p)
                    for statement in SQL_MERGE:
                        steps[statement.split("(")[0][5:].lower()] = _try(honest, statement)
                    honest.close()
                else:
                    steps["managed_push"] = cf.managed_push(p)
                landed = _landed(w, title)
                table.append(
                    {"globals": glob, "inherited": inherited, "path": path, "landed": landed}
                    | steps
                )
                restore = _connect(p)
                for statement in RESET_GLOBALS:
                    _try(restore, statement)
                restore.close()
                _recover(w)
                assert not landed, table[-1]
        root.close()
        _evidence("e1", table)
        result = cf.check_invariants(w)
        assert result["ok"], result["violations"]


# =============================================================================================
# E2 / E3 -- what a forwarder's login CAN land, with no global at all
# =============================================================================================


def test_e2_db_wide_all_login_lands_a_stale_write_without_any_global(tmp_path):
    """A forwarder granted ALL on the hive database merges the bump into the demoted primary,
    re-stamps its stale mark, clears the violation and commits -- session-level SET only. The
    primary's own honest managed push then publishes it as a fast-forward."""
    with cf.composed_world(tmp_path, FRAMES) as w:
        p, f = w["p"], w["f"]
        root = _connect(p)
        _user(root, "fwd", GRANT_SHAPES["db_all"](root))
        fwd = _forwarder_as(f, p, "fwd")
        _stale_forwarded_write(w, fwd, "e2-stale")
        p.bd("sql", "CALL DOLT_FETCH('origin')").check()  # the primary's routine fetch
        attacker = _connect(p, "fwd", PW)
        steps = {}
        for statement in [
            "SELECT @@GLOBAL.dolt_force_transaction_commit",
            "CALL DOLT_COMMIT('-Am', 'commit working set')",
            "SET @@SESSION.dolt_force_transaction_commit = 1",
            "CALL DOLT_MERGE('origin/main')",
            "SET @live = (SELECT epoch FROM bh_epoch_live)",
            "UPDATE bh_write_mark SET epoch = @live WHERE epoch <> @live",
            "DELETE FROM dolt_constraint_violations_bh_write_mark",
            "CALL DOLT_COMMIT('-Am', 'routine')",
        ]:
            steps[statement] = _try(attacker, statement)
        attacker.close()
        steps["primary managed_push"] = cf.managed_push(p)
        w.moves.observe("forced", "p")
        landed = _landed(w, "e2-stale")
        result = cf.check_invariants(w)
        flagged = sorted(k for k, v in result["violations"].items() if v)
        _evidence("e2", {"steps": steps, "landed": landed, "flagged": flagged})
        assert landed
        # Only the history-reading invariants see it; the re-stamp leaves no stale mark, so
        # the product detector's checks (fence_audit: stale_marks / epoch_regressed /
        # placement_ahead) stay clean.
        assert not result["ok"] and "i3_late_epoch_commit" in flagged
        assert "audit" not in flagged


def test_e3_db_wide_dml_login_spoofs_the_guard_identity_on_a_demoted_primary(tmp_path):
    """DML on the whole database includes ``bh_local_ident``. After the demoted primary has
    diverted and reset (the managed rejoin), the forwarder renames the node to the new writer;
    the guard then admits writes on the WRONG frame, stamped at the live epoch."""
    with cf.composed_world(tmp_path, FRAMES) as w:
        p, f = w["p"], w["f"]
        root = _connect(p)
        _user(root, "fwd", GRANT_SHAPES["db_dml"](root))
        fwd = _forwarder_as(f, p, "fwd")
        _stale_forwarded_write(w, fwd, "e3-before")
        cf.divert(p)  # demoted primary is now exactly the remote head; its guard refuses
        refused = fwd.bd("create", "--title", "e3-refused", "-t", "task", actor="f")
        assert refused.returncode != 0 and wg.GUARD_REFUSAL in refused.stderr, refused.stderr
        attacker = _connect(p, "fwd", PW)
        spoof = _try(attacker, "UPDATE bh_local_ident SET frame = 'q'")
        attacker.close()
        made = fwd.bd("create", "--title", "e3-spoofed", "-t", "task", actor="f")
        pushed = cf.managed_push(p)
        w.moves.observe("managed_push", "p")
        landed = _landed(w, "e3-spoofed")
        marks = ef.remote_fence(w.cluster)["marks"]
        result = cf.check_invariants(w)
        flagged = sorted(k for k, v in result["violations"].items() if v)
        _evidence(
            "e3",
            {
                "refused_before": _last(refused),
                "spoof": spoof,
                "create_after": made.returncode,
                "primary managed_push": pushed,
                "landed": landed,
                "remote_marks": marks,
                "flagged": flagged,
            },
        )
        assert spoof == "ok" and made.returncode == 0 and landed
        # Marks at the live epoch, from a frame that is not bh_writer. Only I1 sees it, and only
        # because the fixture's move log knows which frame pushed -- information production does
        # not have (bd commits as beads/root, bh-jbb6r E4). The history invariants and the
        # product detector (fence_audit) are clean: a second claim-granting writer, undetected.
        assert flagged == ["i1_one_writer_per_epoch"]


# =============================================================================================
# E4 -- the login capability matrix per grant shape; bd's verbs under the narrow shape
# =============================================================================================


def test_e4_grant_shapes_capability_matrix(tmp_path):
    with cf.composed_world(tmp_path, FRAMES) as w:
        p, f = w["p"], w["f"]
        _take(w, "p")
        root = _connect(p)
        matrix: dict[str, dict[str, str]] = {}
        verbs: dict[str, dict[str, str]] = {}
        for shape, grants in GRANT_SHAPES.items():
            _user(root, shape, grants(root))
            fwd = _forwarder_as(f, p, shape)
            made = fwd.bd("create", "--title", f"e4-{shape}", "-t", "task", "--json", actor="f")
            verbs[shape] = {"create": _last(made)}
            if made.returncode == 0:
                bead = json.loads(made.stdout)["id"]
                claimed = fwd.bd("update", bead, "--claim", "--json", actor="f")
                verbs[shape]["claim"] = _last(claimed)
                verbs[shape]["close"] = _last(fwd.bd("close", bead, actor="f"))
            login = _connect(p, shape, PW)
            matrix[shape] = {probe: _try(login, probe) for probe in PROBES}
            login.close()
            for statement in RESET_GLOBALS:
                _try(root, statement)
        root.close()
        _evidence("e4", {"verbs": verbs, "matrix": matrix})
        narrow = matrix["narrow"]
        # What option A cannot remove, whatever the grants:
        assert narrow["SET GLOBAL dolt_force_transaction_commit = 1"] == "ok"
        assert narrow["SET PERSIST max_connections = 777"] == "ok"
        assert narrow["UPDATE issues SET notes = notes"] == "ok"
        # What the narrow shape does close:
        for probe in PROBES[3:6] + PROBES[7:]:
            assert narrow[probe].startswith("refused"), (probe, narrow[probe])
        assert all(v.startswith("rc=0") for v in verbs["narrow"].values()), verbs["narrow"]
        assert matrix["db_dml"]["UPDATE bh_local_ident SET frame = frame"] == "ok"
        assert matrix["db_all"]["CALL DOLT_MERGE('main')"] == "ok"
        assert matrix["db_all"]["CALL DOLT_PUSH('--force', 'origin', 'main')"].startswith("refused")


# =============================================================================================
# E5 -- an in-flight forwarded transaction across the demoted primary's reset
# =============================================================================================


def test_e5_in_flight_forwarded_write_is_acked_then_dropped_by_the_divert_reset(tmp_path):
    """Not a landing path: a forwarded transaction that passed the guard before ``q`` adopted
    commits OK after ``p``'s managed rejoin reset, and its row is simply gone -- with or
    without the forced global. The control (no reset in between) keeps it."""
    with cf.composed_world(tmp_path, FRAMES) as w:
        p = w["p"]
        rows = []
        for case in ("control", "reset", "reset+force"):
            for frame in w.cluster.frames.values():
                (frame.hive / ".beads" / "push-state.json").unlink(missing_ok=True)
            _take(w, "p")
            root = _connect(p)
            if case == "reset+force":
                assert _try(root, GLOBALS["force"][0]) == "ok"
            title = f"e5-{case}"
            txn = _connect(p, autocommit=False)
            inserted = _try(
                txn,
                "INSERT INTO issues (id, title, description, design, acceptance_criteria, notes)"
                f" VALUES ('{DB}-e5-{case.replace('+', '-')}', '{title}', '', '', '', '')",
            )
            if case != "control":
                _take(w, "q")
                cf.divert(p)
            committed = _try(txn, "COMMIT")
            txn.close()
            present = bool(p.query(f"SELECT id FROM issues WHERE title = '{title}'"))
            violations = int(p.query("SELECT count(*) n FROM dolt_constraint_violations")[0]["n"])
            pushed = cf.managed_push(p)
            rows.append(
                {
                    "case": case,
                    "insert": inserted,
                    "commit": committed,
                    "row_present_on_primary": present,
                    "violations": violations,
                    "managed_push": pushed,
                    "landed": _landed(w, title),
                }
            )
            for statement in RESET_GLOBALS:
                _try(root, statement)
            root.close()
            _recover(w)
        _evidence("e5", rows)
        by = {r["case"]: r for r in rows}
        assert by["control"]["row_present_on_primary"] and by["control"]["landed"]
        for case in ("reset", "reset+force"):
            assert by[case]["commit"] == "ok" and not by[case]["row_present_on_primary"]
            assert not by[case]["landed"]
        assert cf.check_invariants(w)["ok"]


# =============================================================================================
# E6 -- option B prototype: a per-hive bh RPC service on the primary, no Dolt login on executors
# =============================================================================================

_ID = re.compile(rf"^{DB}-[a-z0-9.]{{1,32}}$")
_ACTOR = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")


class _RpcHandler(BaseHTTPRequestHandler):
    """Closed verb set, per-frame bearer token (stand-in for an mTLS client cert), argv built
    by the service, bd run on the primary's own workspace and login. No SQL is expressible."""

    def log_message(self, *args) -> None:  # quiet
        return

    def _reply(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:  # noqa: N802
        svc = self.server
        frame = svc.tokens.get(self.headers.get("Authorization", "").removeprefix("Bearer "))
        if frame is None:
            return self._reply(401, {"error": "unknown frame"})
        length = int(self.headers.get("Content-Length") or 0)
        if length > 4096:
            return self._reply(413, {"error": "too large"})
        try:
            req = json.loads(self.rfile.read(length))
            verb, actor = req["verb"], req["actor"]
        except (ValueError, KeyError, TypeError):
            return self._reply(400, {"error": "bad request"})
        if not isinstance(actor, str) or not _ACTOR.match(actor):
            return self._reply(400, {"error": "bad actor"})
        bead = req.get("id")
        if verb == "create":
            title = req.get("title")
            if not isinstance(title, str) or not 0 < len(title) <= 200:
                return self._reply(400, {"error": "bad title"})
            argv = ["create", "--title", title, "-t", "task", "--json"]
        elif verb in ("claim", "close"):
            if not isinstance(bead, str) or not _ID.match(bead):
                return self._reply(400, {"error": "bad id"})
            argv = ["update", bead, "--claim", "--json"] if verb == "claim" else ["close", bead]
        else:
            return self._reply(403, {"error": f"verb {verb!r} not offered"})
        env = dict(svc.primary.env, BEADS_ACTOR=f"{frame}/{actor}")
        done = subprocess.run(
            ["bd", *argv],
            cwd=svc.primary.hive,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return self._reply(200, {"rc": done.returncode, "out": done.stdout, "err": done.stderr})


class RpcService:
    def __init__(self, primary, tokens: dict[str, str]):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _RpcHandler)
        self.httpd.primary = primary  # type: ignore[attr-defined]
        self.httpd.tokens = tokens  # type: ignore[attr-defined]
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> RpcService:
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def _call(url: str, token: str, body: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers={"Authorization": f"Bearer {token}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def test_e6_rpc_service_carries_create_claim_close_with_no_executor_login(tmp_path):
    rounds = int(os.environ.get("BH_UHX2R_CLAIM_ROUNDS", "4"))
    racers = int(os.environ.get("BH_UHX2R_CLAIM_RACERS", "4"))
    with cf.composed_world(tmp_path, FRAMES) as w:
        p, f = w["p"], w["f"]
        _take(w, "p")
        token = "f-" + os.urandom(8).hex()
        latency: dict[str, list[float]] = {"rpc_create": [], "forward_create": []}
        direct = cf.Forwarder(f, p)
        with RpcService(p, {token: "f"}) as svc:
            for i in range(3):
                t0 = time.monotonic()
                code, body = _call(
                    svc.url, token, {"verb": "create", "actor": "w0", "title": f"e6-rpc-{i}"}
                )
                latency["rpc_create"].append(round(time.monotonic() - t0, 3))
                assert code == 200 and body["rc"] == 0, body
                t0 = time.monotonic()
                assert direct.bd("create", "--title", f"e6-fwd-{i}", "-t", "task").returncode == 0
                latency["forward_create"].append(round(time.monotonic() - t0, 3))
            refusals = {
                "no token": _call(svc.url, "nope", {"verb": "create", "actor": "w0"})[0],
                "sql verb": _call(
                    svc.url,
                    token,
                    {"verb": "sql", "actor": "w0", "query": "SET GLOBAL read_only = 1"},
                )[0],
                "id injection": _call(
                    svc.url, token, {"verb": "close", "actor": "w0", "id": "fx-1; --force"}
                )[0],
            }
            winners = []
            for n in range(rounds):
                code, body = _call(
                    svc.url, token, {"verb": "create", "actor": "w0", "title": f"e6-race-{n}"}
                )
                bead = json.loads(body["out"])["id"]
                gate = threading.Barrier(racers)
                codes: dict[str, int] = {}

                def claim(actor: str, bead: str = bead, gate=gate, codes=codes) -> None:
                    gate.wait()
                    codes[actor] = _call(
                        svc.url, token, {"verb": "claim", "actor": actor, "id": bead}
                    )[1]["rc"]

                threads = [threading.Thread(target=claim, args=(f"w{i}",)) for i in range(racers)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
                row = json.loads(p.bd("show", bead, "--json").stdout)[0]
                won = [a for a, rc in codes.items() if rc == 0]
                winners.append(len(won))
                assert (
                    won and row["assignee"] == f"f/{won[0]}" and cf.claim_won(row, f"f/{won[0]}")
                ), (
                    codes,
                    row,
                )
                closed = _call(svc.url, token, {"verb": "close", "actor": won[0], "id": bead})
                assert closed[1]["rc"] == 0, closed
            assert cf.managed_push(p) == "pushed"
            # Demoted primary: the service fails closed through the same guard.
            _take(w, "q")
            cf.divert(p)
            code, body = _call(
                svc.url, token, {"verb": "create", "actor": "w0", "title": "e6-after-demotion"}
            )
            demoted = {
                "http": code,
                "rc": body.get("rc"),
                "guard": wg.GUARD_REFUSAL in body.get("err", "") + body.get("out", ""),
                "tail": (body.get("err", "") + body.get("out", "")).strip()[-160:],
            }
        _evidence(
            "e6",
            {
                "winners_per_round": winners,
                "refusals": refusals,
                "demoted": demoted,
                "latency_p50": {k: statistics.median(v) for k, v in latency.items()},
                "latency": latency,
            },
        )
        assert winners == [1] * rounds
        assert refusals == {"no token": 401, "sql verb": 403, "id injection": 400}
        assert demoted["rc"] != 0 and demoted["guard"]
        assert cf.check_invariants(w)["ok"]
