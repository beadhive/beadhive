"""Loader-conformance kit: the cases any BeadLog store loader must pass.

A store loader maps events onto one store and reads the store back
(``docs/design/beadlog-format-adr.md`` sections 7-9). This module ships the cases that pin the
contract, so every loader package runs the same ones against its own store::

    import pytest
    from beadlog import testing

    @pytest.mark.parametrize("case", testing.conformance_cases(), ids=lambda case: case.name)
    def test_conformance(case):
        case.run(make_empty_store)          # () -> a fresh, empty store of the kind under test

The cases drive archives (:func:`conformance_archive`) through a *driver*,
``driver(store, source, *, dry_run) -> loss report``. The default, :func:`load_archive`, opens
the archive with :class:`~beadlog.ArchiveReader` (refusing unknown required capabilities and
tampered members), hands the events to ``store.load`` and adds the report's ``archive`` block.
A loader with its own archive entry point passes that as ``driver`` instead.

Cases: export -> load -> re-export equivalence, idempotent reload, refusal of unknown required
capabilities and of tampered members (nothing is loaded), and loss-report completeness (no
silent drops; capabilities the store lacks show up as losses; a dry run reports what the apply
reports). :class:`InMemoryStore` is the lossless reference store the kit itself is tested on.
Stdlib only; no test framework is imported.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import IO, Any, Protocol, runtime_checkable

from ._fold import fold
from ._ids import identity_projection, mint_event
from ._manifest import (
    KNOWN_CAPABILITIES,
    Generator,
    Group,
    LoaderCapability,
    UnsupportedCapabilityError,
)
from ._merge import union_merge
from ._model import BeadEvent, BeadRef, EventKind
from ._order import ordering_key, sort_events
from .archive import (
    ArchiveReader,
    IntegrityError,
    ReadLimits,
    read_archive,
    write_archive,
)

GROUP_ID = "urn:uuid:7c0f5b1e-0f6a-4a52-9c5e-2f4d6a8b1c3d"
GROUP = Group(GROUP_ID, "conformance", ("conformance/core",), "Loader-conformance fixture group.")
GENERATOR = Generator("beadlog.testing", "0.1.0")
EXTRACTED_AT = "2026-10-31T00:00:00Z"
CORE_LOSS_CODES = frozenset(
    {
        "valid-time.degraded",
        "valid-time.missing",
        "precision.lost",
        "recorded-time.reassigned",
        "recorded-time.null",
        "recorded-time.monotone-violation",
        "field.unsupported",
        "phase.approximated",
        "capability.ignored",
        "erasure.incomplete",
        "edge.dangling-target",
        "provenance.dropped",
    }
)
# Losses that change what a bead folds to (as does any dropped event or x- code); beads touched
# by one are exempt from the fold comparison of the round-trip case: the loader said it cannot
# keep that state.
STATE_LOSS_CODES = frozenset({"field.unsupported", "phase.approximated", "edge.dangling-target"})
_LOSS_CODE = re.compile(r"^([a-z][a-z-]*\.[a-z][a-z-]*|x-[a-z0-9.-]+)$")
_POINTER = re.compile(r"^(/([^~/]|~[01])*)*$")
_EVENT_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DISPOSITIONS = frozenset({"degraded", "reassigned", "approximated", "dropped"})
_LOADER_CAPABILITY = re.compile(r"^[a-z][a-z0-9:._-]*$")


class ConformanceFailure(AssertionError):
    """A loader broke the contract; the message says which clause."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ConformanceFailure(message)


@runtime_checkable
class ConformanceStore(Protocol):
    """What the kit needs of a store loader (the ``StoreLoader`` shape, structurally)."""

    @property
    def name(self) -> str: ...

    @property
    def version(self) -> str: ...

    @property
    def capabilities(self) -> frozenset[str]: ...

    def export(self) -> Iterable[BeadEvent]: ...

    def load(self, events: Sequence[BeadEvent], *, dry_run: bool) -> Mapping[str, Any]: ...


StoreFactory = Callable[[], ConformanceStore]
LoadDriver = Callable[..., Mapping[str, Any]]


class InMemoryStore:
    """The lossless reference store: keeps every event as written, merges on load, applies
    tombstones, reports no losses. Declares every loader capability except the metadata fallback
    and ``erase-logical`` (it erases physically, so it declares ``erase``)."""

    name = "in-memory"
    version = "0.1.0"
    capabilities = frozenset(str(item) for item in LoaderCapability) - {
        "metadata-fallback",
        "erase-logical",
    }

    def __init__(self) -> None:
        self._events: dict[str, BeadEvent] = {}

    def export(self) -> tuple[BeadEvent, ...]:
        return sort_events(self._events.values())

    def load(self, events: Sequence[BeadEvent], *, dry_run: bool) -> Mapping[str, Any]:
        batch = tuple(events)
        merged = union_merge([self._events.values(), batch])
        if not dry_run:
            self._events = {event.id: event for event in merged}
        return {
            "loss_report_version": "0.1.0",
            "loader": {
                "name": self.name,
                "version": self.version,
                "capabilities": sorted(self.capabilities),
            },
            "target": "memory:",
            "mode": "dry-run" if dry_run else "apply",
            "summary": {
                "events": len(batch),
                "loaded": len(batch),
                "degraded": 0,
                "dropped": 0,
                "by_code": {},
            },
            "losses": [],
        }


def load_archive(
    store: ConformanceStore,
    source: str | IO[bytes],
    *,
    dry_run: bool = False,
    implemented_capabilities: Iterable[str] = KNOWN_CAPABILITIES,
    limits: ReadLimits | None = None,
) -> dict[str, Any]:
    """The reference load driver: refuse (raise) on anything the reader refuses, otherwise load
    the archive's events into ``store`` and return its loss report with the ``archive`` block
    (``format_version``, ``manifest_sha256``) filled in, since a store sees events, not the
    manifest."""
    with ArchiveReader(
        source, limits=limits, implemented_capabilities=implemented_capabilities
    ) as reader:
        reader.verify()
        events = tuple(reader.events())
        report = dict(store.load(events, dry_run=dry_run))
        report["archive"] = {
            "format_version": reader.manifest.format_version,
            "manifest_sha256": reader.manifest_sha256,
        }
    return report


# -- the fixture ---------------------------------------------------------------------------


def _bead(name: str) -> BeadRef:
    return BeadRef(GROUP_ID, f"conf-{name}")


def conformance_events() -> tuple[BeadEvent, ...]:
    """The fixture history: three beads covering every facet and time shape (exact, day
    precision, null recorded time), an erased comment (only its tombstone is archived), a
    retirement with a successor, a deleted bead and a checkpoint."""

    def mint(
        kind: str, name: str, data: Mapping[str, Any], at: str | None, rec: str | None, **kw: Any
    ) -> BeadEvent:
        ref = kw.pop("ref", f"{kind}:{name}:{at}")
        return mint_event(
            kind,
            _bead(name),
            data,
            occurred_at=at,
            recorded_at=rec,
            extracted_at=EXTRACTED_AT,
            source=("conformance", ref),
            provenance=kw.pop("provenance", "observed"),
            actor=kw.pop("actor", "dev/conformance"),
            **kw,
        )

    a_create = mint(
        "create",
        "a",
        {"type": "task", "fields": {"title": "Alpha", "priority": 2}},
        "2026-01-01T10:00:00Z",
        "2026-01-01T10:00:02Z",
    )
    a_title = mint(
        "set",
        "a",
        {"field": "title", "value": "Alpha, renamed"},
        "2026-01-02T09:30:00.25Z",
        "2026-01-02T09:30:01Z",
    )
    a_label = mint(
        "label_add", "a", {"label": "area:format"}, "2026-01-02T10:00:00Z", "2026-01-02T10:00:01Z"
    )
    a_edge = mint(
        "edge_add",
        "a",
        {"type": "blocks", "target": _bead("b").to_json()},
        "2026-01-03T08:00:00Z",
        "2026-01-03T08:00:01Z",
    )
    a_note = mint(
        "comment",
        "a",
        {"body": "Naive cafe: check"},
        "2026-01-04T08:00:00Z",
        "2026-01-04T08:00:01Z",
    )
    a_secret = mint(
        "comment",
        "a",
        {"body": "content that must not survive"},
        "2026-01-04T09:00:00Z",
        "2026-01-04T09:00:01Z",
    )
    a_clear = mint(
        "set",
        "a",
        {"field": "assignee", "value": None},
        "2026-01-05T08:00:00Z",
        "2026-01-05T08:00:01Z",
    )
    a_phase = mint(
        "phase", "a", {"phase": "in_progress"}, "2026-01-06T08:00:00Z", "2026-01-06T08:00:01Z"
    )
    erase = mint(
        "tombstone",
        "a",
        {"scope": "events", "events": [a_secret.id], "reason": "pii"},
        "2026-01-07T08:00:00Z",
        "2026-01-07T08:00:01Z",
    )
    b_create = mint(
        "create",
        "b",
        {"type": "task", "fields": {"title": "Beta"}},
        "2026-01-01T00:00:00Z",
        None,
        provenance="inferred",
        occurred_precision="day",
        confidence=0.75,
        actor=None,
    )
    b_phase = mint(
        "phase",
        "b",
        {"phase": "closed", "reason": "done"},
        "2026-01-09T12:00:00Z",
        "2026-01-09T12:00:03Z",
    )
    b_tag = mint("label_add", "b", {"label": "tmp"}, "2026-01-09T13:00:00Z", "2026-01-09T13:00:01Z")
    b_untag = mint(
        "label_remove", "b", {"label": "tmp"}, "2026-01-09T14:00:00Z", "2026-01-09T14:00:01Z"
    )
    b_rel = mint(
        "edge_add",
        "b",
        {"type": "related", "target": _bead("c").to_json()},
        "2026-01-09T15:00:00Z",
        "2026-01-09T15:00:01Z",
    )
    b_unrel = mint(
        "edge_remove",
        "b",
        {"type": "related", "target": _bead("c").to_json()},
        "2026-01-09T16:00:00Z",
        "2026-01-09T16:00:01Z",
    )
    c_create = mint(
        "create",
        "c",
        {"type": "decision", "fields": {"title": "Gamma"}},
        "2026-01-10T08:00:00Z",
        "2026-01-10T08:00:01Z",
    )
    c_retire = mint(
        "retire",
        "c",
        {"successor": _bead("a").to_json(), "reason": "superseded"},
        "2026-01-11T08:00:00Z",
        "2026-01-11T08:00:01Z",
    )
    d_create = mint(
        "create",
        "d",
        {"type": "task", "fields": {"title": "Mistake"}},
        "2026-01-12T08:00:00Z",
        "2026-01-12T08:00:01Z",
    )
    d_delete = mint(
        "delete",
        "d",
        {"reason": "created in error"},
        "2026-01-12T08:30:00Z",
        "2026-01-12T08:30:01Z",
    )

    history = [a_create, a_title, a_label, a_edge, a_note, a_clear, a_phase]
    state = fold(history)[_bead("a")]
    last = max(history, key=ordering_key)
    checkpoint = mint_event(
        "checkpoint",
        _bead("a"),
        {
            "state": state.to_checkpoint_state(),
            "covers_through": {
                "occurred_at": last.occurred_at,
                "recorded_at": last.recorded_at,
                "id": last.id,
            },
        },
        occurred_at=last.occurred_at,
        recorded_at=last.recorded_at,
        extracted_at=EXTRACTED_AT,
        source=("conformance", "checkpoint:a"),
        provenance="derived",
    )
    return (
        a_create, a_title, a_label, a_edge, a_note, a_secret, a_clear, a_phase, erase,
        b_create, b_phase, b_tag, b_untag, b_rel, b_unrel, c_create, c_retire, d_create, d_delete,
        checkpoint,
    )  # fmt: skip


def conformance_archive(*, index: bool = True) -> bytes:
    """The fixture packed as a ``.bdlg`` (the erased comment is dropped by the writer)."""
    buffer = io.BytesIO()
    write_archive(buffer, conformance_events(), groups=[GROUP], generator=GENERATOR, index=index)
    return buffer.getvalue()


def _loaded_events() -> tuple[BeadEvent, ...]:
    return read_archive(io.BytesIO(conformance_archive()))[1]


def _rewrite_member(archive: bytes, path: str, change: Callable[[bytes], bytes]) -> bytes:
    """Copy an archive replacing one member's bytes (for the tamper cases). Keeps entry order
    and compression; recomputes nothing, so the manifest goes stale on purpose."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(archive)) as source, zipfile.ZipFile(out, "w") as target:
        for info in source.infolist():
            data = source.read(info)
            if info.filename == path:
                data = change(data)
            target.writestr(info, data, compress_type=info.compress_type)
    return out.getvalue()


def archive_with_unknown_required_capability() -> bytes:
    """The fixture whose manifest additionally requires a capability no v0.1 reader has."""

    def bump(data: bytes) -> bytes:
        manifest = json.loads(data)
        manifest["capabilities"]["required"].append("kind:alias")
        return (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode()

    return _rewrite_member(conformance_archive(), "MANIFEST.json", bump)


def archive_with_tampered_part() -> bytes:
    """The fixture with one byte of an events part changed behind the manifest's back."""
    flipped = lambda data: data.replace(b"Alpha", b"Omega", 1)  # noqa: E731 - same length
    return _rewrite_member(conformance_archive(), "events/conformance/part-0000.jsonl", flipped)


# -- the cases -----------------------------------------------------------------------------


def _fresh(factory: StoreFactory) -> ConformanceStore:
    store = factory()
    _require(isinstance(store, ConformanceStore), "the factory must return a store loader")
    _require(not tuple(store.export()), "the factory must return an empty store")
    return store


def _apply(
    driver: LoadDriver, store: ConformanceStore, archive: bytes, dry_run: bool
) -> Mapping[str, Any]:
    return driver(store, io.BytesIO(archive), dry_run=dry_run)


def _losses(report: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return list(report["losses"])


def _dropped_ids(report: Mapping[str, Any]) -> set[str]:
    return {loss["event_id"] for loss in _losses(report) if loss["disposition"] == "dropped"}


def _check_round_trip(factory: StoreFactory, driver: LoadDriver) -> None:
    store, loaded = _fresh(factory), _loaded_events()
    report = _apply(driver, store, conformance_archive(), dry_run=False)
    exported = tuple(store.export())
    dropped = _dropped_ids(report)
    by_id = {event.id: event for event in loaded}
    kept = {event.id for event in exported}
    _require(len(kept) == len(exported), "export returned the same event id twice")
    _require(
        not kept - set(by_id),
        f"export holds events that were never loaded: {sorted(kept - set(by_id))[:3]}",
    )
    expected = {event.id for event in loaded if event.kind is not EventKind.CHECKPOINT} - dropped
    _require(
        expected <= kept,
        f"events neither re-exported nor reported as dropped: {sorted(expected - kept)[:3]}",
    )
    for event in exported:
        original = by_id[event.id]
        _require(
            identity_projection(event) == identity_projection(original),
            f"event {event.id} came back with different identity material",
        )
    lossy_beads = {
        by_id[loss["event_id"]].bead
        for loss in _losses(report)
        if loss["event_id"] in by_id
        and (
            loss["disposition"] == "dropped"
            or loss["code"] in STATE_LOSS_CODES
            or loss["code"].startswith("x-")
        )
    }
    before, after = fold(loaded), fold(exported)
    for bead, state in before.items():
        if bead in lossy_beads:
            continue
        _require(after.get(bead) == state, f"{bead.id} folds differently after load and re-export")


def _fingerprint(store: ConformanceStore) -> list[str]:
    return sorted(
        event.id + json.dumps(identity_projection(event), sort_keys=True)
        for event in store.export()
    )


def _check_idempotent(factory: StoreFactory, driver: LoadDriver) -> None:
    store, archive = _fresh(factory), conformance_archive()
    _apply(driver, store, archive, dry_run=False)
    first = _fingerprint(store)
    again = _apply(driver, store, archive, dry_run=False)
    second = _fingerprint(store)
    _require(first == second, "loading the same archive again changed the store")
    _require(again["mode"] == "apply", "the reload's report must say mode apply")


def _check_capability_refusal(factory: StoreFactory, driver: LoadDriver) -> None:
    store = _fresh(factory)
    archive = archive_with_unknown_required_capability()
    for dry_run in (True, False):
        try:
            _apply(driver, store, archive, dry_run=dry_run)
        except UnsupportedCapabilityError as exc:
            _require("kind:alias" in exc.missing, "the refusal must name the unknown capability")
        else:
            raise ConformanceFailure("an archive requiring an unknown capability was not refused")
    _require(not tuple(store.export()), "a refused archive must leave the store untouched")


def _check_integrity_refusal(factory: StoreFactory, driver: LoadDriver) -> None:
    store = _fresh(factory)
    try:
        _apply(driver, store, archive_with_tampered_part(), dry_run=False)
    except IntegrityError:
        pass
    else:
        raise ConformanceFailure("an archive with a tampered part was not refused")
    _require(not tuple(store.export()), "a refused archive must leave the store untouched")


def _check_report_shape(
    report: Mapping[str, Any], store: ConformanceStore, mode: str, ids: set[str]
) -> None:
    for key in ("loss_report_version", "archive", "loader", "target", "mode", "summary", "losses"):
        _require(key in report, f"loss report lacks {key!r}")
    _require(str(report["loss_report_version"]).startswith("0.1."), "unknown loss_report_version")
    _require(report["mode"] == mode, f"report mode is {report['mode']!r}, expected {mode!r}")
    _require(bool(report["target"]), "loss report names no target")
    archive = report["archive"]
    _require(
        _SHA256.fullmatch(str(archive.get("manifest_sha256"))) is not None
        and str(archive.get("format_version")).startswith("0.1."),
        "loss report archive block is malformed",
    )
    loader = report["loader"]
    _require(
        loader.get("name") == store.name and loader.get("version") == store.version,
        "loss report names a different loader than the store",
    )
    declared = list(loader.get("capabilities", ()))
    _require(
        not {"erase", "erase-logical"} <= set(declared) | set(store.capabilities),
        "a loader declares at most one of 'erase' and 'erase-logical'",
    )
    _require(
        set(declared) == set(store.capabilities)
        and len(declared) == len(set(declared))
        and all(_LOADER_CAPABILITY.fullmatch(item) for item in declared),
        "loss report capabilities differ from the store's",
    )
    for loss in _losses(report):
        where = f"loss {loss.get('code')!r} on {loss.get('event_id')!r}"
        _require(loss.get("event_id") in ids, f"{where}: names an event that was not loaded")
        _require(
            _LOSS_CODE.fullmatch(str(loss.get("code"))) is not None, f"{where}: malformed code"
        )
        code = str(loss["code"])
        _require(code in CORE_LOSS_CODES or code.startswith("x-"), f"{where}: unknown core code")
        _require(
            _POINTER.fullmatch(str(loss.get("member"))) is not None, f"{where}: bad member pointer"
        )
        _require(loss.get("disposition") in _DISPOSITIONS, f"{where}: bad disposition")
        _require(
            loss.get("stored_as") is None or isinstance(loss["stored_as"], str),
            f"{where}: bad stored_as",
        )


def _check_loss_report(factory: StoreFactory, driver: LoadDriver) -> None:
    loaded = _loaded_events()
    ids = {event.id for event in loaded}
    archive = conformance_archive()

    preview_store = _fresh(factory)
    preview = _apply(driver, preview_store, archive, dry_run=True)
    _require(not tuple(preview_store.export()), "a dry run must not write to the store")

    store = _fresh(factory)
    report = _apply(driver, store, archive, dry_run=False)
    _check_report_shape(preview, preview_store, "dry-run", ids)
    _check_report_shape(report, store, "apply", ids)

    def canonical(loss_report: Mapping[str, Any]) -> list[str]:
        return sorted(json.dumps(dict(loss), sort_keys=True) for loss in _losses(loss_report))

    _require(
        canonical(preview) == canonical(report), "a dry run and the apply report different losses"
    )
    _require(
        preview["summary"] == report["summary"], "a dry run and the apply summarise differently"
    )

    summary = report["summary"]
    _require(
        summary["events"] == len(loaded),
        f"summary.events is {summary['events']}, archive has {len(loaded)}",
    )
    _require(
        summary["loaded"] + summary["dropped"] == summary["events"],
        "summary.loaded + summary.dropped must equal summary.events",
    )
    dropped = _dropped_ids(report)
    _require(summary["dropped"] == len(dropped), "summary.dropped differs from the dropped losses")
    by_code: dict[str, int] = {}
    for loss in _losses(report):
        by_code[loss["code"]] = by_code.get(loss["code"], 0) + 1
    for code, count in by_code.items():
        _require(
            summary.get("by_code", {}).get(code, 0) >= count,
            f"summary.by_code undercounts {code!r}",
        )

    exported = {event.id for event in store.export()}
    _require(not dropped & exported, "an event reported as dropped is still in the store")
    silent = {
        event.id
        for event in loaded
        if event.kind is not EventKind.CHECKPOINT
        and event.id not in exported
        and event.id not in dropped
    }
    _require(
        not silent, f"events silently dropped (neither stored nor reported): {sorted(silent)[:3]}"
    )

    codes = {loss["code"] for loss in _losses(report)}
    has = lambda *names: bool(codes & set(names))  # noqa: E731
    caps = set(store.capabilities)
    if "valid-time" not in caps:
        _require(
            has("valid-time.degraded", "valid-time.missing"),
            "a store without valid-time must report valid-time losses",
        )
    if not caps & {"recorded-time", "recorded-time-monotone-load"}:
        _require(
            has("recorded-time.reassigned", "recorded-time.null"),
            "a store that cannot keep recorded time must report recorded-time losses",
        )
    tombstones = {event.id for event in loaded if event.kind is EventKind.TOMBSTONE} - dropped
    # `erase-logical` (format ADR A1) keeps bytes in the store's history, so like a store with no
    # erase at all it must flag each tombstone; only a physical `erase` may stay silent.
    if tombstones and "erase" not in caps:
        flagged = {
            loss["event_id"] for loss in _losses(report) if loss["code"] == "erasure.incomplete"
        }
        _require(
            tombstones <= flagged,
            "a store without a physical erase must report erasure.incomplete for each tombstone",
        )


@dataclass(frozen=True)
class ConformanceCase:
    """One contract clause: ``run(factory, driver)`` raises :class:`ConformanceFailure`."""

    name: str
    summary: str
    check: Callable[[StoreFactory, LoadDriver], None]

    def run(self, factory: StoreFactory, driver: LoadDriver = load_archive) -> None:
        self.check(factory, driver)


CASES: tuple[ConformanceCase, ...] = (
    ConformanceCase(
        "export_load_reexport_equivalence",
        "re-exporting a loaded archive returns every kept event with its original id and identity",
        _check_round_trip,
    ),
    ConformanceCase(
        "idempotent_reload",
        "loading the same archive twice leaves the store as after the first load",
        _check_idempotent,
    ),
    ConformanceCase(
        "unknown_required_capability_refused",
        "an archive requiring a capability the reader lacks is refused and nothing is loaded",
        _check_capability_refusal,
    ),
    ConformanceCase(
        "tampered_archive_refused",
        "an archive whose part no longer matches the manifest is refused and nothing is loaded",
        _check_integrity_refusal,
    ),
    ConformanceCase(
        "loss_report_completeness",
        "every load emits a well-formed report; nothing is dropped silently; a dry run matches",
        _check_loss_report,
    ),
)


def conformance_cases() -> tuple[ConformanceCase, ...]:
    """The cases every store loader must pass."""
    return CASES


@dataclass(frozen=True)
class CaseResult:
    case: ConformanceCase
    error: BaseException | None

    @property
    def passed(self) -> bool:
        return self.error is None


def run_conformance(
    factory: StoreFactory,
    driver: LoadDriver = load_archive,
    cases: Iterable[ConformanceCase] | None = None,
) -> list[CaseResult]:
    """Run ``cases`` (default: all) and collect results instead of stopping at the first."""
    results = []
    for case in CASES if cases is None else cases:
        try:
            case.run(factory, driver)
        except ConformanceFailure as exc:
            results.append(CaseResult(case, exc))
        else:
            results.append(CaseResult(case, None))
    return results


def assert_conformant(factory: StoreFactory, driver: LoadDriver = load_archive) -> None:
    """Raise :class:`ConformanceFailure` naming every failed case."""
    failed = [result for result in run_conformance(factory, driver) if not result.passed]
    if failed:
        raise ConformanceFailure(
            "; ".join(f"{result.case.name}: {result.error}" for result in failed)
        )


__all__ = [
    "CASES",
    "CORE_LOSS_CODES",
    "STATE_LOSS_CODES",
    "CaseResult",
    "ConformanceCase",
    "ConformanceFailure",
    "ConformanceStore",
    "InMemoryStore",
    "archive_with_tampered_part",
    "archive_with_unknown_required_capability",
    "assert_conformant",
    "conformance_archive",
    "conformance_cases",
    "conformance_events",
    "load_archive",
    "run_conformance",
]
