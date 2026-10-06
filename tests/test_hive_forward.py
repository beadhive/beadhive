"""Unit tests for the option A forward write path (bh-g7dlo): fakes only, no Dolt.

The Dolt-backed evidence (TLS, real grants, bd verbs, claim races, the in-flight kill) is
``tests/test_hive_forward_int.py``."""

from __future__ import annotations

import json
import os
import re

import pytest

from beadhive import hive_forward as hf
from beadhive.modules.config.contracts import BeadhiveConfig, HostForwardConfig

DB = "fx"
TABLES = [
    "issues",
    "labels",
    "child_counters",
    "bh_writer",
    "bh_epoch_live",
    "bh_write_mark",
    "bh_local_ident",
]
FWD = hf.Account("fwd-e1", "10.0.0.21")


class FakeServer:
    """Just enough of a hive server's grant tables, processlist and fence rows."""

    def __init__(self, tables=TABLES):
        self.tables = list(tables)
        self.users: dict[hf.Account, str] = {hf.Account("root", "localhost"): ""}
        self.grants: dict[hf.Account, dict[str, set[str]]] = {}
        self.processes: list[tuple[int, str, str]] = []
        self.globals = dict(hf.WATCHED_GLOBALS)
        self.ident: str | None = "p"
        self.writer: tuple[str, int] | None = ("p", 3)
        self.log: list[str] = []

    # -- the Sql port --------------------------------------------------------------------
    def query(self, sql: str) -> list[dict]:
        self.log.append(sql)
        if sql.startswith("SELECT user, host, ssl_type FROM mysql.user"):
            return [{"User": a.user, "Host": a.host, "ssl_type": t} for a, t in self.users.items()]
        if sql.startswith("SHOW GRANTS FOR"):
            account = _account(sql.removeprefix("SHOW GRANTS FOR "))
            lines = [f"GRANT USAGE ON *.* TO `{account.user}`@`{account.host}`"]
            for target, privs in sorted(self.grants.get(account, {}).items()):
                if not privs:
                    continue
                db, _, tbl = target.partition(".")
                shown = "*" if db == "*" else f"`{db}`"
                shown += ".*" if tbl == "*" else f".`{tbl}`"
                order = sorted(privs - {"GRANT OPTION"})
                line = f"GRANT {', '.join(order)} ON {shown} TO `{account.user}`@`{account.host}`"
                if "GRANT OPTION" in privs:
                    line += " WITH GRANT OPTION"
                lines.append(line)
            return [{"Grants": line} for line in lines]
        if sql.startswith("SHOW FULL TABLES"):
            return [{f"Tables_in_{DB}": t, "Table_type": "BASE TABLE"} for t in self.tables]
        if sql.startswith("SELECT id, user, host FROM information_schema.processlist"):
            return [{"ID": i, "USER": u, "HOST": h} for i, u, h in self.processes]
        if sql.startswith("SELECT @@GLOBAL."):
            name = sql.split("@@GLOBAL.")[1].split()[0]
            if name not in self.globals:
                raise RuntimeError(f"Unknown system variable '{name}'")
            return [{"v": self.globals[name]}]
        if sql.startswith("SELECT frame FROM bh_local_ident"):
            return [] if self.ident is None else [{"frame": self.ident}]
        if sql.startswith("SELECT frame, epoch FROM bh_writer"):
            return (
                [] if self.writer is None else [{"frame": self.writer[0], "epoch": self.writer[1]}]
            )
        raise AssertionError(f"unexpected query {sql!r}")

    def execute(self, statements) -> None:
        for sql in statements:
            self.log.append(sql)
            self._one(sql)

    def _one(self, sql: str) -> None:
        if m := re.match(
            r"CREATE USER (\S+) IDENTIFIED WITH mysql_native_password "
            r"AS '\*[0-9A-F]{40}'( REQUIRE SSL)?$",
            sql,
        ):
            self.users[_account(m.group(1))] = "ANY" if m.group(2) else ""
        elif m := re.match(r"ALTER USER (\S+) REQUIRE SSL$", sql):
            self.users[_account(m.group(1))] = "ANY"
        elif re.match(r"ALTER USER \S+ IDENTIFIED WITH", sql):
            pass
        elif m := re.match(r"GRANT (.+) ON `(\w+)`\.`(\w+)` TO (\S+)$", sql):
            privs = {p.strip() for p in m.group(1).split(",")}
            target = f"{m.group(2)}.{m.group(3)}"
            self.grants.setdefault(_account(m.group(4)), {}).setdefault(target, set()).update(privs)
        elif m := re.match(r"REVOKE (.+) ON (\S+) FROM (\S+)$", sql):
            account = _account(m.group(3))
            target = m.group(2).replace("`", "")
            held = self.grants.get(account, {}).get(target, set())
            for p in (x.strip() for x in m.group(1).split(",")):
                if p == "ALL PRIVILEGES":
                    held.clear()
                else:
                    held.discard(p)
        elif m := re.match(r"KILL (\d+)$", sql):
            self.processes = [p for p in self.processes if p[0] != int(m.group(1))]
        elif m := re.match(r"DROP USER (\S+)$", sql):
            self.users.pop(_account(m.group(1)), None)
            self.grants.pop(_account(m.group(1)), None)
        else:
            raise AssertionError(f"unexpected statement {sql!r}")

    def grant(self, account: hf.Account, target: str, *privs: str, ssl: str = "ANY") -> None:
        self.users.setdefault(account, ssl)
        self.grants.setdefault(account, {}).setdefault(target, set()).update(privs)


def _account(text: str) -> hf.Account:
    return hf.Account.parse(text)


# ---- accounts -----------------------------------------------------------------------------


def test_account_parse_and_sql_quote_forms():
    for text in ("'fwd'@'10.0.0.2'", "`fwd`@`10.0.0.2`", "fwd@10.0.0.2"):
        assert hf.Account.parse(text) == hf.Account("fwd", "10.0.0.2")
    assert hf.Account("fwd", "10.0.0.2").sql == "'fwd'@'10.0.0.2'"
    with pytest.raises(ValueError):
        hf.Account.parse("no-host")


@pytest.mark.parametrize(
    ("account", "why"),
    [
        (hf.Account("root", "10.0.0.2"), "shared, root or operator"),
        (hf.Account("watchdog", "10.0.0.2"), "shared, root or operator"),
        (hf.Account("__dolt_local_user__", "localhost"), "shared, root or operator"),
        (hf.Account("fwd", "%"), "not pinned"),
        (hf.Account("fwd", "10.0.0.%"), "not pinned"),
        (hf.Account("fwd", "host_1"), "not pinned"),
        (hf.Account("fwd", ""), "not pinned"),
        (hf.Account("fwd;drop", "10.0.0.2"), "not a plain login"),
    ],
)
def test_shared_root_and_unpinned_accounts_are_refused(account, why):
    assert any(why in p for p in hf.account_problems(account))


def test_a_per_frame_pinned_account_passes():
    assert hf.account_problems(FWD) == []


def test_native_password_hash_matches_mysql():
    # SELECT PASSWORD('pw') on MySQL 5.7: *D821809F681A40A6E379B50D0463EFAE20BDD122
    assert hf.native_password_hash("pw") == "*D821809F681A40A6E379B50D0463EFAE20BDD122"
    with pytest.raises(ValueError):
        hf.native_password_hash("")


# ---- the grant shape ----------------------------------------------------------------------


def test_grant_plan_is_table_scoped_with_read_only_fence():
    plan = hf.grant_plan(
        ["issues", "bh_writer", "bh_epoch_live", "bh_local_ident", "bh_write_mark"]
    )
    assert plan["issues"] == hf.DML
    assert plan["dolt_ignore"] == hf.DML
    for table in ("bh_writer", "bh_epoch_live", "bh_local_ident"):
        assert plan[table] == {"SELECT"}
    assert plan["bh_write_mark"] == {"SELECT", "INSERT"}
    # An unknown future bh_* table is fence: read-only by default.
    assert hf.grant_plan(["bh_future"])["bh_future"] == {"SELECT"}
    with pytest.raises(hf.ForwardError):
        hf.grant_plan(["bad`name"])


def _g(privs: str, target: str, option: bool = False) -> str:
    return f"GRANT {privs} ON {target} TO `fwd`@`10.0.0.2`" + (
        " WITH GRANT OPTION" if option else ""
    )


@pytest.mark.parametrize(
    ("line", "finding"),
    [
        (_g("ALL PRIVILEGES", "`fx`.*"), "holds ALL"),
        (_g("SELECT, INSERT, UPDATE, DELETE", "`fx`.*"), "database-wide"),
        (_g("SELECT", "*.*"), "server-wide"),
        (_g("SELECT", "`other`.`issues`"), "another database"),
        (_g("UPDATE", "`fx`.`bh_local_ident`"), "beyond ['SELECT']"),
        (_g("UPDATE", "`fx`.`bh_write_mark`"), "beyond ['INSERT', 'SELECT']"),
        (_g("DELETE", "`fx`.`bh_write_mark`"), "beyond"),
        (_g("INSERT", "`fx`.`bh_writer`"), "beyond"),
        (_g("SELECT", "`fx`.`issues`", option=True), "grant option"),
        (_g("ALTER, DROP", "`fx`.`issues`"), "beyond"),
        (_g("EXECUTE", "PROCEDURE `fx`.`dolt_commit`"), "beyond"),
        ("GRANT `somerole` TO `fwd`@`10.0.0.2`", "unrecognised"),
    ],
)
def test_wider_than_the_shape_is_a_finding(line, finding):
    problems = hf.check_forwarder_grants([line], database=DB)
    assert problems and finding in problems[0], problems


def test_the_narrow_shape_is_clean():
    lines = [
        "GRANT USAGE ON *.* TO `fwd`@`10.0.0.2`",
        _g("SELECT, INSERT, UPDATE, DELETE", "`fx`.`issues`"),
        _g("SELECT, INSERT, UPDATE, DELETE", "`fx`.`dolt_ignore`"),
        _g("SELECT", "`fx`.`bh_writer`"),
        _g("SELECT", "`fx`.`bh_local_ident`"),
        _g("SELECT, INSERT", "`fx`.`bh_write_mark`"),
        _g("SELECT", "`fx`.`issues`"),  # narrower than allowed is fine
    ]
    assert hf.check_forwarder_grants(lines, database=DB) == []


# ---- conformance and provisioning ---------------------------------------------------------


def test_conformance_flags_every_non_operator_account():
    s = FakeServer()
    s.grant(hf.Account("wide", "10.0.0.3"), "fx.*", "SELECT", "INSERT", "UPDATE", "DELETE")
    s.grant(hf.Account("plain", "10.0.0.4"), "fx.issues", "SELECT", ssl="")
    s.grant(hf.Account("any", "%"), "fx.issues", "SELECT")
    s.grant(hf.Account("twice", "10.0.0.5"), "fx.issues", "SELECT")
    s.grant(hf.Account("twice", "10.0.0.6"), "fx.issues", "SELECT")
    s.grant(hf.Account("watchdog", "127.0.0.1"), "*.*", "SELECT")  # an operator: not checked
    problems = hf.conformance(s, database=DB)
    text = "\n".join(problems)
    assert "'wide'@'10.0.0.3': holds" in text and "database-wide" in text
    assert "'plain'@'10.0.0.4': does not REQUIRE SSL" in text
    assert "'any'@'%': host '%' is not pinned" in text
    assert "principal 'twice' is shared by 2 hosts" in text
    assert "watchdog" not in text
    assert "root" not in text
    assert not any(
        "plain" in p for p in hf.conformance(s, database=DB, require_tls=False) if "SSL" in p
    )


def test_provision_creates_a_tls_account_with_the_narrow_shape():
    s = FakeServer()
    report = hf.provision(s, database=DB, account=FWD, password="s3cret")
    assert report.created and not report.revoked
    assert s.users[FWD] == "ANY"
    held = s.grants[FWD]
    assert held["fx.issues"] == hf.DML and held["fx.child_counters"] == hf.DML
    assert held["fx.dolt_ignore"] == hf.DML
    assert held["fx.bh_writer"] == {"SELECT"} and held["fx.bh_epoch_live"] == {"SELECT"}
    assert held["fx.bh_write_mark"] == {"SELECT", "INSERT"}
    assert not any(t.endswith(".*") for t in held)
    # The password crosses only as its native hash; plaintext never reaches a statement.
    assert not any("s3cret" in line for line in s.log)
    assert hf.conformance(s, database=DB) == []


def test_provision_is_idempotent_and_regrants_new_tables():
    s = FakeServer()
    hf.provision(s, database=DB, account=FWD, password="pw")
    again = hf.provision(s, database=DB, account=FWD)
    assert not again.created and not again.granted and not again.revoked
    s.tables.append("issue_snapshots")  # bd added a table
    third = hf.provision(s, database=DB, account=FWD)
    assert third.granted == (
        "GRANT DELETE, INSERT, SELECT, UPDATE ON `fx`.`issue_snapshots` TO 'fwd-e1'@'10.0.0.21'",
    )


def test_provision_narrows_a_wider_existing_account():
    s = FakeServer()
    s.grant(FWD, "fx.*", "SELECT", "INSERT", "UPDATE", "DELETE", ssl="")
    s.grant(FWD, "fx.bh_local_ident", "SELECT", "UPDATE")
    s.grant(FWD, "fx.issues", "SELECT", "GRANT OPTION")
    report = hf.provision(s, database=DB, account=FWD)
    assert not report.created
    assert any("ON `fx`.* FROM" in r for r in report.revoked)
    assert any("REVOKE UPDATE ON `fx`.`bh_local_ident`" in r for r in report.revoked)
    assert any("REVOKE GRANT OPTION" in r for r in report.revoked)
    assert s.users[FWD] == "ANY"
    assert hf.conformance(s, database=DB) == []


@pytest.mark.parametrize(
    "account", [hf.Account("root", "10.0.0.2"), hf.Account("fwd", "%"), hf.Account("watchdog", "h")]
)
def test_provision_refuses_shared_root_and_unpinned_logins(account):
    s = FakeServer()
    with pytest.raises(hf.ForwardRefused):
        hf.provision(s, database=DB, account=account, password="pw")
    assert account not in s.grants


def test_provision_refuses_a_principal_already_pinned_elsewhere():
    s = FakeServer()
    hf.provision(s, database=DB, account=FWD, password="pw")
    with pytest.raises(hf.ForwardRefused, match="never shared"):
        hf.provision(s, database=DB, account=hf.Account(FWD.user, "10.0.0.99"), password="pw")


def test_provision_refuses_when_the_result_is_still_wider():
    class Stubborn(FakeServer):
        def _one(self, sql):  # a server that ignores REVOKE
            if not sql.startswith("REVOKE"):
                super()._one(sql)

    s = Stubborn()
    s.grant(FWD, "fx.*", "ALL PRIVILEGES")
    with pytest.raises(hf.GrantShapeViolation) as caught:
        hf.provision(s, database=DB, account=FWD)
    assert any("holds ALL" in p for p in caught.value.problems)


def test_new_account_needs_a_password():
    with pytest.raises(hf.ForwardError, match="needs a password"):
        hf.provision(FakeServer(), database=DB, account=FWD)


def test_revoke_kills_sessions_then_drops_and_never_drops_operators():
    s = FakeServer()
    hf.provision(s, database=DB, account=FWD, password="pw")
    s.processes = [(7, FWD.user, "10.0.0.21:5555"), (8, "root", "localhost")]
    assert hf.revoke(s, account=FWD)
    assert FWD not in s.users and s.processes == [(8, "root", "localhost")]
    assert not hf.revoke(s, account=FWD)
    with pytest.raises(hf.ForwardRefused):
        hf.revoke(s, account=hf.Account("root", "localhost"))


# ---- sessions: quiesce before the divert reset ----------------------------------------------


def test_quiesce_kills_every_non_operator_session_only():
    s = FakeServer()
    s.processes = [
        (1, "root", "localhost"),
        (2, "fwd-e1", "10.0.0.21"),
        (3, "fwd-e2", "10.0.0.22"),
        (4, "watchdog", "127.0.0.1"),
        (5, "event_scheduler", "localhost"),
        (6, "beads", "localhost"),
    ]
    killed = hf.quiesce(s, operators=["root", "watchdog", "beads"])
    assert sorted(k.id for k in killed) == [2, 3]
    assert [p[0] for p in s.processes] == [1, 4, 5, 6]


def test_quiesce_before_reset_spares_bds_own_login_and_honours_opt_out(monkeypatch):
    s = FakeServer()
    s.processes = [(2, "fwd", "h"), (3, "beads", "localhost")]
    settings = {"quiesce_before_reset": True, "operators": ["root"]}
    killed = hf.quiesce_before_reset(s, login="beads", settings=settings)
    assert [k.id for k in killed] == [2] and s.processes == [(3, "beads", "localhost")]

    s.processes = [(2, "fwd", "h")]
    assert hf.quiesce_before_reset(s, settings={**settings, "quiesce_before_reset": False}) == []
    monkeypatch.setenv(hf.QUIESCE_ENV, "off")
    assert hf.quiesce_before_reset(s, settings=settings) == []
    assert s.processes == [(2, "fwd", "h")]


def test_quiesce_before_reset_never_raises():
    class Broken:
        def query(self, sql):
            raise RuntimeError("server gone")

    assert hf.quiesce_before_reset(Broken(), settings={"operators": []}) == []


# ---- globals, root, primary report ---------------------------------------------------------


def test_check_globals_reads_and_never_sets():
    s = FakeServer()
    s.globals["read_only"] = "ON"
    s.globals.pop("max_connections")
    results = {r.name: r.status for r in hf.check_globals(s)}
    assert results == {
        "dolt_force_transaction_commit": "ok",
        "dolt_transaction_commit": "ok",
        "read_only": "drift",
        "max_connections": "unverifiable",
    }
    assert not any(line.upper().startswith("SET") for line in s.log)


def test_watched_globals_are_configurable():
    w = hf.watched_globals({"max_connections": "200"}, ["read_only"])
    assert w["max_connections"] == "200" and "read_only" not in w
    assert hf.watched_globals() == hf.WATCHED_GLOBALS
    assert "dolt_allow_commit_conflicts" not in hf.WATCHED_GLOBALS


def test_root_problems(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root bypasses file modes")
    assert hf.root_problems(tmp_path / "missing")
    dot = tmp_path / ".dolt"
    dot.mkdir()
    (dot / "config_global.json").write_text("{}")
    assert hf.root_problems(tmp_path)
    (dot / "config_global.json").chmod(0o444)
    dot.chmod(0o555)
    try:
        assert hf.root_problems(tmp_path) == []
    finally:
        dot.chmod(0o755)


def test_primary_report_collects_every_finding():
    s = FakeServer()
    s.globals["dolt_force_transaction_commit"] = "1"
    s.grant(hf.Account("wide", "10.0.0.3"), "fx.*", "ALL PRIVILEGES")
    settings = hf.serve_settings({})
    report = hf.primary_report(s, database=DB, settings=settings)
    text = "\n".join(report["findings"])
    assert "dolt_force_transaction_commit drift" in text
    assert "DOLT_ROOT_PATH not checked" in text
    assert "forwarder grants: 'wide'@'10.0.0.3': holds ALL" in text
    clean = FakeServer()
    ok = hf.primary_report(clean, database=DB, settings={**settings, "root_path": ""})
    assert ok["findings"] == [
        "DOLT_ROOT_PATH not checked: set host.forward.serve.root_path to the server's root"
    ]


# ---- the forwarder's side -------------------------------------------------------------------

EP = {
    "host": "10.0.0.10",
    "port": 3307,
    "database": DB,
    "user": "fwd-e1",
    "tls_mode": "required",
    "server_name": "10.0.0.10",
    "ca_file": "/etc/beadhive/forward/ca.pem",
    "credential": {"config_path": "/etc/fnox.toml", "profile": "fleet", "key": "FWD_P"},
}


def test_resolve_target():
    assert hf.resolve_target(("me", 3), {"p": EP}, self_frame="me") is None
    target = hf.resolve_target(("p", 3), {"p": EP}, self_frame="me")
    assert (target.frame, target.epoch, target.endpoint["host"]) == ("p", 3, "10.0.0.10")
    with pytest.raises(hf.ForwardRefused, match="no placement"):
        hf.resolve_target(None, {"p": EP}, self_frame="me")
    with pytest.raises(hf.ForwardRefused, match="no hive server"):
        hf.resolve_target(("q", 4), {"p": EP}, self_frame="me")


@pytest.mark.parametrize(
    ("ident", "writer", "why"),
    [
        ("p", ("q", 4), "demoted primary"),
        ("q", ("q", 4), "identifies as q"),
        (None, ("p", 3), "identifies as <no bh_local_ident>"),
        ("p", None, "not cut over"),
        ("p", ("p", 2), "adopt incomplete"),
        ("p", ("p", 5), "ahead of the cached placement"),
    ],
)
def test_preflight_fails_closed(ident, writer, why):
    s = FakeServer()
    s.ident, s.writer = ident, writer
    with pytest.raises(hf.ForwardRefused, match=why):
        hf.preflight(s, hf.ForwardTarget("p", 3, EP))


def test_preflight_passes_on_the_writer():
    assert hf.preflight(FakeServer(), hf.ForwardTarget("p", 3, EP)) == ("p", 3)


def _hive(tmp_path):
    """A hive checkout with a git dir and a tracked-style bd metadata.json."""
    hive = tmp_path / "hive"
    (hive / ".git").mkdir(parents=True)
    (hive / ".beads").mkdir()
    meta = {"database": "dolt", "backend": "dolt", "dolt_mode": "server", "dolt_database": DB}
    (hive / ".beads" / "metadata.json").write_text(json.dumps(meta))
    return hive


def test_marker_is_git_private_and_shared_by_worktrees(tmp_path):
    hive = _hive(tmp_path)
    assert hf.marker_path(hive) == hive / ".git" / "bh" / "forward.json"
    # A linked worktree: .git is a file pointing at .git/worktrees/<name>, whose commondir
    # leads back to the main repository's git dir.
    wt_git = hive / ".git" / "worktrees" / "w1"
    wt_git.mkdir(parents=True)
    (wt_git / "commondir").write_text("../..\n")
    worktree = tmp_path / "worktrees" / "w1"
    (worktree / "src").mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {wt_git}\n")
    assert hf.marker_path(worktree / "src") == (hive / ".git" / "bh" / "forward.json").resolve()
    plain = tmp_path / "plain"
    plain.mkdir()
    assert hf.marker_path(plain) == plain / ".beads" / hf.MARKER


def test_point_refuse_stop_round_trip_never_touches_tracked_metadata(tmp_path):
    hive = _hive(tmp_path)
    before = (hive / ".beads" / "metadata.json").read_bytes()
    marker = hf.point(
        hive, hf.ForwardTarget("p", 3, EP), prefix=DB, self_frame="me", endpoints={"p": EP}
    )
    assert hf.read_marker(hive) == marker and marker.state == "forwarding"
    assert hf.marker_path(hive).stat().st_mode & 0o777 == 0o600
    assert "s3cret" not in hf.marker_path(hive).read_text()  # only the fnox reference

    hf.refuse(hive, "demoted", prefix=DB, self_frame="me", endpoints={"p": EP}, placement=("p", 3))
    with pytest.raises(hf.ForwardRefused, match="demoted"):
        hf.bd_env(hive)

    assert hf.stop(hive)
    assert hf.read_marker(hive) is None and not hf.stop(hive)
    assert (hive / ".beads" / "metadata.json").read_bytes() == before


def test_bd_env_points_bd_at_the_target_only_for_a_forwarded_checkout(tmp_path, monkeypatch):
    hive = _hive(tmp_path)
    assert hf.bd_env(hive) is None
    hf.point(hive, hf.ForwardTarget("p", 3, EP), prefix=DB, self_frame="me", endpoints={"p": EP})
    monkeypatch.setenv(hf.PASSWORD_ENV, "pw-from-env")
    env = hf.bd_env(hive, base={"PATH": "/bin", "BEADS_DOLT_SHARED_SERVER": "1"})
    assert env == {
        "PATH": "/bin",
        "BEADS_DOLT_SERVER_MODE": "1",
        "BEADS_DOLT_SHARED_SERVER": "0",
        "BEADS_DOLT_AUTO_START": "0",
        "BEADS_DOLT_SERVER_HOST": "10.0.0.10",
        "BEADS_DOLT_SERVER_PORT": "3307",
        "BEADS_DOLT_SERVER_USER": "fwd-e1",
        "BEADS_DOLT_SERVER_TLS": "1",
        "BEADS_DOLT_SERVER_DATABASE": DB,
        "BEADS_DOLT_PASSWORD": "pw-from-env",
        "SSL_CERT_FILE": "/etc/beadhive/forward/ca.pem",
    }


def test_ensure_current_repoints_on_placement_change_and_refuses_a_demoted_target(tmp_path):
    hive = _hive(tmp_path)
    ep_q = {**EP, "host": "10.0.0.11", "server_name": "10.0.0.11"}
    endpoints = {"p": EP, "q": ep_q}
    hf.point(hive, hf.ForwardTarget("p", 3, EP), prefix=DB, self_frame="me", endpoints=endpoints)
    servers = {"10.0.0.10": FakeServer(), "10.0.0.11": FakeServer()}
    servers["10.0.0.11"].ident, servers["10.0.0.11"].writer = "q", ("q", 4)
    opened = []

    def open_sql(ep):
        opened.append(ep["host"])
        return servers[ep["host"]]

    placed = {"value": ("p", 3)}

    def placement(_marker):
        return placed["value"]

    # Unchanged placement: no connection at all.
    assert hf.ensure_current(hive, placement=placement, open_sql=open_sql).frame == "p"
    assert opened == []
    # q adopted: re-point to q after its preflight.
    placed["value"] = ("q", 4)
    m = hf.ensure_current(hive, placement=placement, open_sql=open_sql)
    assert (m.state, m.frame, m.epoch, m.endpoint["host"]) == ("forwarding", "q", 4, "10.0.0.11")
    assert opened == ["10.0.0.11"]
    # Placement names p again but p's data says q: p is demoted — fail closed.
    placed["value"] = ("p", 5)
    servers["10.0.0.10"].writer = ("q", 4)
    m = hf.ensure_current(hive, placement=placement, open_sql=open_sql)
    assert m.state == "refused" and "demoted" in m.reason
    with pytest.raises(hf.ForwardRefused):
        hf.bd_env(hive)
    # An unreachable target also fails closed.
    placed["value"] = ("q", 6)

    def unreachable(ep):
        raise OSError("no route to host")

    m = hf.ensure_current(hive, placement=placement, open_sql=unreachable)
    assert m.state == "refused" and "cannot reach" in m.reason

    # An unreadable placement cache keeps the last decision (still refused).
    def broken(_marker):
        raise RuntimeError("no HQ clone")

    assert hf.ensure_current(hive, placement=broken, open_sql=open_sql).state == "refused"
    # This frame placed: stop forwarding.
    placed["value"] = ("me", 7)
    assert hf.ensure_current(hive, placement=placement, open_sql=open_sql) is None
    assert hf.read_marker(hive) is None


# ---- configuration: opt-in, every default configurable ---------------------------------------


def test_forwarding_is_opt_in_per_frame_by_default():
    fwd = BeadhiveConfig().host.forward
    assert fwd.enabled is False and fwd.endpoints == {}
    assert fwd.serve.enabled is False
    assert fwd.serve.quiesce_before_reset is True and fwd.serve.require_tls is True
    assert fwd.serve.operators == ["root", "watchdog"]


def test_forward_config_validates_endpoints_and_serve():
    ok = HostForwardConfig.model_validate({"enabled": True, "endpoints": {"p": EP}})
    assert ok.endpoints["p"].host == "10.0.0.10"
    with pytest.raises(ValueError, match="requires ca_file"):
        HostForwardConfig.model_validate({"endpoints": {"p": {**EP, "ca_file": ""}}})
    with pytest.raises(ValueError):
        HostForwardConfig.model_validate({"serve": {"root_path": "relative"}})
    with pytest.raises(ValueError):
        HostForwardConfig.model_validate({"serve": {"watched": {"x; drop": "1"}}})
    settings = hf.serve_settings({"host": {"forward": {"serve": {"operators": ["root", "bd"]}}}})
    assert settings["operators"] == ["root", "bd"]


def test_residual_trust_text_names_what_grants_cannot_stop():
    for phrase in ("SET GLOBAL", "SET PERSIST", "any bead row", "watchdog", "fence_audit"):
        assert phrase in hf.RESIDUAL_TRUST


# ---- wiring: the server-mode divert reset quiesces forwarders first ---------------------------


def test_server_engine_reset_kills_forwarder_sessions_before_dolt_reset(tmp_path, monkeypatch):
    import subprocess

    from beadhive import fence_data

    hive = _hive(tmp_path)
    monkeypatch.delenv(hf.QUIESCE_ENV, raising=False)
    monkeypatch.setenv("BEADS_DOLT_SERVER_USER", "beads")
    calls: list[str] = []
    processes = [(11, "fwd-e1", "10.0.0.21"), (12, "beads", "localhost"), (13, "root", "localhost")]

    class Recording(fence_data.BdServerEngine):
        def _bd(self, *args):
            statement = args[-1]
            calls.append(statement)
            out = "[]"
            if "information_schema.processlist" in statement:
                out = json.dumps([{"id": i, "user": u, "host": h} for i, u, h in processes])
            return subprocess.CompletedProcess(["bd", *args], 0, out, "")

    engine = Recording(hive)
    assert engine.login() == "beads"
    defaults = hf.serve_settings({})
    monkeypatch.setattr(hf, "serve_settings", lambda cfg=None: defaults)
    engine.reset_to_remote()
    kills = [c for c in calls if c.startswith("KILL")]
    assert kills == ["KILL 11"]  # bd's own login and root are never killed
    assert calls.index("KILL 11") < next(
        i for i, c in enumerate(calls) if c.startswith("CALL DOLT_RESET")
    )
    assert calls[0].startswith("CALL DOLT_FETCH")

    calls.clear()
    monkeypatch.setenv(hf.QUIESCE_ENV, "off")
    engine.reset_to_remote()
    assert not [c for c in calls if c.startswith("KILL") or "processlist" in c]
