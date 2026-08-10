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


def test_enable_all_is_refused_not_silently_ignored():
    result = runner.invoke(app, ["host", "dispatch", "enable", "--all"])
    assert result.exit_code == 1
    assert "--all is not valid" in result.output


def test_disable_all_is_refused_not_silently_ignored():
    result = runner.invoke(app, ["host", "dispatch", "disable", "--all"])
    assert result.exit_code == 1
    assert "--all is not valid" in result.output


def test_status_all_is_a_legitimate_aggregate_read_not_refused(monkeypatch):
    monkeypatch.setattr(host_cli.dispatch_status, "compute_status_all", lambda cfg: [])
    result = runner.invoke(app, ["host", "dispatch", "status", "--all"])
    assert result.exit_code == 0
    assert "not valid" not in result.output


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
