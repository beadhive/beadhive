"""``bh backfill apply|export --store``: the default is exactly the bd path, ``xtdb`` goes through
the ``beadlog.store`` capability, and an unavailable store fails with an actionable message.

The bd side is the in-memory :class:`~tests.test_backfill_bd.FakeBd` behind the real bd
StoreLoader (as in ``tests/test_backfill_cli.py``). The ``xtdb`` side replaces the
``backfill_cli.beadlog_store`` seam with a fake ``BeadlogStore`` over beadlog's lossless
:class:`~beadlog.testing.InMemoryStore`, except where a test exercises the real composition
root's refusals. Nothing touches a live hive or a node.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from tests.test_backfill_bd import cfg_path as cfg_path  # noqa: F401 — fixture resolved by name
from tests.test_backfill_cli import ENV, STAMP, _invoke, _neutral_plan
from tests.test_backfill_cli import hive as hive  # noqa: F401 — fixture resolved by name
from typer.testing import CliRunner

import beadlog
from beadhive import backfill_cli, cli
from beadhive.bootstrap import beadlog_store as composition
from beadhive.modules.beadlog_store.contracts import BeadlogStore, StoreSnapshot
from beadhive_backfill import Plan
from beadlog.testing import InMemoryStore
from beadlog_sources import reconcile, write_plan


class MemoryLoader(InMemoryStore):
    target = "memory://xtdb-fake"


class FakeStore:
    """A ``BeadlogStore`` over the lossless in-memory reference store."""

    store = "xtdb"

    def __init__(self, loader: MemoryLoader | None = None, *, notes=None) -> None:
        self.memory = loader or MemoryLoader()
        self.notes = notes or {}
        self.closed = 0
        self.snapshots: list[dict[str, str]] = []

    def loader(self) -> MemoryLoader:
        return self.memory

    def snapshot(self, *, group: str, extracted_at: str) -> StoreSnapshot:
        self.snapshots.append({"group": group, "extracted_at": extracted_at})
        events = tuple(e for e in self.memory.export() if e.bead.group == group)
        return StoreSnapshot(self.memory.target, events, self.notes)

    def close(self) -> None:
        self.closed += 1


@pytest.fixture
def followups(monkeypatch) -> list[str]:
    """Records the bd-only follow-ups of an applied plan (cursor and coverage refresh)."""
    seen: list[str] = []
    monkeypatch.setattr(backfill_cli, "_record_cursor", lambda *a: seen.append("cursor"))
    monkeypatch.setattr(
        backfill_cli, "refresh_coverage_after_apply", lambda *a: seen.append("coverage")
    )
    return seen


@pytest.fixture
def fake_store(monkeypatch) -> FakeStore:
    store = FakeStore()
    assert isinstance(store, BeadlogStore)
    requested: list[str] = []

    def seam(name: str) -> FakeStore:
        requested.append(name)
        return store

    monkeypatch.setattr(backfill_cli, "beadlog_store", seam)
    store.requested = requested  # type: ignore[attr-defined]
    return store


def _fresh_plan(group: beadlog.Group, path: Path) -> Plan:
    """A ``beadlog reconcile`` plan of one NEW bead against an EMPTY store."""
    ref = beadlog.BeadRef(group.id, "wd-9")
    proposed = [
        beadlog.mint_event(
            kind,
            ref,
            data,
            occurred_at=at,
            recorded_at=None,
            extracted_at="2026-10-31T00:00:00Z",
            source=("docs", f"docs/decisions/0009.md#{kind}"),
            provenance="inferred",
        )
        for kind, data, at in (
            ("create", {"type": "decision", "fields": {"title": "Adopt BeadLog"}}, STAMP),
            ("label_add", {"label": "origin:backfill"}, "2026-07-01T00:00:01Z"),
        )
    ]
    write_plan(
        path,
        reconcile(proposed, ()),
        groups=[group],
        generator=beadlog.Generator("beadlog", "0.1.0"),
    )
    return Plan.read(path)


def _fails(*args: str) -> Any:
    result = CliRunner().invoke(cli.app, list(args), env=ENV)
    assert result.exit_code == 1, result.output
    return result


# -- the default is exactly today's bd path ---------------------------------------------------


def test_no_store_and_store_bd_are_the_same_bd_path(hive, tmp_path, monkeypatch, followups):  # noqa: F811
    store, group = hive
    monkeypatch.setattr(
        backfill_cli, "beadlog_store", lambda name: pytest.fail("bd never composes a plugin")
    )
    plan_path = tmp_path / "plan.bdlg"
    _neutral_plan(store, group, plan_path)

    plain = _invoke("backfill", "apply", str(plan_path), "--dry-run", "--json").stdout
    flagged = _invoke("backfill", "apply", str(plan_path), "--dry-run", "--json", "--store", "bd")
    assert flagged.stdout == plain
    assert "store" not in json.loads(plain)  # the bd JSON keys are unchanged

    plain_export = json.loads(
        _invoke("backfill", "export", "--out", str(tmp_path / "a.bdlg"), "--json").stdout
    )
    flagged_export = json.loads(
        _invoke(
            "backfill", "export", "--out", str(tmp_path / "b.bdlg"), "--json", "--store", "bd"
        ).stdout
    )
    assert {k: v for k, v in flagged_export.items() if k != "path"} == {
        k: v for k, v in plain_export.items() if k != "path"
    }
    assert "store" not in plain_export
    a_events = beadlog.read_archive(tmp_path / "a.bdlg")[1]
    b_events = beadlog.read_archive(tmp_path / "b.bdlg")[1]
    assert [e.id for e in a_events] == [e.id for e in b_events]

    audit = json.loads(
        _invoke("backfill", "apply", str(plan_path), "--json", "--store", "bd").stdout
    )
    assert audit["state"] == "applied"
    assert store.rows["wd-9"]["title"] == "Adopt BeadLog"
    assert (tmp_path / "plan.pre-state.jsonl").is_file()  # the bd recovery artifact, as before
    assert followups == ["cursor", "coverage"]


# -- --store xtdb through the capability --------------------------------------------------------


def test_apply_store_xtdb_runs_the_envelope_against_the_store(
    hive,  # noqa: F811
    tmp_path,
    fake_store,
    followups,
) -> None:
    bd_store, group = hive
    plan_path = tmp_path / "plan.bdlg"
    plan = _fresh_plan(group, plan_path)

    preview = json.loads(
        _invoke(
            "backfill", "apply", str(plan_path), "--store", "xtdb", "--dry-run", "--json"
        ).stdout
    )
    assert preview["store"] == "xtdb" and preview["target"] == "memory://xtdb-fake"
    assert (preview["plan_hash"], preview["changes"]) == (plan.plan_hash, 1)
    assert preview["loss_report"]["mode"] == "dry-run"
    assert fake_store.memory.export() == ()

    text = _invoke("backfill", "apply", str(plan_path), "--store", "xtdb").stdout
    assert "applied 1 beads to memory://xtdb-fake; verification clean" in text
    folded = beadlog.fold(fake_store.memory.export())
    [state] = folded.values()
    assert state.fields["title"] == "Adopt BeadLog" and "origin:backfill" in state.labels
    audit = json.loads((tmp_path / "plan.audit.json").read_text())
    assert audit["state"] == "applied"
    pre_state = tmp_path / "plan.pre-state.bdlg"
    assert audit["recovery"]["pre_state_artifact"] == str(pre_state)
    assert beadlog.read_archive(pre_state)[1] == ()  # the store was empty before the apply
    assert bd_store.writes == []  # the hive's bd store is never touched
    assert followups == []  # the bd cursor and coverage describe bd, not this store
    assert fake_store.requested == ["xtdb", "xtdb"]
    assert fake_store.closed == 2

    again = _fails("backfill", "apply", str(plan_path), "--store", "xtdb")
    assert "corpus changed after planning" in again.output


def test_export_store_xtdb_writes_the_store_snapshot(hive, tmp_path, fake_store) -> None:  # noqa: F811
    _bd, group = hive
    _fresh_plan(group, tmp_path / "plan.bdlg")
    _invoke("backfill", "apply", str(tmp_path / "plan.bdlg"), "--store", "xtdb")
    fake_store.notes = {"comment.id-not-preserved": 2}
    out = tmp_path / "store.bdlg"

    summary = json.loads(
        _invoke("backfill", "export", "--store", "xtdb", "--out", str(out), "--json").stdout
    )

    assert summary["store"] == {
        "store": "xtdb",
        "target": "memory://xtdb-fake",
        "read_notes": {"comment.id-not-preserved": 2},
    }
    assert summary["group"] == group.to_json()
    assert (summary["events"], summary["beads"]) == (2, 1)
    manifest, events = beadlog.read_archive(out)
    assert manifest.groups == (group,)
    assert manifest.annotations[backfill_cli.STORE_ANNOTATION]["store"] == "xtdb"
    assert {e.id for e in events} == {e.id for e in fake_store.memory.export()}
    assert fake_store.snapshots[-1]["group"] == group.id


def test_export_store_xtdb_defaults_its_own_file_name(hive, tmp_path, fake_store, monkeypatch):  # noqa: F811
    monkeypatch.chdir(tmp_path)
    _bd, group = hive
    _fresh_plan(group, tmp_path / "plan.bdlg")
    _invoke("backfill", "apply", str(tmp_path / "plan.bdlg"), "--store", "xtdb")
    text = _invoke("backfill", "export", "--store", "xtdb").stdout
    assert f"to {group.dir}.xtdb.bdlg" in text
    assert (tmp_path / f"{group.dir}.xtdb.bdlg").is_file()


def test_export_history_is_refused_with_a_store(hive, fake_store) -> None:  # noqa: F811
    result = _fails("backfill", "export", "--store", "xtdb", "--history")
    assert "--history reads the bd events table" in result.output
    assert fake_store.requested == []


# -- an unavailable store: the real composition root's messages --------------------------------


def test_store_xtdb_disabled_by_default_fails_with_the_enable_hint(hive, tmp_path) -> None:  # noqa: F811
    _bd, group = hive
    _fresh_plan(group, tmp_path / "plan.bdlg")
    for args in (
        ("backfill", "apply", str(tmp_path / "plan.bdlg"), "--store", "xtdb"),
        ("backfill", "export", "--store", "xtdb"),
    ):
        result = _fails(*args)
        assert "--store xtdb needs the beadlog-xtdb plugin" in result.output
        assert "bh config set plugins.beadlog-xtdb.enabled true" in result.output


def test_store_xtdb_enabled_but_not_installed_names_the_extra(hive, tmp_path, monkeypatch) -> None:  # noqa: F811
    monkeypatch.setattr(
        backfill_cli.config,
        "load",
        lambda *a, **k: {"plugins": {"beadlog-xtdb": {"enabled": True}}},
    )

    def missing(name: str):
        raise ModuleNotFoundError(f"No module named {name!r}", name="beadhive_beadlog_xtdb")

    monkeypatch.setattr(composition, "import_module", missing)
    result = _fails("backfill", "export", "--store", "xtdb")
    assert "its package is not installed — install `beadhive[xtdb]`" in result.output


def test_an_unknown_store_is_refused(hive) -> None:  # noqa: F811
    result = _fails("backfill", "export", "--store", "nope")
    assert "--store must be one of bd, xtdb (got 'nope')" in result.output
