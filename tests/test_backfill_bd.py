"""The bd StoreLoader (``beadhive.backfill_bd``) against an in-memory bd.

``FakeBd`` emulates the bd 1.3.0 behavior the loader relies on, as measured on a scratch
``bd init`` store: ``bd import`` honors ``created_at`` / ``closed_at`` / ``started_at`` and
dependency and comment ``created_at`` and fills ``priority`` 0 and type ``task``; ``bd update``
merges metadata top-level keys and stamps ``updated_at``; ``bd close`` / ``bd comments add`` /
``bd dep add`` stamp the store's own time. Its clock is a counter, never the wall clock. The
argv the real port sends is pinned by ``packages/beadhive-bd-cli/tests/test_routes.py``; the
import path through ``bd.import_labeled`` is exercised below with bd's process boundary faked.
"""

from __future__ import annotations

import copy
import io
import json
from collections import namedtuple
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import jsonschema
import pytest

import beadlog
from beadhive import backfill_bd, bd, validate
from beadhive.backfill_bd import BdCliPort, BdStoreLoader, origin_labels, stamp_labels
from beadhive_backfill import (
    ROLLBACK_REASON,
    AuditState,
    BackfillEngine,
    BackfillError,
    Stamper,
    inverse_of,
)
from beadlog.testing import conformance_archive, conformance_events
from beadlog_sources import BdBaselineStrategy, ExtractRequest

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads(
    (ROOT / "docs/schemas/beadlog/v0.1/loss-report.schema.json").read_text(encoding="utf-8")
)
GROUP = "urn:uuid:00000000-0000-4000-8000-0000000000b8"
TRIPLET = ("provider:github", "org:agentguides", "repo:infra")
Completed = namedtuple("Completed", "returncode stdout stderr")


class Ticks:
    """A deterministic clock: each call is one second after the previous."""

    def __init__(self, start: int = 0) -> None:
        self.n = start

    def __call__(self) -> str:
        self.n += 1
        return f"2026-10-31T{self.n // 3600:02d}:{self.n // 60 % 60:02d}:{self.n % 60:02d}Z"


class FakeBd:
    """An in-memory bd store behind :class:`backfill_bd.BdPort` (see module doc)."""

    target = "bd:fake"

    def __init__(
        self,
        rows: Iterable[Mapping[str, Any]] = (),
        *,
        identity_labels: Sequence[str] = (),
        actor: str = "dev/loader",
    ) -> None:
        self.rows: dict[str, dict[str, Any]] = {row["id"]: copy.deepcopy(dict(row)) for row in rows}
        self.identity_labels = tuple(identity_labels)
        self.actor = actor
        self.clock = Ticks(1000)
        self.writes: list[tuple[str, ...]] = []
        self.reads = 0
        self.fail: Callable[[str, str], bool] = lambda op, bead: False

    # -- reads
    def export_rows(self) -> list[dict[str, Any]]:
        self.reads += 1
        return [copy.deepcopy(self.rows[key]) for key in sorted(self.rows)]

    def export_to(self, path: Path) -> None:
        path.write_text("".join(json.dumps(row) + "\n" for row in self.export_rows()))

    # -- writes
    def _write(self, op: str, bead: str) -> None:
        if self.fail(op, bead):
            raise backfill_bd.BdLoadError(f"injected failure: {op} {bead}")
        self.writes.append((op, bead))

    def import_records(self, records: Sequence[Mapping[str, Any]]) -> None:
        for record in records:
            self._write("import", record["id"])
            if not record.get("title"):
                raise backfill_bd.BdLoadError("title is required")
            now = self.clock()
            row = copy.deepcopy(dict(record))
            row["labels"] = list(record.get("labels") or [])
            row["labels"] += [lb for lb in self.identity_labels if lb not in row["labels"]]
            row.setdefault("priority", 0)
            row.setdefault("issue_type", "task")
            row.setdefault("status", "open")
            row.setdefault("created_at", now)
            row["updated_at"] = now
            if row["status"] == "closed":
                row.setdefault("closed_at", now)
            for dep in row.get("dependencies") or []:
                dep.setdefault("created_at", now)
            for index, comment in enumerate(row.get("comments") or []):
                comment.setdefault("created_at", now)
                comment.setdefault("id", f"{record['id']}-c{index}")
            self.rows[record["id"]] = row

    def apply_changes(
        self,
        bead: str,
        *,
        fields: Mapping[str, str],
        add_labels: Sequence[str],
        remove_labels: Sequence[str],
        status: str,
        metadata: Mapping[str, Any] | None,
        force: bool,
    ) -> None:
        self._write("update", bead)
        row = self.rows[bead]
        columns = {
            "acceptance": "acceptance_criteria",
            "external-ref": "external_ref",
            "due": "due_at",
            "defer": "defer_until",
            "estimate": "estimated_minutes",
            "spec-id": "spec_id",
            "type": "issue_type",
        }
        if "assignee" in fields and row.get("status") == "in_progress" and not force:
            raise backfill_bd.BdLoadError("cannot reassign a held bead without --force")
        for flag, value in fields.items():
            column = columns.get(flag, flag)
            if flag == "title" and not value:
                raise backfill_bd.BdLoadError("title cannot be empty")
            if value == "":
                row.pop(column, None)
            elif flag in ("priority", "estimate"):
                row[column] = int(value)
            else:
                row[column] = value
        labels = [lb for lb in row.get("labels", []) if lb not in remove_labels]
        row["labels"] = labels + [lb for lb in add_labels if lb not in labels]
        if status:
            if status == "closed":
                raise backfill_bd.BdLoadError("close through bd close")
            if row.get("status") == "closed":
                row.pop("closed_at", None)
                row.pop("close_reason", None)
            row["status"] = status
        if metadata:
            row["metadata"] = {**row.get("metadata", {}), **copy.deepcopy(dict(metadata))}
        row["updated_at"] = self.clock()

    def unset_metadata(self, bead: str, keys: Sequence[str]) -> None:
        self._write("unset", bead)
        for key in keys:
            self.rows[bead].get("metadata", {}).pop(key, None)
        self.rows[bead]["updated_at"] = self.clock()

    def close(self, bead: str, reason: str) -> None:
        self._write("close", bead)
        row = self.rows[bead]
        row.update(status="closed", close_reason=reason, closed_at=self.clock())
        row["updated_at"] = row["closed_at"]

    def add_dependency(self, bead: str, target: str, type_: str) -> None:
        self._write("dep-add", bead)
        deps = self.rows[bead].setdefault("dependencies", [])
        deps.append(
            {"issue_id": bead, "depends_on_id": target, "type": type_, "created_at": self.clock()}
        )

    def remove_dependency(self, bead: str, target: str) -> None:
        self._write("dep-remove", bead)
        row = self.rows[bead]
        row["dependencies"] = [
            dep for dep in row.get("dependencies", []) if dep["depends_on_id"] != target
        ]

    def add_comment(self, bead: str, body: str, author: str | None) -> None:
        self._write("comment", bead)
        comments = self.rows[bead].setdefault("comments", [])
        comments.append(
            {
                "id": f"{bead}-n{len(comments)}",
                "author": author or self.actor,
                "text": body,
                "created_at": self.clock(),
            }
        )

    def delete(self, bead: str) -> None:
        self._write("delete", bead)
        del self.rows[bead]


def loader(store: FakeBd | None = None, **options: Any) -> BdStoreLoader:
    options.setdefault("group", GROUP)
    return BdStoreLoader(store if store is not None else FakeBd(), clock=Ticks(), **options)


def mint(kind: str, iid: str, data: Mapping[str, Any], at: str | None, **kw: Any) -> Any:
    return beadlog.mint_event(
        kind,
        beadlog.BeadRef(kw.pop("group", GROUP), iid),
        data,
        occurred_at=at,
        recorded_at=kw.pop("recorded_at", None),
        extracted_at="2026-10-31T00:00:00Z",
        source=kw.pop("source", ("docs", f"{iid}/{kind}/{at}")),
        provenance=kw.pop("provenance", "inferred"),
        **kw,
    )


def by_code(report: Mapping[str, Any]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for loss in report["losses"]:
        out.setdefault(loss["code"], set()).add(loss["event_id"])
    return out


@pytest.fixture
def cfg_path(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text((Path(__file__).parent / "fixture_config.yaml").read_text())
    monkeypatch.setenv("BH_HOME", str(tmp_path))
    monkeypatch.setenv("BH_CONFIG", str(path))
    return path


# -- conformance ---------------------------------------------------------------------------

CONFORMANCE_GROUP = conformance_events()[0].bead.group


@pytest.mark.parametrize("case", beadlog.conformance_cases(), ids=lambda case: case.name)
def test_beadlog_loader_conformance(case: beadlog.ConformanceCase) -> None:
    case.run(lambda: loader(group=CONFORMANCE_GROUP))


def test_conformance_loss_report_validates_against_the_published_schema() -> None:
    store = loader(group=CONFORMANCE_GROUP)
    report = beadlog.load_archive(store, _conformance_archive(), dry_run=False)
    jsonschema.validate(report, SCHEMA)
    codes = by_code(report)
    assert {"valid-time.degraded", "recorded-time.reassigned", "recorded-time.null"} <= set(codes)
    assert "phase.approximated" in codes  # the retirement
    assert "erasure.incomplete" in codes  # the tombstone
    assert "x-bd.delete-disabled" in codes  # deletion is off by default


def _conformance_archive() -> Any:
    return io.BytesIO(conformance_archive())


# -- export -> load -> export --------------------------------------------------------------

LIVE_ROWS = [
    {
        "id": "pr-a1",
        "title": "Alpha",
        "description": "-starts with a dash",
        "notes": "n",
        "status": "in_progress",
        "priority": 2,
        "issue_type": "feature",
        "assignee": "dev/bob",
        "estimated_minutes": 30,
        "created_at": "2026-01-01T10:00:00Z",
        "updated_at": "2026-03-01T00:00:00Z",
        "started_at": "2026-02-01T00:00:00Z",
        "due_at": "2026-04-01T00:00:00Z",
        "external_ref": "gh-1",
        "metadata": {"team": "core", "budget": 1.5},
        "labels": ["provider:github", "area:x"],
        "dependencies": [
            {
                "issue_id": "pr-a1",
                "depends_on_id": "pr-b1",
                "type": "blocks",
                "created_at": "2026-01-02T00:00:00Z",
            }
        ],
        "comments": [
            {"id": "c1", "author": "dev/carol", "text": "hi", "created_at": "2026-01-03T00:00:00Z"}
        ],
    },
    {
        "id": "pr-b1",
        "title": "Beta",
        "status": "closed",
        "priority": 1,
        "issue_type": "task",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-09T12:00:00Z",
        "closed_at": "2026-01-09T12:00:00Z",
        "close_reason": "done",
    },
]


def _strip_comment_ids(state: beadlog.BeadState) -> Any:
    return (
        state.exists,
        state.type,
        state.phase,
        dict(state.fields),
        state.labels,
        state.edges,
        [(c.actor, c.body) for c in state.comments],
    )


def test_export_load_into_an_empty_store_re_exports_an_equivalent_event_set() -> None:
    source = loader(FakeBd(LIVE_ROWS))
    exported = source.export()
    target_store = FakeBd()
    target = loader(target_store)
    report = target.load(exported, dry_run=False)
    jsonschema.validate(report, SCHEMA)
    assert report["summary"]["dropped"] == 0

    again = target.export()
    assert {e.id for e in again} == {e.id for e in exported}
    before, after = beadlog.fold(exported), beadlog.fold(again)
    assert {b: _strip_comment_ids(s) for b, s in before.items()} == {
        b: _strip_comment_ids(s) for b, s in after.items()
    }
    # bd honors the native times of a new bead on import
    row = target_store.rows["pr-a1"]
    assert row["created_at"] == "2026-01-01T10:00:00Z"
    assert row["dependencies"][0]["created_at"] == "2026-01-02T00:00:00Z"
    assert row["comments"][0]["created_at"] == "2026-01-03T00:00:00Z"
    assert target_store.rows["pr-b1"]["closed_at"] == "2026-01-09T12:00:00Z"
    assert row["metadata"]["budget"] == "1.5"  # floats travel as decimal strings
    assert target_store.writes == [("import", "pr-a1"), ("import", "pr-b1")]  # one batch


def test_the_neutral_strategy_and_the_loader_read_the_same_baseline() -> None:
    rows = FakeBd(LIVE_ROWS)
    text = "".join(json.dumps(row) + "\n" for row in rows.export_rows())
    strategy = BdBaselineStrategy(lambda argv: text)
    request = ExtractRequest(root=Path("."), group=GROUP, extracted_at="2026-10-31T00:00:00Z")
    assert {e.id for e in strategy.extract(request)} == {e.id for e in loader(rows).export()}


# -- idempotency ---------------------------------------------------------------------------


def _history() -> list[Any]:
    return [
        mint(
            "create",
            "bf-1",
            {"type": "task", "fields": {"title": "Draft", "priority": 2}},
            "2026-05-01T00:00:00Z",
            occurred_precision="day",
        ),
        mint("set", "bf-1", {"field": "title", "value": "Final"}, "2026-05-02T00:00:00Z"),
        mint("set", "bf-1", {"field": "metadata.est", "value": 3}, "2026-05-02T01:00:00Z"),
        mint("label_add", "bf-1", {"label": "area:x"}, "2026-05-03T00:00:00Z"),
        mint("label_add", "bf-1", {"label": "tmp"}, "2026-05-03T01:00:00Z"),
        mint("label_remove", "bf-1", {"label": "tmp"}, "2026-05-03T02:00:00Z"),
        mint(
            "edge_add",
            "bf-1",
            {"type": "blocks", "target": {"group": GROUP, "id": "pr-b1"}},
            "2026-05-04T00:00:00Z",
        ),
        mint("phase", "bf-1", {"phase": "closed", "reason": "shipped"}, "2026-05-05T00:00:00Z"),
        mint("comment", "bf-1", {"body": "from the plan"}, "2026-05-06T00:00:00Z", actor="dev/a"),
        mint("label_add", "pr-a1", {"label": "area:y"}, "2026-05-07T00:00:00Z"),
        mint("set", "pr-a1", {"field": "notes", "value": None}, "2026-05-07T01:00:00Z"),
        mint("set", "pr-a1", {"field": "metadata.team", "value": None}, "2026-05-07T02:00:00Z"),
        mint("set", "pr-a1", {"field": "assignee", "value": "dev/zed"}, "2026-05-07T03:00:00Z"),
        mint("phase", "pr-a1", {"phase": "closed"}, "2026-05-07T04:00:00Z", recorded_at=None),
        mint("comment", "pr-a1", {"body": "late note"}, "2026-05-07T05:00:00Z", actor="dev/q"),
    ]


def test_loading_the_same_plan_twice_writes_nothing_the_second_time() -> None:
    store = FakeBd(LIVE_ROWS)
    bd_loader = loader(store)
    events = _history()
    first = bd_loader.load(events, dry_run=False)
    writes = list(store.writes)
    assert writes  # the first load wrote
    snapshot = copy.deepcopy(store.rows)

    second = bd_loader.load(events, dry_run=False)
    assert store.writes == writes  # zero writes the second time
    assert store.rows == snapshot
    jsonschema.validate(first, SCHEMA)
    jsonschema.validate(second, SCHEMA)
    # the superseded label event is reported dropped both times, never silently ignored
    tmp_add = events[4]
    assert tmp_add.id in by_code(first)["x-bd.superseded"]
    assert tmp_add.id in by_code(second)["x-bd.superseded"]


def test_existing_bead_changes_are_batched_into_one_update() -> None:
    store = FakeBd(LIVE_ROWS)
    loader(store).load(_history(), dry_run=False)
    pr_a1 = [op for op in store.writes if op[1] == "pr-a1"]
    assert pr_a1 == [
        ("update", "pr-a1"),
        ("unset", "pr-a1"),
        ("close", "pr-a1"),
        ("comment", "pr-a1"),
    ]
    row = store.rows["pr-a1"]
    assert "notes" not in row and "team" not in row["metadata"]
    assert row["assignee"] == "dev/zed"  # forced over the live in_progress claim
    assert row["status"] == "closed" and row["close_reason"] == "closed"
    assert row["metadata"]["bdlg"]["label:area:y"]["occurred_at"] == "2026-05-07T00:00:00Z"
    new = store.rows["bf-1"]
    assert new["title"] == "Final" and new["status"] == "closed"
    assert new["closed_at"] == "2026-05-05T00:00:00Z" and new["close_reason"] == "shipped"
    assert new["labels"] == ["area:x"] and new["metadata"]["est"] == 3


def test_a_dry_run_writes_nothing_and_reports_what_the_apply_reports() -> None:
    preview_store, store = FakeBd(LIVE_ROWS), FakeBd(LIVE_ROWS)
    preview = loader(preview_store).load(_history(), dry_run=True)
    applied = loader(store).load(_history(), dry_run=False)
    assert preview_store.writes == []
    assert preview["losses"] == applied["losses"]
    assert preview["summary"] == applied["summary"]
    assert preview["mode"] == "dry-run" and applied["mode"] == "apply"


# -- loss report ---------------------------------------------------------------------------


def test_every_field_bd_cannot_honor_is_in_the_loss_report() -> None:
    store = FakeBd(LIVE_ROWS)
    events = [
        mint("set", "pr-a1", {"field": "owner", "value": "o@x"}, "2026-06-01T00:00:00Z"),
        mint("set", "pr-a1", {"field": "story_points", "value": 3}, "2026-06-01T00:00:01Z"),
        mint("set", "pr-a1", {"field": "title", "value": None}, "2026-06-01T00:00:02Z"),
        mint("set", "pr-a1", {"field": "metadata.bdlg", "value": 1}, "2026-06-01T00:00:03Z"),
        mint("phase", "pr-a1", {"phase": "triaged"}, "2026-06-01T00:00:04Z"),
        mint(
            "edge_add",
            "pr-a1",
            {"type": "related", "target": {"group": "urn:uuid:other", "id": "x-1"}},
            "2026-06-01T00:00:05Z",
        ),
        mint(
            "edge_add",
            "pr-a1",
            {"type": "related", "target": {"group": GROUP, "id": "nowhere-9"}},
            "2026-06-01T00:00:06Z",
        ),
        mint("retire", "pr-b1", {"reason": "superseded"}, "2026-06-02T00:00:00Z"),
        mint("delete", "pr-b1", {"reason": "mistake"}, "2026-06-03T00:00:00Z"),
        mint(
            "tombstone",
            "pr-a1",
            {"scope": "events", "events": ["sha256:" + "1" * 64], "reason": "pii"},
            "2026-06-04T00:00:00Z",
        ),
        mint("label_add", "pr-a1", {"label": "z"}, "2026-06-05T00:00:00Z", group="urn:uuid:other"),
        mint("label_add", "ghost-1", {"label": "z"}, "2026-06-05T00:00:00Z"),
        mint("create", "bf-2", {"type": "task", "fields": {"title": "T"}}, None),
    ]
    report = loader(store).load(events, dry_run=False)
    jsonschema.validate(report, SCHEMA)
    codes = by_code(report)
    ids = [event.id for event in events]
    assert set(ids[:4]) <= codes["field.unsupported"]
    assert ids[4] in codes["x-bd.unknown-status"]
    assert {ids[5], ids[6]} <= codes["edge.dangling-target"]
    assert ids[7] in codes["phase.approximated"]
    assert ids[8] in codes["x-bd.delete-disabled"]
    assert ids[9] in codes["erasure.incomplete"]
    assert ids[10] in codes["x-bd.foreign-group"]
    assert ids[11] in codes["x-bd.missing-bead"]
    assert ids[12] in codes["valid-time.missing"]
    assert ids[12] in codes["x-bd.default-filled"]  # no priority: bd fills 0
    # nothing is silently dropped: every event is either stored or reported dropped
    dropped = {loss["event_id"] for loss in report["losses"] if loss["disposition"] == "dropped"}
    assert report["summary"]["dropped"] == len(dropped)
    assert report["summary"]["loaded"] + len(dropped) == len(events)
    exported = {event.id for event in loader(store).export()}
    for event in events:
        assert (event.id in exported) != (event.id in dropped), event.kind
    assert "pr-b1" in store.rows  # deletion is off by default: nothing was deleted
    assert store.rows["pr-b1"]["metadata"]["bdlg"]["retired"] == {"reason": "superseded"}
    assert report["summary"]["by_code"]["x-bd.non-identity"] >= 1


def test_delete_needs_the_opt_in_and_then_reports_the_bead_gone() -> None:
    store = FakeBd(LIVE_ROWS)
    delete = mint("delete", "pr-b1", {"reason": "mistake"}, "2026-06-03T00:00:00Z")
    report = loader(store, delete_enabled=True).load([delete], dry_run=False)
    assert "pr-b1" not in store.rows
    assert delete.id in by_code(report)["x-bd.deleted"]


def test_an_id_collision_refuses_the_load_before_any_write() -> None:
    store = FakeBd(LIVE_ROWS)
    first = mint("label_add", "pr-a1", {"label": "q"}, "2026-06-01T00:00:00Z")
    loader(store).load([first], dry_run=False)
    writes = list(store.writes)
    forged = beadlog.BeadEvent.from_json({**first.to_json(), "data": {"label": "other"}})
    with pytest.raises(beadlog.IdCollisionError):
        loader(store).load([forged], dry_run=False)
    assert store.writes == writes


# -- labels --------------------------------------------------------------------------------


def test_new_beads_carry_origin_and_source_labels_and_pass_label_validation(cfg_path) -> None:
    store = FakeBd(identity_labels=TRIPLET)
    create = mint("create", "ag-infra-9", {"type": "task", "fields": {"title": "T"}}, None)
    events = stamp_labels([create], [*TRIPLET, *origin_labels("git-commits")])
    report = loader(store).load(events, dry_run=False)
    labels = store.rows["ag-infra-9"]["labels"]
    assert {"origin:backfill", "source:git-commits", *TRIPLET} == set(labels)
    assert "x-bd.labels-added" not in by_code(report)  # the plan already carried the triplet
    assert validate.bead_violations(None, "ag-infra-9", labels) == []
    assert stamp_labels(events, TRIPLET) == events  # idempotent: no duplicate label events


def test_labels_bd_import_adds_behind_the_plan_are_reported() -> None:
    store = FakeBd(identity_labels=TRIPLET)
    create = mint("create", "ag-infra-8", {"type": "task", "fields": {"title": "T"}}, None)
    report = loader(store).load([create], dry_run=False)
    assert create.id in by_code(report)["x-bd.labels-added"]


def test_origin_labels_reject_an_unsafe_kind() -> None:
    assert origin_labels("docs") == ("origin:backfill", "source:docs")
    with pytest.raises(ValueError):
        origin_labels("Docs Bridge")


def test_the_cli_port_imports_through_import_labeled(cfg_path, monkeypatch, tmp_path) -> None:
    """The real port's import goes through `bd.import_labeled`: the identity triplet is merged
    and each new record is gated on its own labels (bh-vfx9)."""
    seen: list[list[dict[str, Any]]] = []

    def fake_run(cmd: list[str], **_: Any) -> Completed:
        source = Path(cmd[-1])
        seen.append([json.loads(line) for line in source.read_text().splitlines()])
        return Completed(0, "", "")

    monkeypatch.setattr(bd, "_run", fake_run)
    monkeypatch.setattr(bd, "workspace_identity", lambda cwd=None: TRIPLET_IDENT)
    monkeypatch.setattr(backfill_bd, "workspace_identity", lambda cwd=None: TRIPLET_IDENT)
    port = BdCliPort(tmp_path, actor="dev/loader")
    assert port.identity_labels == TRIPLET
    port.import_records([{"id": "ag-infra-9", "title": "T", "labels": ["origin:backfill"]}])
    (records,) = seen
    assert set(records[0]["labels"]) == {"origin:backfill", *TRIPLET}
    with pytest.raises(backfill_bd.BdLoadError, match="label violations"):
        port.import_records([{"id": "ag-infra-9", "title": "T", "labels": ["origin:pigeon"]}])


TRIPLET_IDENT = ("github", "agentguides", "infra")


def test_the_cli_port_turns_bd_failures_into_errors(monkeypatch, tmp_path) -> None:
    calls: list[list[str]] = []

    class Transport:
        def run(self, args, cwd, actor="", capture=False, text_input=None, **_):
            calls.append([*args])
            return Completed(1, "", "Error: boom")

    monkeypatch.setattr(bd, "run", Transport().run)
    port = BdCliPort(tmp_path, actor="dev/loader")
    with pytest.raises(backfill_bd.BdLoadError, match="boom"):
        port.close("bh-1", "done")
    with pytest.raises(backfill_bd.BdLoadError, match="bd export failed"):
        port.export_rows()
    assert calls[0][:2] == ["close", "bh-1"]
    assert calls[1] == ["export", "--all"]


# -- pre-state -----------------------------------------------------------------------------


def test_pre_state_export_needs_a_path_and_writes_a_restorable_jsonl(tmp_path) -> None:
    store = FakeBd(LIVE_ROWS)
    with pytest.raises(backfill_bd.BdLoadError, match="no pre-state path"):
        loader(store).export_pre_state(())
    path = tmp_path / "plan.pre-state.jsonl"
    assert loader(store, pre_state_path=path).export_pre_state(()) == str(path)
    assert [json.loads(line)["id"] for line in path.read_text().splitlines()] == ["pr-a1", "pr-b1"]
    assert backfill_bd.default_pre_state_path(tmp_path / "p.bdlg") == tmp_path / "p.pre-state.jsonl"


# -- rollback ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "event",
    [
        mint(
            "create", "bf-9", {"type": "task", "fields": {"title": "New"}}, "2026-07-01T00:00:00Z"
        ),
        mint("set", "pr-a1", {"field": "title", "value": "Renamed"}, "2026-07-01T00:00:00Z"),
        mint("set", "pr-a1", {"field": "metadata.team", "value": "edge"}, "2026-07-01T00:00:00Z"),
        mint("label_add", "pr-a1", {"label": "new:1"}, "2026-07-01T00:00:00Z"),
        mint("label_remove", "pr-a1", {"label": "area:x"}, "2026-07-01T00:00:00Z"),
        mint(
            "edge_add",
            "pr-a1",
            {"type": "related", "target": {"group": GROUP, "id": "pr-b1"}},
            "2026-07-01T00:00:00Z",
        ),
        mint(
            "edge_remove",
            "pr-a1",
            {"type": "blocks", "target": {"group": GROUP, "id": "pr-b1"}},
            "2026-07-01T00:00:00Z",
        ),
        mint("phase", "pr-b1", {"phase": "open"}, "2026-07-01T00:00:00Z"),
        mint("retire", "pr-a1", {"reason": "superseded"}, "2026-07-01T00:00:00Z"),
    ],
    ids=lambda event: str(event.kind) + ":" + event.bead.id,
)
def test_each_kind_is_undone_by_its_inverse(event: beadlog.BeadEvent) -> None:
    store = FakeBd(LIVE_ROWS)
    bd_loader = loader(store)
    before = bd_loader.export()
    prior = beadlog.fold(before).get(event.bead)
    bd_loader.load([event], dry_run=False)
    assert store.writes, "the event changed nothing"
    inverses = inverse_of(event, prior, stamp=Stamper("2026-10-31T00:00:00Z", [event]), scope="t")
    assert inverses
    bd_loader.load(inverses, dry_run=False)
    after = beadlog.fold(bd_loader.export())[event.bead]
    if prior is None:  # a create is undone by retiring the bead, never by deleting it
        row = store.rows[event.bead.id]
        assert row["status"] == "closed" and row["close_reason"] == ROLLBACK_REASON
        assert after.phase == "retired" and "delete" not in {op for op, _ in store.writes}
        return
    assert after.type == prior.type
    assert dict(after.fields) == dict(prior.fields)
    assert after.labels == prior.labels and after.edges == prior.edges
    assert backfill_bd._status(after.phase) == backfill_bd._status(prior.phase)


# -- the apply envelope end to end ---------------------------------------------------------


class CreatePass:
    """Proposes brand-new beads, stamped with the identity and origin labels."""

    name = "bd-loader-test"
    schema_version = "1"
    invalidating_fields = frozenset({"exists", "fields.title", "labels"})
    drift_applicable = False

    def __init__(self, titles: Mapping[str, str]) -> None:
        self.titles = dict(titles)

    def propose(self, baseline, *, extracted_at, judge):
        creates = [
            mint("create", iid, {"type": "task", "fields": {"title": title}}, extracted_at)
            for iid, title in sorted(self.titles.items())
        ]
        return stamp_labels(creates, [*TRIPLET, *origin_labels("docs")])


class Sink:
    def __init__(self) -> None:
        self.entries: list[Any] = []

    def record(self, entry: Any) -> None:
        self.entries.append(entry)


def _engine() -> BackfillEngine:
    return BackfillEngine(groups=[beadlog.Group(GROUP, "bh")], clock=Ticks(5000))


def test_the_envelope_applies_converges_and_proves_idempotency(tmp_path) -> None:
    store = FakeBd(LIVE_ROWS, identity_labels=TRIPLET)
    bd_loader = loader(store, pre_state_path=tmp_path / "pre.jsonl")
    eng, pass_ = (
        _engine(),
        CreatePass({"bf-1": "Reconstructed gamma work", "bf-2": "Reconstructed delta work"}),
    )
    audit = eng.apply(
        eng.plan(pass_, bd_loader), bd_loader, bd_loader, Sink(), pre_state=bd_loader, pass_=pass_
    )
    assert audit.state is AuditState.APPLIED
    assert audit.recovery.pre_state_artifact == str(tmp_path / "pre.jsonl")
    assert {"origin:backfill", "source:docs"} <= set(store.rows["bf-1"]["labels"])
    assert eng.plan(pass_, bd_loader).changes == 0


def test_a_failed_apply_rolls_back_by_retiring_never_deleting(tmp_path) -> None:
    store = FakeBd(LIVE_ROWS, identity_labels=TRIPLET)
    bd_loader = loader(store, pre_state_path=tmp_path / "pre.jsonl")
    eng, pass_ = (
        _engine(),
        CreatePass({"bf-1": "Reconstructed gamma work", "bf-2": "Reconstructed delta work"}),
    )
    plan = eng.plan(pass_, bd_loader)
    store.fail = lambda op, bead: op == "import" and bead == "bf-2"
    sink = Sink()
    with pytest.raises(BackfillError, match="apply failed and was audited"):
        eng.apply(plan, bd_loader, bd_loader, sink, pre_state=bd_loader, pass_=pass_)
    row = store.rows["bf-1"]
    assert row["status"] == "closed" and row["close_reason"] == ROLLBACK_REASON
    assert "origin:backfill" not in row["labels"]
    assert "bf-2" not in store.rows
    assert not any(op == "delete" for op, _ in store.writes)
    assert sink.entries[-1].state is AuditState.ROLLED_BACK


def test_actorless_comments_reload_without_duplicating() -> None:
    """bd credits a comment with no BeadLog actor to the loader's seat (or leaves the author
    empty on import); the reader still recognizes it, so a reload adds nothing."""
    store = FakeBd(LIVE_ROWS)
    events = [
        mint("create", "bf-5", {"type": "task", "fields": {"title": "T"}}, "2026-08-01T00:00:00Z"),
        mint("comment", "bf-5", {"body": "anonymous on a new bead"}, "2026-08-02T00:00:00Z"),
        mint("comment", "pr-a1", {"body": "anonymous on an old bead"}, "2026-08-03T00:00:00Z"),
    ]
    bd_loader = loader(store)
    bd_loader.load(events, dry_run=False)
    writes = list(store.writes)
    bd_loader.load(events, dry_run=False)
    assert store.writes == writes
    assert {e.id for e in events} <= {e.id for e in bd_loader.export()}
    assert store.rows["pr-a1"]["comments"][-1]["author"] == "dev/loader"


def test_a_create_on_an_existing_bead_cannot_set_an_import_only_field() -> None:
    store = FakeBd(LIVE_ROWS)
    create = mint(
        "create", "pr-a1", {"type": "task", "fields": {"title": "A", "owner": "o@x"}}, None
    )
    report = loader(store).load([create], dry_run=False)
    assert create.id in by_code(report)["field.unsupported"]
    assert store.writes == []


@pytest.mark.parametrize("enabled", [False, True])
def test_a_bead_scope_tombstone_deletes_only_with_the_opt_in(enabled: bool) -> None:
    store = FakeBd(LIVE_ROWS)
    erase = mint("tombstone", "pr-b1", {"scope": "bead", "reason": "pii"}, "2026-09-01T00:00:00Z")
    label = mint("label_add", "pr-b1", {"label": "late"}, "2026-09-02T00:00:00Z")
    report = loader(store, delete_enabled=enabled).load([erase, label], dry_run=False)
    codes = by_code(report)
    assert erase.id in codes["erasure.incomplete"]  # Dolt history keeps the content either way
    assert label.id in codes["x-bd.erased"]
    assert ("pr-b1" in store.rows) is not enabled
    assert store.writes == ([("delete", "pr-b1")] if enabled else [])
