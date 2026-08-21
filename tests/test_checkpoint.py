"""Contract tests for command-coupled measured-fact replication."""

from __future__ import annotations

import json
from collections import namedtuple
from pathlib import Path

import pytest
from typer.testing import CliRunner

from beadhive import checkpoint, cli

_CP = namedtuple("CP", "returncode stdout stderr")
_FACT = {"measured_at": "2026-08-21T01:00:00Z", "tag_sha": "abc123"}


class FakeBd:
    def __init__(self):
        self.records = {
            "bh-control": {"id": "bh-control", "status": "open", "metadata": {}},
            "bh-wisp-step": {
                "id": "bh-wisp-step",
                "status": "open",
                "ephemeral": True,
                "metadata": {},
            },
        }
        self.calls: list[list[str]] = []
        self.update_rc = 0
        self.close_rc = 0
        self.dep_tree = "bh-control: control [READY]\n"

    def show(self, bead, _main):
        record = self.records.get(bead)
        return dict(record) if record else None

    def run(self, args, _main, **_kwargs):
        args = list(args)
        self.calls.append(args)
        if args[:2] == ["dep", "tree"]:
            return _CP(0, self.dep_tree, "")
        if args[:1] == ["update"]:
            if self.update_rc:
                return _CP(self.update_rc, "", "update failed")
            payload = json.loads(args[args.index("--metadata") + 1])
            self.records[args[1]]["metadata"].update(payload)
            return _CP(0, "", "")
        if args[:1] == ["close"]:
            if self.close_rc:
                return _CP(self.close_rc, "", "close failed")
            self.records[args[1]]["status"] = "closed"
            return _CP(0, "", "")
        return _CP(1, "", f"unexpected: {args}")


@pytest.fixture
def store(monkeypatch):
    fake = FakeBd()
    monkeypatch.setattr(checkpoint.bd, "show", fake.show)
    monkeypatch.setattr(checkpoint.bd, "run", fake.run)
    return fake


def _command(monkeypatch, rc=0, *, mutate=None):
    calls = []

    def fake_run(args, **kwargs):
        calls.append((list(args), kwargs))
        if mutate:
            mutate()
        return _CP(rc, "", "")

    monkeypatch.setattr(checkpoint, "run", fake_run)
    return calls


def test_failing_command_leaves_metadata_and_step_untouched(store, monkeypatch):
    calls = _command(monkeypatch, rc=17)

    rc = checkpoint.execute(
        Path("/hive"),
        "bh-control",
        "release.bump",
        _FACT,
        ["bh", "release", "attest"],
        step_id="bh-wisp-step",
    )

    assert rc == 17
    assert calls[0][0] == ["bh", "release", "attest"]
    assert store.records["bh-control"]["metadata"] == {}
    assert store.records["bh-wisp-step"]["status"] == "open"
    assert not any(call[0] in {"update", "close"} for call in store.calls)


def test_success_records_timestamped_fact_then_closes_step(store, monkeypatch):
    _command(monkeypatch)

    rc = checkpoint.execute(
        Path("/hive"),
        "bh-control",
        "release.bump",
        _FACT,
        ["real-command"],
        step_id="bh-wisp-step",
    )

    assert rc == 0
    assert store.records["bh-control"]["metadata"] == {"release.bump": _FACT}
    assert store.records["bh-wisp-step"]["status"] == "closed"
    mutations = [call[0] for call in store.calls if call[0] in {"update", "close"}]
    assert mutations == ["update", "close"]


def test_existing_checkpoint_key_refuses_before_running_command(store, monkeypatch):
    store.records["bh-control"]["metadata"]["release.bump"] = {"old": "irrecoverable"}
    calls = _command(monkeypatch)

    with pytest.raises(checkpoint.CheckpointError, match="already exists"):
        checkpoint.execute(Path("/hive"), "bh-control", "release.bump", _FACT, ["real-command"])

    assert calls == []
    assert store.records["bh-control"]["metadata"]["release.bump"] == {"old": "irrecoverable"}


def test_key_claimed_while_command_runs_is_not_overwritten(store, monkeypatch):
    def concurrent_write():
        store.records["bh-control"]["metadata"]["release.bump"] = {"by": "other actor"}

    _command(monkeypatch, mutate=concurrent_write)

    with pytest.raises(checkpoint.CheckpointError, match="already exists"):
        checkpoint.execute(Path("/hive"), "bh-control", "release.bump", _FACT, ["real-command"])

    assert store.records["bh-control"]["metadata"]["release.bump"] == {"by": "other actor"}
    assert not any(call[0] == "update" for call in store.calls)


def test_metadata_failure_never_closes_step(store, monkeypatch):
    store.update_rc = 1
    _command(monkeypatch)

    with pytest.raises(checkpoint.CheckpointError, match="could not record"):
        checkpoint.execute(
            Path("/hive"),
            "bh-control",
            "release.bump",
            _FACT,
            ["real-command"],
            step_id="bh-wisp-step",
        )

    assert store.records["bh-wisp-step"]["status"] == "open"
    assert not any(call[0] == "close" for call in store.calls)


def test_dependency_tree_is_byte_identical_before_and_after_write(store, monkeypatch):
    _command(monkeypatch)
    before = store.run(["dep", "tree", "bh-control"], Path("/hive")).stdout

    checkpoint.execute(Path("/hive"), "bh-control", "release.bump", _FACT, ["real-command"])

    after = store.run(["dep", "tree", "bh-control"], Path("/hive")).stdout
    assert after == before
    assert not any(call[0] == "dep" and call[1] != "tree" for call in store.calls)


@pytest.mark.parametrize(
    "raw, message",
    [
        ('{"tag_sha":"abc"}', "measured_at"),
        ('{"measured_at":"2026-08-21T01:00:00"}', "timezone"),
        ('{"measured_at":"2026-08-21T01:00:00Z"}', "concrete fact"),
        ('["not", "an", "object"]', "JSON object"),
    ],
)
def test_measurement_requires_timestamp_and_concrete_fact(raw, message):
    with pytest.raises(checkpoint.CheckpointError, match=message):
        checkpoint.parse_measurement(raw)


def test_cli_forwards_command_argv_after_separator(monkeypatch):
    seen = {}

    monkeypatch.setattr(checkpoint.config, "load", lambda: {})
    monkeypatch.setattr(checkpoint.registry, "hive_dir_for", lambda _cfg, _hive: Path("/hive"))

    def fake_execute(main, bead, key, measurement, command, *, step_id=""):
        seen.update(
            main=main,
            bead=bead,
            key=key,
            measurement=measurement,
            command=command,
            step_id=step_id,
        )
        return 0

    monkeypatch.setattr(checkpoint, "execute", fake_execute)
    result = CliRunner().invoke(
        cli.app,
        [
            "checkpoint",
            "run",
            "bh-control",
            "release.bump",
            "--value",
            json.dumps(_FACT),
            "--step",
            "bh-wisp-step",
            "--",
            "bh",
            "release",
            "attest",
            "--if-needed",
        ],
    )

    assert result.exit_code == 0
    assert seen == {
        "main": Path("/hive"),
        "bead": "bh-control",
        "key": "release.bump",
        "measurement": _FACT,
        "command": ["bh", "release", "attest", "--if-needed"],
        "step_id": "bh-wisp-step",
    }
