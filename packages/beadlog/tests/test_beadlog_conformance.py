"""The loader-conformance kit (``beadlog.testing``): the reference store passes every case, and
each case catches the loader fault it exists for."""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

import beadlog
from beadlog import testing


@pytest.mark.parametrize("case", testing.conformance_cases(), ids=lambda case: case.name)
def test_reference_store_passes_every_case(case: testing.ConformanceCase) -> None:
    case.run(testing.InMemoryStore)


def test_assert_conformant_and_results() -> None:
    testing.assert_conformant(testing.InMemoryStore)
    results = testing.run_conformance(testing.InMemoryStore)
    assert [result.case.name for result in results] == [c.name for c in testing.CASES]
    assert all(result.passed for result in results)
    assert len({case.name for case in testing.CASES}) == len(testing.CASES) == 5


def test_the_fixture_covers_every_kind_and_every_time_shape() -> None:
    manifest, events = beadlog.read_archive(io.BytesIO(testing.conformance_archive()))
    assert set(manifest.counts["by_kind"]) == {str(kind) for kind in beadlog.EventKind}
    assert any(e.recorded_at is None for e in events)
    assert any(e.occurred_precision is not None for e in events)
    assert any(e.occurred_at and "." in e.occurred_at for e in events)
    assert manifest.capabilities.required == ("tombstones",)
    # The erased comment is in the fixture's input but never in its archive.
    assert len(testing.conformance_events()) == len(events) + 1
    assert testing.conformance_archive() == testing.conformance_archive()


def test_tamper_fixtures_really_are_refused_by_the_reader() -> None:
    with pytest.raises(beadlog.UnsupportedCapabilityError):
        beadlog.ArchiveReader(io.BytesIO(testing.archive_with_unknown_required_capability()))
    with beadlog.ArchiveReader(io.BytesIO(testing.archive_with_tampered_part())) as reader:
        with pytest.raises(beadlog.PartHashMismatchError):
            list(reader.events())


def test_load_archive_fills_the_archive_block_and_refuses_before_loading() -> None:
    store = testing.InMemoryStore()
    report = testing.load_archive(store, io.BytesIO(testing.conformance_archive()), dry_run=True)
    assert report["archive"]["format_version"] == beadlog.FORMAT_VERSION
    assert len(report["archive"]["manifest_sha256"]) == 64
    assert not store.export()
    with pytest.raises(beadlog.UnsupportedCapabilityError):
        testing.load_archive(
            store,
            io.BytesIO(testing.conformance_archive()),
            implemented_capabilities=["checkpoints"],
        )


# -- faulty loaders: each must fail the clause it breaks ------------------------------------


def _failing(factory: testing.StoreFactory, driver: Any = testing.load_archive) -> set[str]:
    return {r.case.name for r in testing.run_conformance(factory, driver) if not r.passed}


class SilentDrop(testing.InMemoryStore):
    """Drops label events without a word."""

    def load(self, events: Sequence[beadlog.BeadEvent], *, dry_run: bool) -> Mapping[str, Any]:
        kept = [e for e in events if e.kind is not beadlog.EventKind.LABEL_ADD]
        report = dict(super().load(kept, dry_run=dry_run))
        report["summary"] = dict(report["summary"], events=len(events), loaded=len(events))
        return report


class RewritesIds(testing.InMemoryStore):
    """Re-mints ids on the way in (what the format forbids)."""

    def load(self, events: Sequence[beadlog.BeadEvent], *, dry_run: bool) -> Mapping[str, Any]:
        remade = [
            beadlog.BeadEvent.from_json(e.to_json() | {"id": "sha256:" + "ab" * 32})
            if e.kind is beadlog.EventKind.COMMENT
            else e
            for e in events
        ]
        report = dict(super().load(remade, dry_run=dry_run))
        return report


class DryRunWrites(testing.InMemoryStore):
    """Ignores ``dry_run``."""

    def load(self, events: Sequence[beadlog.BeadEvent], *, dry_run: bool) -> Mapping[str, Any]:
        return super().load(events, dry_run=False)


class NotIdempotent(testing.InMemoryStore):
    """Treats a reload as a replace and loses the comments."""

    def load(self, events: Sequence[beadlog.BeadEvent], *, dry_run: bool) -> Mapping[str, Any]:
        if not dry_run and self._events:
            self._events = {}
            events = [e for e in events if e.kind is not beadlog.EventKind.COMMENT]
        return super().load(events, dry_run=dry_run)


class LossyButQuiet(testing.InMemoryStore):
    """No valid time, no recorded time, no erase -- and reports nothing."""

    capabilities = frozenset({"metadata-fallback"})


class HonestlyLossy(testing.InMemoryStore):
    """Keeps current state in metadata, says so for every dated event, flags the tombstone,
    and drops (reporting it) the events it has no place for."""

    name = "honest-lossy"
    capabilities = frozenset({"metadata-fallback"})

    def _assess(self, events: Sequence[beadlog.BeadEvent]) -> dict[str, Any]:
        losses: list[dict[str, Any]] = []
        for event in events:
            if event.kind is beadlog.EventKind.CHECKPOINT:
                continue
            if event.occurred_at is not None:
                losses.append(_loss(event, "valid-time.degraded", "/occurred_at", "degraded"))
            if event.recorded_at is not None:
                losses.append(
                    _loss(event, "recorded-time.reassigned", "/recorded_at", "reassigned")
                )
            if event.kind is beadlog.EventKind.TOMBSTONE:
                losses.append(_loss(event, "erasure.incomplete", "", "degraded"))
            if event.kind is beadlog.EventKind.COMMENT:
                losses.append(_loss(event, "provenance.dropped", "", "dropped"))
        return {"losses": losses}

    def export(self) -> tuple[beadlog.BeadEvent, ...]:
        return tuple(e for e in super().export() if e.kind is not beadlog.EventKind.COMMENT)

    def load(self, events: Sequence[beadlog.BeadEvent], *, dry_run: bool) -> Mapping[str, Any]:
        batch = tuple(events)
        report = dict(super().load(batch, dry_run=dry_run))
        losses = self._assess(batch)["losses"]
        dropped = {loss["event_id"] for loss in losses if loss["disposition"] == "dropped"}
        by_code: dict[str, int] = {}
        for loss in losses:
            by_code[loss["code"]] = by_code.get(loss["code"], 0) + 1
        report["loader"] = dict(report["loader"], name=self.name)
        report["losses"] = losses
        report["summary"] = {
            "events": len(batch),
            "loaded": len(batch) - len(dropped),
            "degraded": len(losses) - len(dropped),
            "dropped": len(dropped),
            "by_code": by_code,
        }
        return report


def _loss(event: beadlog.BeadEvent, code: str, member: str, disposition: str) -> dict[str, Any]:
    return {
        "event_id": event.id,
        "code": code,
        "member": member,
        "disposition": disposition,
        "stored_as": None,
    }


def test_a_loader_that_silently_drops_events_is_caught() -> None:
    failed = _failing(SilentDrop)
    assert "loss_report_completeness" in failed
    assert "export_load_reexport_equivalence" in failed


def test_a_loader_that_rewrites_ids_is_caught() -> None:
    assert "export_load_reexport_equivalence" in _failing(RewritesIds)


def test_a_dry_run_that_writes_is_caught() -> None:
    assert _failing(DryRunWrites) == {"loss_report_completeness"}


def test_a_non_idempotent_reload_is_caught() -> None:
    assert _failing(NotIdempotent) == {"idempotent_reload"}


def test_capabilities_the_store_lacks_must_show_up_as_losses() -> None:
    assert _failing(LossyButQuiet) == {"loss_report_completeness"}


def test_a_lossy_loader_that_reports_honestly_conforms() -> None:
    assert _failing(HonestlyLossy) == set()


def test_a_driver_that_loads_anyway_fails_the_refusal_cases() -> None:
    def careless(
        store: testing.ConformanceStore, source: Any, *, dry_run: bool
    ) -> Mapping[str, Any]:
        _, events = _read(source)
        return {**store.load(events, dry_run=dry_run), "archive": _ARCHIVE}

    failed = _failing(testing.InMemoryStore, careless)
    assert {"unknown_required_capability_refused", "tampered_archive_refused"} <= failed


_ARCHIVE = {"format_version": "0.1.0", "manifest_sha256": "0" * 64}


def _read(source: Any) -> tuple[None, tuple[beadlog.BeadEvent, ...]]:
    """Read without verifying or checking capabilities: events straight from the parts."""
    events = []
    with zipfile.ZipFile(source) as archive:
        for name in archive.namelist():
            if name.startswith(("events/", "checkpoints/")):
                for line in archive.read(name).splitlines():
                    events.append(beadlog.BeadEvent.from_json(json.loads(line)))
    return None, tuple(events)


def test_malformed_reports_are_named() -> None:
    class NoTarget(testing.InMemoryStore):
        def load(self, events: Sequence[beadlog.BeadEvent], *, dry_run: bool) -> Mapping[str, Any]:
            report = dict(super().load(events, dry_run=dry_run))
            report["target"] = ""
            return report

    class BadCode(testing.InMemoryStore):
        def load(self, events: Sequence[beadlog.BeadEvent], *, dry_run: bool) -> Mapping[str, Any]:
            report = dict(super().load(events, dry_run=dry_run))
            first = next(iter(events))
            report["losses"] = [_loss(first, "made.up", "", "degraded")]
            return report

    for store in (NoTarget, BadCode):
        with pytest.raises(testing.ConformanceFailure):
            testing.assert_conformant(store)


def test_the_factory_must_return_an_empty_store() -> None:
    def used() -> testing.ConformanceStore:
        store = testing.InMemoryStore()
        testing.load_archive(store, io.BytesIO(testing.conformance_archive()))
        return store

    with pytest.raises(testing.ConformanceFailure, match="empty"):
        testing.CASES[0].run(used)
