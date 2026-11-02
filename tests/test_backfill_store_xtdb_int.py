"""``bh backfill apply <plan.bdlg> --store xtdb`` into a real XTDB 2.2.0 node (opt-in, integration).

Skipped unless ``BEADLOG_XTDB_TEST_DSN`` names a node (no password in it; the config refuses
one) and the opt-in plugin is installed. ``BEADLOG_XTDB_TEST_PASSWORD`` is the node's
``XTDB_PASSWORD`` when it runs with one; it is handed to the store through the password port
in place of the fnox lookup, never through configuration. For example, a throwaway node::

    docker run -d --name xtdb-bh-int --memory 2g --user "$(id -u):$(id -g)" \\
      -p 127.0.0.1:15470:5432 -e XTDB_PASSWORD=... -v /data/<real-disk>/xtdb:/var/lib/xtdb \\
      --health-cmd 'wget -q -O /dev/null http://127.0.0.1:8080/healthz/alive' \\
      ghcr.io/xtdb/xtdb@sha256:a6a426505b95a36e4e0e1d0de88efcf4220cb6bde8f1f5e594b0a1f92a948a77
    BEADLOG_XTDB_TEST_DSN="host=127.0.0.1 port=15470 user=xtdb dbname=xtdb" \\
      BEADLOG_XTDB_TEST_PASSWORD=... pytest -m integration tests/test_backfill_store_xtdb_int.py

Each test loads into its own ``ATTACH DATABASE`` (a fresh clock, so the monotone initial load
applies), driven through the real CLI, the real ``beadlog.store`` composition root and the real
plugin. No test depends on wall-clock timing: the fixture's times are fixed and in the past.
"""

from __future__ import annotations

import importlib.util
import json
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import beadlog
from beadhive import backfill_cli, cli
from beadhive import beadlog_store_credentials as credentials
from beadhive_backfill import Plan
from beadlog_sources import reconcile, write_plan

pytestmark = pytest.mark.integration

DSN = os.environ.get("BEADLOG_XTDB_TEST_DSN", "")
PASSWORD = os.environ.get("BEADLOG_XTDB_TEST_PASSWORD") or None
ENV = {"COLUMNS": "120", "BH_SKIP_SETUP_CHECK": "1", "NO_COLOR": "1"}
GROUP = beadlog.Group("urn:uuid:6a1d0b63-9b54-4f1f-8b6f-2d8e3c4f5a71", "wd", description="it")


def _dsn_for(database: str) -> str:
    parts = [part for part in DSN.split() if not part.startswith("dbname=")]
    return " ".join([*parts, f"dbname={database}"])


@pytest.fixture
def fresh_dsn() -> Iterator[str]:
    """The DSN of a freshly attached database on the node."""
    if not DSN:
        pytest.skip("BEADLOG_XTDB_TEST_DSN is not set (no XTDB node to test against)")
    if importlib.util.find_spec("beadhive_beadlog_xtdb") is None:
        pytest.skip("the beadhive[xtdb] plugin is not installed")
    from beadlog_xtdb import connect

    extra = {"password": PASSWORD} if PASSWORD else {}
    try:
        admin = connect(DSN, autocommit=True, connect_timeout=10, **extra)
    except Exception as exc:  # noqa: BLE001 - any connect failure means no usable node
        pytest.skip(f"no XTDB node reachable at BEADLOG_XTDB_TEST_DSN: {type(exc).__name__}")
    name = "bhint_" + uuid.uuid4().hex[:12]
    try:
        admin.execute(f"ATTACH DATABASE {name}")
        yield _dsn_for(name)
    finally:
        admin.close()


@pytest.fixture
def store_config(fresh_dsn, monkeypatch, tmp_path) -> str:
    """The host config: the plugin enabled at the fresh database; the password via the port."""
    cfg = {"plugins": {"beadlog-xtdb": {"enabled": True, "dsn": fresh_dsn}}}
    monkeypatch.setattr(backfill_cli.config, "load", lambda *a, **k: cfg)
    monkeypatch.setattr(credentials, "fnox_password_source", lambda values: lambda: PASSWORD)
    monkeypatch.setattr(backfill_cli, "_target", lambda hive: (tmp_path, GROUP))
    return fresh_dsn


def _ev(kind: str, bead: str, data: dict[str, Any], occurred: str, recorded: str | None):
    return beadlog.mint_event(
        kind,
        beadlog.BeadRef(GROUP.id, bead),
        data,
        occurred_at=occurred,
        recorded_at=recorded,
        extracted_at="2026-10-31T00:00:00Z",
        source=("it", f"{kind}:{bead}:{occurred}"),
        provenance="observed",
        actor="dev/it",
    )


def fixture_plan(path: Path) -> Plan:
    """A ``beadlog reconcile`` plan of two NEW beads against the empty store."""
    proposed = [
        _ev("create", "wd-1", {"type": "task", "fields": {"title": "First"}},
            "2026-07-01T10:00:00Z", "2026-07-01T10:00:05Z"),
        _ev("label_add", "wd-1", {"label": "area:cli"},
            "2026-07-01T10:01:00Z", "2026-07-01T10:01:05Z"),
        _ev("create", "wd-2", {"type": "bug", "fields": {"title": "Second"}},
            "2026-07-02T09:00:00Z", "2026-07-02T09:00:05Z"),
        _ev("phase", "wd-2", {"phase": "closed"}, "2026-07-03T09:00:00Z", None),
    ]  # fmt: skip
    write_plan(path, reconcile(proposed, ()), groups=[GROUP],
               generator=beadlog.Generator("beadlog", "0.1.0"))  # fmt: skip
    return Plan.read(path)


def _invoke(*args: str) -> Any:
    result = CliRunner().invoke(cli.app, list(args), env=ENV)
    assert result.exit_code == 0, result.output
    return result


def test_apply_store_xtdb_loads_a_fixture_plan_into_a_real_node(store_config, tmp_path) -> None:
    plan_path = tmp_path / "plan.bdlg"
    plan = fixture_plan(plan_path)

    preview = json.loads(
        _invoke(
            "backfill", "apply", str(plan_path), "--store", "xtdb", "--dry-run", "--json"
        ).stdout
    )
    assert preview["store"] == "xtdb" and preview["changes"] == 2
    assert preview["loss_report"]["loader"]["name"] == "xtdb"

    audit = json.loads(
        _invoke("backfill", "apply", str(plan_path), "--store", "xtdb", "--json").stdout
    )
    assert audit["state"] == "applied"
    assert audit["verification"]["second_dry_run_changes"] == 0
    assert (tmp_path / "plan.pre-state.bdlg").is_file()

    from beadlog_xtdb import XtdbStoreLoader, connect

    extra = {"password": PASSWORD} if PASSWORD else {}
    connection = connect(store_config, autocommit=True, **extra)
    try:
        loaded = XtdbStoreLoader(connection).export()
    finally:
        connection.close()
    assert {event.id for event in loaded} == {event.id for event in plan.applicable_events()}
    states = beadlog.fold(loaded)
    by_id = {ref.id: state for ref, state in states.items()}
    assert by_id["wd-1"].fields["title"] == "First" and "area:cli" in by_id["wd-1"].labels
    assert by_id["wd-2"].phase == "closed"

    out = tmp_path / "store.bdlg"
    summary = json.loads(
        _invoke("backfill", "export", "--store", "xtdb", "--out", str(out), "--json").stdout
    )
    assert (summary["beads"], summary["store"]["store"]) == (2, "xtdb")
    _manifest, exported = beadlog.read_archive(out)
    exported_states = {ref.id: state for ref, state in beadlog.fold(exported).items()}
    assert exported_states["wd-1"].fields["title"] == "First"
    assert exported_states["wd-2"].phase == "closed"

    again = CliRunner().invoke(
        cli.app, ["backfill", "apply", str(plan_path), "--store", "xtdb"], env=ENV
    )
    assert again.exit_code == 1 and "corpus changed after planning" in again.output
