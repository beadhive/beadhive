"""`bh host dispatch enable|disable|status|logs` (bh-e7r9q.5).

The CLI wiring around `dispatch_status`/`dispatch_supervisor`/`dispatch_log` — the operator
surface itself. `_sandbox_bh_home` (tests/conftest.py, autouse) keeps every invocation below
off the operator's real `~/.beadhive`.
"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from beadhive import guard, host_adopt, host_cli
from beadhive.cli import app

runner = CliRunner()


# ---- --all is FORBIDDEN on the per-entity mutations, exactly as the naming ADR requires ----
#
# An accepted-but-ignored flag and a refused flag look identical from the outside until an
# operator runs it across every hive on the fleet — so these assert the REFUSAL explicitly.
#
# The refusal is Typer's OWN "No such option", because `enable`/`disable` DECLARE NO `--all`.
# They used to declare one whose `--help` text read "NOT VALID here", which is worse than not
# offering it: `--help` advertised a flag that always failed, and the advertisement and the
# handler's refusal were two things that could drift apart. Not declaring it makes the refusal
# structural — it happens in argument parsing, before any hive is resolved or any lease touched
# — and keeps `--all` off `--help`, which is where an operator looks to find out it exists.


def test_enable_all_is_refused_not_silently_ignored():
    result = runner.invoke(app, ["host", "dispatch", "enable", "--all"])
    assert result.exit_code != 0
    assert "No such option" in result.output
    assert "--all" in result.output


def test_disable_all_is_refused_not_silently_ignored():
    result = runner.invoke(app, ["host", "dispatch", "disable", "--all"])
    assert result.exit_code != 0
    assert "No such option" in result.output
    assert "--all" in result.output


def test_enable_and_disable_do_not_advertise_all_in_help():
    """The other half: a flag that always fails must not be listed as an option."""
    for verb in ("enable", "disable"):
        result = runner.invoke(app, ["host", "dispatch", verb, "--help"])
        assert result.exit_code == 0
        assert "--all" not in result.output, f"`host dispatch {verb} --help` still offers --all"
    # …while `status`, where it IS a legitimate aggregate read, still does.
    result = runner.invoke(app, ["host", "dispatch", "status", "--help"])
    assert "--all" in result.output


def test_enable_without_all_is_not_refused_by_the_all_guard(monkeypatch):
    """A normal single-hive `enable` must of course still run — proving the refusal is keyed on
    `--all` specifically and not on the verb."""
    monkeypatch.setattr(host_cli, "_dispatch_entry", lambda hive, cfg: ({}, "acme/widgets"))
    monkeypatch.setattr(host_cli, "_ensure_lease_for_enable", lambda hive, cfg: (True, "ok"))
    monkeypatch.setattr(host_cli.dispatch_log, "ensure_sink_dir", lambda: None)
    monkeypatch.setattr(host_cli.dispatch_log, "hive_slug", lambda entry: "slug")

    class _Backend:
        name = "fake"

        def enable(self, slug, exec_argv, env):
            pass

    monkeypatch.setattr(
        host_cli.dispatch_supervisor, "get_supervisor_backend", lambda cfg: _Backend()
    )
    monkeypatch.setattr(
        host_cli.dispatch_status,
        "compute_status",
        lambda hive, cfg, backend: host_cli.dispatch_status.DispatchStatus(
            hive="acme/widgets",
            hive_slug="slug",
            backend="fake",
            installed=True,
            running=True,
            persisted=True,
            lease_in_force=False,
            lease_held=False,
            lease_expires_at="",
            lease_detail="",
            last_pass_at="",
            seats_in_flight=0,
            last_escalation=None,
            state="running",
            detail="",
        ),
    )
    result = runner.invoke(app, ["host", "dispatch", "enable", "--hive", "acme/widgets"])
    assert result.exit_code == 0
    assert "--all is not valid" not in result.output


def test_status_all_is_a_legitimate_aggregate_read_not_refused(monkeypatch):
    monkeypatch.setattr(host_cli.dispatch_status, "compute_status_all", lambda cfg: [])
    result = runner.invoke(app, ["host", "dispatch", "status", "--all"])
    assert result.exit_code == 0
    assert "not valid" not in result.output


def test_status_all_renders_every_hive_this_host_supervises(monkeypatch):
    """`status --all` isn't just un-refused — it must actually work: render every hive's row,
    not silently drop rows or error on a non-empty aggregate."""
    rows = [
        host_cli.dispatch_status.DispatchStatus(
            hive="acme/widgets",
            hive_slug="acme-widgets",
            backend="systemd",
            installed=True,
            running=True,
            persisted=True,
            lease_in_force=True,
            lease_held=True,
            lease_expires_at="2026-08-10T23:00:00Z",
            lease_detail="",
            last_pass_at="2026-08-10T22:55:00Z",
            seats_in_flight=2,
            last_escalation=None,
            state="running-healthy",
            detail="",
        ),
        host_cli.dispatch_status.DispatchStatus(
            hive="acme/other",
            hive_slug="acme-other",
            backend="systemd",
            installed=False,
            running=False,
            persisted=False,
            lease_in_force=False,
            lease_held=False,
            lease_expires_at="",
            lease_detail="",
            last_pass_at="",
            seats_in_flight=0,
            last_escalation=None,
            state="not-enabled",
            detail="",
        ),
    ]
    monkeypatch.setattr(host_cli.dispatch_status, "compute_status_all", lambda cfg: rows)
    result = runner.invoke(app, ["host", "dispatch", "status", "--all"])
    assert result.exit_code == 0
    assert "acme/widgets" in result.output
    assert "acme/other" in result.output
    assert "running-healthy" in result.output
    assert "not-enabled" in result.output


def test_status_all_as_json_emits_every_hive_machine_readable(monkeypatch):
    rows = [
        host_cli.dispatch_status.DispatchStatus(
            hive="acme/widgets",
            hive_slug="acme-widgets",
            backend="systemd",
            installed=True,
            running=True,
            persisted=True,
            lease_in_force=True,
            lease_held=True,
            lease_expires_at="2026-08-10T23:00:00Z",
            lease_detail="",
            last_pass_at="2026-08-10T22:55:00Z",
            seats_in_flight=1,
            last_escalation=None,
            state="running-healthy",
            detail="",
        ),
    ]
    monkeypatch.setattr(host_cli.dispatch_status, "compute_status_all", lambda cfg: rows)
    result = runner.invoke(app, ["host", "dispatch", "status", "--all", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert isinstance(payload["hives"], list) and len(payload["hives"]) == 1
    assert payload["hives"][0]["hive"] == "acme/widgets"


# ---- _ensure_lease_for_enable: adopt-or-refuse-with-the-actionable-command -----------------


def test_ensure_lease_proceeds_when_multi_host_model_not_in_force(monkeypatch):
    monkeypatch.setattr(guard, "primary_state", lambda *a, **k: None)
    ok, msg = host_cli._ensure_lease_for_enable("acme/widgets", {})
    assert ok is True
    assert "single-host default" in msg


def test_ensure_lease_proceeds_when_already_held_by_this_host(monkeypatch):
    class _Lease:
        def held_by(self, host_id, at=None):  # noqa: ARG002
            return True

        def describe(self):
            return "held here"

    monkeypatch.setattr(guard, "primary_state", lambda *a, **k: ("acme", "this-host", _Lease()))
    ok, msg = host_cli._ensure_lease_for_enable("acme/widgets", {})
    assert ok is True
    assert "already held" in msg


def test_ensure_lease_refuses_with_the_actionable_command_when_held_elsewhere(monkeypatch):
    class _Lease:
        is_tombstone = False

        def held_by(self, host_id, at=None):  # noqa: ARG002
            return False

        def is_expired(self, at=None):  # noqa: ARG002
            return False  # unexpired -> genuinely held elsewhere, not just stale

        def describe(self):
            return "someone-else (host-b), epoch 3"

    monkeypatch.setattr(guard, "primary_state", lambda *a, **k: ("acme", "this-host", _Lease()))
    ok, msg = host_cli._ensure_lease_for_enable("acme/widgets", {})
    assert ok is False
    assert "bh host lease adopt acme/widgets --force" in msg  # the actionable next command


def test_ensure_lease_adopts_a_free_lease(monkeypatch):
    class _Lease:
        is_tombstone = True

        def held_by(self, host_id, at=None):  # noqa: ARG002
            return False

        def is_expired(self, at=None):  # noqa: ARG002
            return True

        def describe(self):
            return "released"

    class _Outcome:
        epoch = 1

        class lease:
            expires_at = "2099-01-01T00:00:00Z"

    monkeypatch.setattr(guard, "primary_state", lambda *a, **k: ("acme", "this-host", _Lease()))
    monkeypatch.setattr(host_cli, "adopt_one", lambda hive, force=False: _Outcome())  # noqa: ARG005

    ok, msg = host_cli._ensure_lease_for_enable("acme/widgets", {})

    assert ok is True
    assert "adopted lease" in msg


def test_ensure_lease_refuses_cleanly_when_adopt_raises(monkeypatch):
    class _Lease:
        is_tombstone = True

        def held_by(self, host_id, at=None):  # noqa: ARG002
            return False

        def is_expired(self, at=None):  # noqa: ARG002
            return True

        def describe(self):
            return "released"

    def _raise(hive, force=False):  # noqa: ARG001
        raise host_adopt.AdoptError("no HQ clone on this host")

    monkeypatch.setattr(guard, "primary_state", lambda *a, **k: ("acme", "this-host", _Lease()))
    monkeypatch.setattr(host_cli, "adopt_one", _raise)

    ok, msg = host_cli._ensure_lease_for_enable("acme/widgets", {})

    assert ok is False
    assert "could not adopt" in msg
    assert "bh host lease adopt acme/widgets" in msg


# ---- logs: no records yet reads cleanly, not as an error -----------------------------------


def test_logs_with_no_records_yet_is_not_an_error(monkeypatch, tmp_path):
    monkeypatch.setattr(host_cli.registry, "hive_dir_for", lambda cfg, hive: tmp_path)
    monkeypatch.setattr(
        host_cli.registry,
        "entry_for_dir",
        lambda cfg, cwd: {"provider": "github", "org": "acme", "repo": "widgets"},
    )
    result = runner.invoke(app, ["host", "dispatch", "logs"])
    assert result.exit_code == 0
    assert "no dispatch log records yet" in result.output


def test_logs_json_emits_the_tailed_records(monkeypatch, tmp_path):
    entry = {"provider": "github", "org": "acme", "repo": "widgets"}
    monkeypatch.setattr(host_cli.registry, "hive_dir_for", lambda cfg, hive: tmp_path)
    monkeypatch.setattr(host_cli.registry, "entry_for_dir", lambda cfg, cwd: entry)
    from beadhive import dispatch_log

    sink = dispatch_log.sink_path({}, entry)
    sink.parent.mkdir(parents=True, exist_ok=True)
    sink.write_text(json.dumps({"event": "seat_spawned", "bead": "bh-1"}) + "\n")

    result = runner.invoke(app, ["host", "dispatch", "logs", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["records"][0]["bead"] == "bh-1"
