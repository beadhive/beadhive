"""The XTDB as-of reader's output meets the BeadLog contract (bead bh-1e8ad).

Rows shaped as XTDB 2.2 returns them go through ``read_state`` / ``read_history``; the events
must validate against the v0.1 JSON Schemas (the byte copies every archive embeds, which a root
test pins to ``docs/schemas/beadlog/v0.1``), the archive written from them must pass
``beadlog verify`` (``ArchiveReader.verify(ids=True)``) and its ``MANIFEST.json`` the manifest
schema, and the events must serve as a ``beadlog_sources.reconcile`` baseline. ``jsonschema`` and
``beadlog_sources`` are dev tools, not dependencies of this package, so those checks skip where
they are absent. The real-store round trip is ``test_beadlog_xtdb_reader_int.py``. No store, no
clock, no network.
"""

from __future__ import annotations

import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import beadlog
from beadlog_xtdb import read_history, read_state, write_baseline

GROUP = "urn:uuid:5f0c9a52-8a43-4f0e-9a5e-1c7d2b3e4f60"
EXTRACTED = "2026-10-31T00:00:00Z"


def _validator(archive: Path, name: str) -> Any:
    """A Draft 2020-12 validator for the schema copy embedded in ``archive``."""
    jsonschema = pytest.importorskip("jsonschema")
    with zipfile.ZipFile(archive) as bundle:
        schema = json.loads(bundle.read(f"schema/{name}.schema.json"))
    return jsonschema.Draft202012Validator(schema)


def _at(text: str | None) -> datetime | None:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC) if text else None


def _row(
    key: str,
    vf: str,
    vt: str | None = None,
    sf: str | None = None,
    st: str | None = None,
    **doc: Any,
):
    return {
        "_id": key,
        **doc,
        "_valid_from": _at(vf),
        "_valid_to": _at(vt),
        "_system_from": _at(sf or vf),
        "_system_to": _at(st),
    }


def _bead(name: str, vf: str, vt: str | None = None, **doc: Any):
    return _row(
        f"bh-{name}",
        vf,
        vt,
        group_id=GROUP,
        bead_id=f"bh-{name}",
        issue_type=doc.pop("issue_type", "task"),
        title=doc.pop("title", name.upper()),
        **doc,
    )


class _Connection:
    def __init__(self, **tables: list[dict[str, Any]]) -> None:
        self.tables = tables

    def execute(self, sql: str, params: Any = None) -> Any:
        rows = self.tables.get(sql.split(" FROM ", 1)[1].split()[0], [])
        names: list[str] = []
        for record in rows:
            names += [name for name in record if name not in names]
        return SimpleNamespace(
            description=[SimpleNamespace(name=name) for name in names],
            fetchall=lambda: [tuple(r.get(n) for n in names) for r in rows],
        )


def _store() -> _Connection:
    """Every kind the reader emits: create, set (with a clear), phase, retire, delete, label and
    edge adds and removes, a comment, and (with checkpoints) the derived checkpoint."""
    return _Connection(
        beads=[
            _bead("a", "2026-10-01T00:00:00Z", "2026-10-03T00:00:00Z", priority=2, description="d"),
            _bead("a", "2026-10-03T00:00:00Z", "2026-10-05T00:00:00Z", priority=1, title="A2"),
            _bead("a", "2026-10-05T00:00:00Z", priority=1, title="A2", phase="closed"),
            _bead(
                "b",
                "2026-10-02T00:00:00Z",
                issue_type="bug",
                phase="retired",
                retired={"successor": {"group": GROUP, "id": "bh-a"}, "reason": "merged"},
            ),
            _bead("c", "2026-10-02T00:00:00Z", deleted=True),
        ],
        bead_labels=[
            _row(
                "bh-a|x", "2026-10-02T00:00:00Z", "2026-10-04T00:00:00Z", bead_id="bh-a", label="x"
            ),
            _row(
                "bh-a|x",
                "2026-10-04T00:00:00Z",
                sf="2026-10-02T00:00:00Z",
                st="2026-10-04T00:00:01Z",
                bead_id="bh-a",
                label="x",
            ),
        ],
        bead_edges=[
            _row(
                "bh-a|blocks|bh-b",
                "2026-10-03T00:00:00.5Z",
                "2026-10-06T00:00:00Z",
                from_id="bh-a",
                type="blocks",
                to_id="bh-b",
                created_by="alice",
            ),
        ],
        bead_comments=[
            _row(
                "sha256:" + "c" * 64,
                "2026-10-04T11:00:00Z",
                bead_id="bh-a",
                author="alice",
                text="hi",
            ),
        ],
    )


def _read(mode: str) -> Any:
    reader = read_history if mode == "history" else read_state
    return reader(_store(), extracted_at=EXTRACTED, checkpoints=True)


@pytest.mark.parametrize("mode", ["history", "state"])
def test_every_event_validates_against_the_event_schema(mode: str, tmp_path: Path) -> None:
    result = _read(mode)
    path = tmp_path / "baseline.bdlg"
    write_baseline(path, result)
    validator = _validator(path, "event")
    kinds = {str(event.kind) for event in result.all_events}
    assert {"create", "checkpoint", "comment", "phase", "retire", "delete"} <= kinds
    for event in result.all_events:
        validator.validate(event.to_json())
    assert {e.source.system for e in result.all_events} == {"xtdb"}
    assert {str(e.provenance) for e in result.events} == {"observed"}


def test_history_carries_every_change_kind() -> None:
    kinds = {str(e.kind) for e in read_history(_store(), extracted_at=EXTRACTED).events}
    assert kinds == {
        "create", "set", "phase", "retire", "delete", "label_add", "label_remove",
        "edge_add", "edge_remove", "comment",
    }  # fmt: skip


def test_the_baseline_archive_passes_verify_and_the_manifest_schema(tmp_path: Path) -> None:
    result = _read("history")
    path = tmp_path / "xtdb-baseline.bdlg"
    write_baseline(path, result, description="baseline read from XTDB", index=True)
    with beadlog.ArchiveReader(path) as reader:
        reader.verify(ids=True)  # what `beadlog verify` runs
        assert reader.manifest.counts["events"] == len(result.all_events)
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read("MANIFEST.json"))
    _validator(path, "manifest").validate(manifest)
    assert "checkpoints" in manifest["capabilities"]["optional"]
    assert "recorded-time-monotone-load" in manifest["capabilities"]["optional"]


def test_the_events_are_a_reconcile_baseline(tmp_path: Path) -> None:
    sources = pytest.importorskip("beadlog_sources")
    result = read_history(_store(), extracted_at=EXTRACTED)
    path = tmp_path / "baseline.bdlg"
    write_baseline(path, result)
    _, baseline = beadlog.read_archive(path)
    # The store against itself proposes nothing ...
    assert sources.reconcile(baseline, baseline).empty
    # ... and a bead it lacks is NEW.
    new = beadlog.mint_event(
        "create",
        beadlog.BeadRef(GROUP, "bh-z"),
        {"type": "task", "fields": {"title": "Something nobody has filed"}},
        occurred_at="2026-10-20T00:00:00Z",
        recorded_at=None,
        extracted_at=EXTRACTED,
        source=("docs", "plan#z"),
        provenance="inferred",
    )
    plan = sources.reconcile([*baseline, new], baseline)
    assert plan.counts()[str(sources.ReconcileClass.NEW)] == 1
    assert not plan.empty
