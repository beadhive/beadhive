"""Integration: the bd StoreLoader over a REAL ``bd`` store (embedded Dolt in ``tmp_path``).

Proves what the unit suite's FakeBd assumes about bd 1.3.0: ``bd import`` honors the supplied
``created_at`` / ``closed_at`` and dependency / comment ``created_at``; ``metadata.bdlg`` survives
as a nested document; the batched ``bd update`` argv is accepted; re-exporting returns the loaded
events with their original ids; and a second load of the same events writes nothing.

Marked ``integration``; self-skips without ``bd`` on PATH. Nothing here touches a real hive.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import beadlog
from beadhive import backfill_bd, bd
from beadhive.backfill_bd import BdCliPort, BdStoreLoader, stamp_labels
from harness.beads import _SHARED_SERVER_ENV, bd_json, init_embedded, skip_if_no_bd
from harness.beads import bd as run_bd
from harness.world import git

pytestmark = [pytest.mark.integration, skip_if_no_bd]

GROUP = "urn:uuid:00000000-0000-4000-8000-0000000000b9"
IDENT = ("github", "agentguides", "infra")
TRIPLET = ("provider:github", "org:agentguides", "repo:infra")


@pytest.fixture
def hive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for key in _SHARED_SERVER_ENV:
        monkeypatch.delenv(key, raising=False)
    config = tmp_path / "config.yaml"
    config.write_text((Path(__file__).parent / "fixture_config.yaml").read_text())
    monkeypatch.setenv("BH_HOME", str(tmp_path))
    monkeypatch.setenv("BH_CONFIG", str(config))
    monkeypatch.setattr(bd, "workspace_identity", lambda cwd=None: IDENT)
    monkeypatch.setattr(backfill_bd, "workspace_identity", lambda cwd=None: IDENT)
    root = tmp_path / "hive"
    root.mkdir()
    git("init", "-q", "-b", "main", cwd=root)
    init_embedded(root, "ag-infra")
    return root


def _mint(kind: str, iid: str, data: dict, at: str | None, **kw) -> beadlog.BeadEvent:
    return beadlog.mint_event(
        kind,
        beadlog.BeadRef(GROUP, iid),
        data,
        occurred_at=at,
        recorded_at=kw.pop("recorded_at", None),
        extracted_at="2026-10-31T00:00:00Z",
        source=("docs", f"{iid}/{kind}/{at}"),
        provenance="inferred",
        **kw,
    )


def test_real_bd_round_trip_honors_import_times_and_reloads_with_zero_writes(hive: Path) -> None:
    history = [
        _mint(
            "create",
            "ag-infra-1",
            {"type": "task", "fields": {"title": "Old work", "priority": 2}},
            "2026-01-01T10:00:00Z",
        ),
        _mint(
            "create",
            "ag-infra-2",
            {"type": "task", "fields": {"title": "Other", "priority": 1}},
            "2026-01-01T11:00:00Z",
        ),
        _mint(
            "edge_add",
            "ag-infra-1",
            {"type": "blocks", "target": {"group": GROUP, "id": "ag-infra-2"}},
            "2026-01-02T00:00:00Z",
        ),
        _mint("comment", "ag-infra-1", {"body": "-a note"}, "2026-01-03T00:00:00Z", actor="dev/a"),
        _mint("phase", "ag-infra-1", {"phase": "closed", "reason": "done"}, "2026-01-04T00:00:00Z"),
    ]
    events = stamp_labels(history, [*TRIPLET, "origin:backfill", "source:docs"])
    loader = BdStoreLoader(BdCliPort(hive, actor="dev/loader"), group=GROUP)
    loader.load(events, dry_run=False)

    row = bd_json("show", "ag-infra-1", cwd=hive)
    row = row[0] if isinstance(row, list) else row
    assert row["created_at"].startswith("2026-01-01T10:00:00")
    assert row["closed_at"].startswith("2026-01-04T00:00:00")
    assert row["status"] == "closed" and row["close_reason"] == "done"
    assert row["metadata"]["bdlg"]["created"]["event_id"] == history[0].id
    assert {"origin:backfill", "source:docs", *TRIPLET} <= set(row["labels"])

    exported = {event.id for event in loader.export()}
    assert {event.id for event in events} <= exported

    # an existing bead takes ONE batched update (fields, labels, metadata) plus a comment
    later = [
        _mint(
            "set",
            "ag-infra-2",
            {"field": "description", "value": "-dash first"},
            "2026-02-01T00:00:00Z",
        ),
        _mint("label_add", "ag-infra-2", {"label": "area:x"}, "2026-02-02T00:00:00Z"),
        _mint(
            "set", "ag-infra-2", {"field": "metadata.team", "value": "core"}, "2026-02-03T00:00:00Z"
        ),
        _mint(
            "comment", "ag-infra-2", {"body": "follow-up"}, "2026-02-04T00:00:00Z", actor="dev/b"
        ),
    ]
    loader.load(later, dry_run=False)
    exported = {event.id for event in loader.export()}
    assert {event.id for event in later} <= exported
    before = run_bd("export", cwd=hive, capture=True).stdout
    assert before.count('"_type":"issue"') == 2
    loader.load([*events, *later], dry_run=False)
    head = bd_json("show", "ag-infra-2", cwd=hive)
    head = head[0] if isinstance(head, list) else head
    assert head["description"] == "-dash first" and head["metadata"]["team"] == "core"
    assert run_bd("export", cwd=hive, capture=True).stdout == before  # the reload wrote nothing


def test_real_bd_drops_an_edge_to_a_bead_created_by_a_later_load_and_the_loader_reports_it(
    hive: Path,
) -> None:
    """#163: the premise of the parents-first apply order, against the real bd."""
    epic = _mint(
        "create",
        "ag-infra-9",
        {"type": "epic", "fields": {"title": "Area"}},
        "2026-01-01T00:00:00Z",
    )
    child = _mint(
        "create", "ag-infra-1", {"type": "task", "fields": {"title": "Kid"}}, "2026-01-01T01:00:00Z"
    )
    edge = _mint(
        "edge_add",
        "ag-infra-1",
        {"type": "parent-child", "target": {"group": GROUP, "id": "ag-infra-9"}},
        "2026-01-01T02:00:00Z",
    )
    loader = BdStoreLoader(BdCliPort(hive, actor="dev/loader"), group=GROUP)
    report = loader.load(stamp_labels([child, edge], [*TRIPLET, "origin:backfill"]), dry_run=False)
    assert {loss["code"] for loss in report["losses"]} >= {"edge.dangling-target"}
    loader.load(stamp_labels([epic], [*TRIPLET, "origin:backfill"]), dry_run=False)
    row = bd_json("show", "ag-infra-1", cwd=hive)
    row = row[0] if isinstance(row, list) else row
    deps = row.get("dependencies") or []
    assert not any(d.get("depends_on_id") == "ag-infra-9" for d in deps), row  # bd dropped it
