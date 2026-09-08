"""Exact identity and real public-source coverage for the operator API."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from beadhive import (
    operator_contract,
    operator_feed,
    operator_sources,
    public_readers,
    run_journal,
    state_stream,
)
from beadhive.agent_run_summary import Freshness
from beadhive.public_readers import AgentRunSnapshot, Coverage

NOW = datetime(2026, 8, 24, tzinfo=UTC).isoformat().replace("+00:00", "Z")
DIGEST = "sha256:" + "a" * 64


def _entry(*, repo: str = "beadhive", prefix: str = "bh") -> dict[str, str]:
    return {
        "provider": "github",
        "org": "beadhive",
        "repo": repo,
        "prefix": prefix,
        "kind": "org-native",
    }


def _cfg(*entries: dict[str, str]) -> dict:
    return {
        "managed_repos": list(entries or (_entry(),)),
        "git_workspace": {"hive_match": "flexible"},
    }


def _runtime(host_id: str = "host-1", source_id: str = "runtime") -> AgentRunSnapshot:
    return AgentRunSnapshot(
        host_id=host_id,
        source_id=source_id,
        revision="opaque:runtime",
        summaries=(),
        coverage=Coverage.UNKNOWN,
        coverage_reason="source_missing",
        freshness=Freshness(detail="source missing; writer coverage unknown"),
    )


def _snapshot(hive: str = "github/beadhive/beadhive") -> state_stream.ProviderSnapshot:
    issue = state_stream.StreamIssue(
        id="bh-1",
        hive=hive,
        issue_type="task",
        status="open",
        priority="P1",
        title="Operator source",
        updated_at=NOW,
    )
    return state_stream.ProviderSnapshot(
        scope="hive", revision="opaque:beads", as_of=NOW, issues=(issue,)
    )


class Provider:
    def __init__(self, snapshot: state_stream.ProviderSnapshot | None = None) -> None:
        self.snapshot = snapshot or _snapshot()
        self.requests: list[state_stream.StreamRequest] = []

    def refresh(self, request: state_stream.StreamRequest) -> state_stream.ProviderSnapshot:
        self.requests.append(request)
        return self.snapshot


def _sources(tmp_path: Path, *, cfg: dict | None = None, provider: Provider | None = None):
    return operator_sources.OperatorSources(
        cfg=cfg or _cfg(),
        host_id="host-1",
        provider=provider or Provider(),
        summary_reader=lambda _path, host, source: _runtime(host, source),
        journal_base=tmp_path,
        dispatch_sink_for_entry=lambda _cfg, _entry: tmp_path / "dispatch.jsonl",
    )


def _record(
    revision: str,
    *,
    run_id: str = "run-1",
    hive: str = "github/beadhive/beadhive",
) -> dict:
    return {
        "version": run_journal.VERSION,
        "source_revision": revision,
        "timestamp_ms": 1,
        "run_id": run_id,
        "hive": hive,
        "bead": "bh-1",
        "driver": "baml",
        "provider": "claude-code",
        "manifest_digest": DIGEST,
        "provider_continuation": None,
        "writer": run_journal.WRITER_LOCAL_LOOP,
        "activity": {"kind": "run.created", "phase": "planned"},
    }


def _write_run(base: Path, hive: str, run_id: str, records: list[dict]) -> Path:
    path = run_journal.journal_path_for_hive(hive, run_id, base=base)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    return path


def test_exact_registry_resolution_rejects_prefix_missing_and_duplicate(tmp_path: Path) -> None:
    sources = _sources(tmp_path)
    hive = sources.resolve_hive("github/beadhive/beadhive")
    assert hive.identity == "github/beadhive/beadhive"

    with pytest.raises(operator_sources.OperatorSourceError) as prefix:
        sources.resolve_hive("bh")
    assert (prefix.value.code, prefix.value.status_code) == ("invalid_hive_identity", 400)

    with pytest.raises(operator_sources.OperatorSourceError) as missing:
        sources.resolve_hive("github/beadhive/missing")
    assert (missing.value.code, missing.value.status_code) == ("hive_not_found", 404)

    duplicate = _sources(tmp_path, cfg=_cfg(_entry(), _entry(prefix="other")))
    with pytest.raises(operator_sources.OperatorSourceError) as ambiguous:
        duplicate.resolve_hive("github/beadhive/beadhive")
    assert (ambiguous.value.code, ambiguous.value.status_code) == (
        "ambiguous_hive_identity",
        409,
    )


@pytest.mark.parametrize(
    "identity",
    [
        "github/beadhive",
        "github//beadhive",
        "github/beadhive/../beadhive",
        "github\\beadhive\\beadhive",
        "github/beadhive/bead hive",
        "github/beadhive/beadhive\x00",
    ],
)
def test_canonical_identity_syntax_is_closed(identity: str) -> None:
    with pytest.raises(operator_sources.OperatorSourceError, match="canonical"):
        operator_sources.validate_canonical_identity(identity)


def test_refresh_uses_canonical_hive_and_refuses_foreign_entities(tmp_path: Path) -> None:
    provider = Provider()
    sources = _sources(tmp_path, provider=provider)
    hive = sources.resolve_hive("github/beadhive/beadhive")
    bead_state, runtime = sources.refresh_hive(hive)
    assert bead_state.issues[0].hive == hive.identity
    assert runtime.coverage is Coverage.UNKNOWN
    assert provider.requests[0].hive == hive.identity

    provider.snapshot = _snapshot("beadhive")
    with pytest.raises(operator_sources.OperatorSourceError) as mismatch:
        sources.refresh_hive(hive)
    assert mismatch.value.code == "snapshot_hive_mismatch"


def test_refresh_reads_the_real_host_local_dispatch_summary(tmp_path: Path) -> None:
    sink = tmp_path / "dispatch.jsonl"
    sink.write_text(
        json.dumps(
            {
                "event": "seat_spawned",
                "timestamp": "2026-08-24T00:00:00Z",
                "bead": "bh-1",
                "role": "developer",
                "session_id": "seat-1",
            }
        )
        + "\n"
    )
    sources = operator_sources.OperatorSources(
        cfg=_cfg(),
        host_id="host-1",
        provider=Provider(),
        journal_base=tmp_path,
        dispatch_sink_for_entry=lambda _cfg, _entry: sink,
    )
    hive = sources.resolve_hive("github/beadhive/beadhive")
    _beads, runtime = sources.refresh_hive(hive)
    assert runtime.coverage is Coverage.COMPLETE
    assert runtime.summaries[0].session_id == "seat-1"
    assert runtime.summaries[0].bead == "bh-1"


def test_factory_contract_is_flat_authoritative_and_path_free() -> None:
    payload = operator_contract.factory_snapshot(
        [_entry()],
        generated_at=1000,
        host_id="host-1",
        instance_id="instance-1",
        ready=True,
    )
    assert set(
        ("schemaVersion", "hives", "worktrees", "edges", "workspaceRoot", "generatedAt", "coverage")
    ).issubset(payload)
    assert payload["workspaceRoot"] is None
    assert payload["worktrees"] == []
    assert payload["coverage"]["hives"]["state"] == "complete"
    assert payload["coverage"]["worktrees"]["state"] == "unavailable"
    assert "/" not in json.dumps(payload["worktrees"])


def test_exact_run_lookup_uses_real_public_reader_and_rejects_hive_mismatch(
    tmp_path: Path,
) -> None:
    sources = _sources(tmp_path)
    path = _write_run(tmp_path, "github/beadhive/beadhive", "run-1", [_record("rev-1")])
    hive, located = sources.locate_run("run-1")
    assert (hive.identity, located.path) == ("github/beadhive/beadhive", path)
    frame = sources.read_run(hive, located, "run-1")
    assert frame.records[0]["source_revision"] == "rev-1"

    _write_run(
        tmp_path,
        "github/beadhive/beadhive",
        "run-1",
        [
            _record("rev-rewritten"),
            _record("rev-wrong", hive="github/other/repo"),
        ],
    )
    with pytest.raises(operator_sources.OperatorSourceError) as mismatch:
        sources.read_run(hive, located, "run-1")
    assert (mismatch.value.code, mismatch.value.status_code) == (
        "activity_identity_mismatch",
        409,
    )


@pytest.mark.parametrize("wrong_record", ["first", "later"])
def test_run_directory_binds_every_record_to_the_authoritative_hive_root(
    tmp_path: Path, wrong_record: str
) -> None:
    hive = "github/beadhive/beadhive"
    records = [_record("rev-1"), _record("rev-2")]
    records[0 if wrong_record == "first" else 1] = _record(
        f"rev-wrong-{wrong_record}", hive="github/other/repo"
    )
    path = _write_run(tmp_path, hive, "run-1", records)

    inventory = public_readers.read_run_directory(((hive, path.parent),))

    assert inventory.coverage is Coverage.DEGRADED
    assert inventory.coverage_reason == "hive_identity_mismatch"
    assert inventory.entries[0].state == "invalid"
    assert inventory.entries[0].reason_code == "hive_identity_mismatch"
    assert inventory.entries[0].coverage_reason == "hive_identity_mismatch"
    with pytest.raises(public_readers.RunDirectoryError, match="hive_identity_mismatch"):
        inventory.resolve("run-1")


def test_run_directory_surfaces_wrong_hive_in_mixed_and_collision_inventory(
    tmp_path: Path,
) -> None:
    hive_one = "github/beadhive/beadhive"
    hive_two = "github/beadhive/second"
    first_root = run_journal.journal_root_for_hive(hive_one, base=tmp_path)
    second_root = run_journal.journal_root_for_hive(hive_two, base=tmp_path)
    _write_run(
        tmp_path,
        hive_one,
        "run-good",
        [_record("rev-good", run_id="run-good")],
    )
    _write_run(
        tmp_path,
        hive_one,
        "run-wrong",
        [_record("rev-wrong", run_id="run-wrong", hive=hive_two)],
    )

    mixed = public_readers.read_run_directory(((hive_one, first_root),))

    assert mixed.coverage is Coverage.DEGRADED
    assert mixed.coverage_reason == "hive_identity_mismatch"
    assert mixed.resolve("run-good").hive_id == hive_one
    with pytest.raises(public_readers.RunDirectoryError, match="hive_identity_mismatch"):
        mixed.resolve("run-wrong")

    _write_run(
        tmp_path,
        hive_two,
        "run-good",
        [_record("rev-two", run_id="run-good", hive=hive_two)],
    )
    collision = public_readers.read_run_directory(((hive_one, first_root), (hive_two, second_root)))

    assert collision.coverage is Coverage.DEGRADED
    assert collision.coverage_reason == "hive_identity_mismatch"
    with pytest.raises(public_readers.RunDirectoryError, match="ambiguous_run_id"):
        collision.resolve("run-good")


def test_operator_lookup_rejects_initial_wrong_hive_identity(tmp_path: Path) -> None:
    hive = "github/beadhive/beadhive"
    _write_run(tmp_path, hive, "run-1", [_record("rev-wrong", hive="github/other/repo")])
    sources = _sources(tmp_path)

    with pytest.raises(operator_sources.OperatorSourceError) as mismatch:
        sources.locate_run("run-1")

    assert (mismatch.value.code, mismatch.value.status_code, mismatch.value.retryable) == (
        "hive_identity_mismatch",
        409,
        False,
    )


def test_unavailable_root_precedes_visible_identity_degradation_and_hides_duplicates(
    tmp_path: Path,
) -> None:
    hive_one = "github/beadhive/beadhive"
    hive_two = "github/beadhive/second"
    _write_run(tmp_path, hive_one, "run-shared", [_record("rev-visible", run_id="run-shared")])
    _write_run(
        tmp_path,
        hive_one,
        "run-wrong",
        [_record("rev-wrong", run_id="run-wrong", hive=hive_two)],
    )
    _write_run(
        tmp_path,
        hive_two,
        "run-shared",
        [_record("rev-hidden", run_id="run-shared", hive=hive_two)],
    )
    hidden_root = run_journal.journal_root_for_hive(hive_two, base=tmp_path)
    hidden_root.chmod(0)
    try:
        inventory = public_readers.read_run_directory(
            (
                (hive_one, run_journal.journal_root_for_hive(hive_one, base=tmp_path)),
                (hive_two, hidden_root),
            )
        )
    finally:
        hidden_root.chmod(0o700)

    assert inventory.coverage is Coverage.DEGRADED
    assert inventory.coverage_reason == "source_unreadable"
    assert inventory.reason_codes == ("source_unreadable", "hive_identity_mismatch")
    invalid = next(entry for entry in inventory.entries if entry.run_id == "run-wrong")
    assert invalid.reason_code == "hive_identity_mismatch"
    with pytest.raises(public_readers.RunDirectoryError, match="run_directory_unavailable"):
        inventory.resolve("run-shared")
    with pytest.raises(public_readers.RunDirectoryError, match="run_directory_unavailable"):
        inventory.resolve("run-wrong")


def test_empty_authoritative_root_is_not_masked_by_a_valid_sibling_root(tmp_path: Path) -> None:
    hive_one = "github/beadhive/beadhive"
    hive_two = "github/beadhive/second"
    _write_run(tmp_path, hive_one, "run-visible", [_record("rev-1", run_id="run-visible")])
    empty_root = run_journal.journal_root_for_hive(hive_two, base=tmp_path)
    empty_root.mkdir(parents=True)

    inventory = public_readers.read_run_directory(
        (
            (hive_one, run_journal.journal_root_for_hive(hive_one, base=tmp_path)),
            (hive_two, empty_root),
        )
    )

    assert inventory.coverage is Coverage.PARTIAL
    assert inventory.coverage_reason == "source_empty"
    assert inventory.reason_codes == ("source_empty",)
    assert inventory.exact_lookup_available is False
    with pytest.raises(public_readers.RunDirectoryError, match="run_directory_unavailable"):
        inventory.resolve("run-visible")

    sources = _sources(tmp_path, cfg=_cfg(_entry(), _entry(repo="second", prefix="second")))
    with pytest.raises(operator_sources.OperatorSourceError) as unavailable:
        sources.locate_run("run-visible")
    assert (unavailable.value.code, unavailable.value.status_code) == (
        "run_directory_unavailable",
        503,
    )


def test_run_directory_entry_limit_is_bounded_and_fail_closed(tmp_path: Path) -> None:
    hive = "github/beadhive/beadhive"
    root = run_journal.journal_root_for_hive(hive, base=tmp_path)
    for index in range(8):
        run_id = f"run-{index}"
        _write_run(tmp_path, hive, run_id, [_record(f"rev-{index}", run_id=run_id)])

    inventory = public_readers.read_run_directory(
        ((hive, root),),
        max_inventory_entries=3,
    )

    assert inventory.coverage is Coverage.PARTIAL
    assert inventory.coverage_reason == "inventory_limit_exceeded"
    assert inventory.reason_codes == ("inventory_limit_exceeded",)
    assert inventory.observed_roots == 1
    assert inventory.observed_entries == 3
    assert len(inventory.entries) <= 3
    with pytest.raises(public_readers.RunDirectoryError, match="run_directory_unavailable"):
        inventory.resolve("run-0")


def test_run_directory_root_limit_hides_no_duplicate_and_retains_bounded_facts(
    tmp_path: Path,
) -> None:
    roots: list[tuple[str, Path]] = []
    for index in range(6):
        hive = f"github/beadhive/repo-{index}"
        run_id = "run-shared" if index in {0, 5} else f"run-{index}"
        _write_run(tmp_path, hive, run_id, [_record(f"rev-{index}", run_id=run_id, hive=hive)])
        roots.append((hive, run_journal.journal_root_for_hive(hive, base=tmp_path)))

    inventory = public_readers.read_run_directory(roots, max_inventory_roots=2)

    assert inventory.coverage is Coverage.PARTIAL
    assert inventory.coverage_reason == "inventory_limit_exceeded"
    assert inventory.observed_roots == 2
    assert len(inventory.entries) == 2
    assert len(inventory.reason_codes) == 1
    with pytest.raises(public_readers.RunDirectoryError, match="run_directory_unavailable"):
        inventory.resolve("run-shared")


def test_run_directory_total_retained_byte_budget_is_finite(tmp_path: Path) -> None:
    hive = "github/beadhive/beadhive"
    root = run_journal.journal_root_for_hive(hive, base=tmp_path)
    for index in range(4):
        run_id = f"run-{index}"
        _write_run(tmp_path, hive, run_id, [_record(f"rev-{index}", run_id=run_id)])

    inventory = public_readers.read_run_directory(
        ((hive, root),),
        max_inventory_bytes=700,
    )

    assert inventory.coverage is Coverage.PARTIAL
    assert inventory.coverage_reason == "inventory_limit_exceeded"
    assert inventory.retained_bytes <= 700
    assert len(inventory.entries) <= 4
    with pytest.raises(public_readers.RunDirectoryError, match="run_directory_unavailable"):
        inventory.resolve("run-0")


def test_run_directory_filename_enumeration_consumes_the_global_byte_budget(
    tmp_path: Path,
) -> None:
    hive = "github/beadhive/beadhive"
    root = tmp_path / "journals"
    root.mkdir()
    for index in range(20):
        (root / f"non-journal-{index}.txt").touch()

    byte_budget = len(os.fsencode(hive)) + len(os.fsencode(root)) + 40
    inventory = public_readers.read_run_directory(
        ((hive, root),),
        max_inventory_bytes=byte_budget,
    )

    assert inventory.coverage is Coverage.PARTIAL
    assert inventory.coverage_reason == "inventory_limit_exceeded"
    assert inventory.retained_bytes <= byte_budget
    assert inventory.observed_entries <= 2
    assert inventory.entries == ()


@pytest.mark.parametrize(
    ("content", "coverage", "reason"),
    [
        (b"not-json\n", Coverage.DEGRADED, "invalid_complete_record"),
        (
            (json.dumps(_record("rev-1")) + "\nnot-json\n").encode(),
            Coverage.DEGRADED,
            "invalid_complete_record",
        ),
        (
            (json.dumps(_record("rev-1")) + "\n" + '{"version":').encode(),
            Coverage.PARTIAL,
            "final_record_incomplete",
        ),
    ],
)
def test_run_directory_never_reports_corrupt_or_interrupted_journals_complete(
    tmp_path: Path,
    content: bytes,
    coverage: Coverage,
    reason: str,
) -> None:
    path = run_journal.journal_path_for_hive("github/beadhive/beadhive", "run-1", base=tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(content)

    inventory = public_readers.read_run_directory((("github/beadhive/beadhive", path.parent),))

    assert inventory.coverage is coverage
    assert inventory.coverage_reason == reason
    assert inventory.entries[0].coverage is coverage
    assert inventory.entries[0].coverage_reason == reason
    if coverage is Coverage.DEGRADED:
        with pytest.raises(public_readers.RunDirectoryError, match=reason):
            inventory.resolve("run-1")


def test_configured_activity_record_limit_bounds_reads_and_cached_history(tmp_path: Path) -> None:
    _write_run(
        tmp_path,
        "github/beadhive/beadhive",
        "run-1",
        [_record(f"rev-{index}") for index in range(1, 4)],
    )
    sources = operator_sources.OperatorSources(
        cfg=_cfg(),
        host_id="host-1",
        provider=Provider(),
        summary_reader=lambda _path, host, source: _runtime(host, source),
        journal_base=tmp_path,
        max_records_per_read=2,
    )
    feed = operator_feed.OperatorFeed(sources)

    first = feed.activity_with_cursor("run-1")
    second = feed.activity_with_cursor("run-1")

    assert [item["sourceRevision"] for item in first["activities"]] == ["rev-1", "rev-2"]
    assert second["activities"] == first["activities"]
    assert first["coverage"] == {
        "state": "partial",
        "detail": "record_limit_exceeded",
    }
    state = feed._activities[("github/beadhive/beadhive", "run-1")]
    assert len(state.records) == 2


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"max_record_bytes": 64}, "record_too_large"),
        ({"max_read_bytes": 64}, "source_byte_limit_exceeded"),
    ],
)
def test_activity_reader_has_deterministic_per_record_and_source_byte_bounds(
    tmp_path: Path, kwargs: dict[str, int], reason: str
) -> None:
    path = _write_run(
        tmp_path,
        "github/beadhive/beadhive",
        "run-1",
        [_record("rev-1")],
    )

    inventory = public_readers.read_run_directory(
        (("github/beadhive/beadhive", path.parent),), **kwargs
    )

    assert inventory.coverage is Coverage.PARTIAL
    assert inventory.coverage_reason == reason
    assert inventory.entries[0].coverage_reason == reason


def test_missing_duplicate_and_unsafe_runs_are_deterministic(tmp_path: Path) -> None:
    cfg = _cfg(_entry(), _entry(repo="second", prefix="second"))
    sources = _sources(tmp_path, cfg=cfg)
    with pytest.raises(operator_sources.OperatorSourceError) as missing:
        sources.locate_run("run-missing")
    assert (missing.value.code, missing.value.status_code) == (
        "run_directory_unavailable",
        503,
    )

    for hive in ("github/beadhive/beadhive", "github/beadhive/second"):
        _write_run(tmp_path, hive, "run-1", [_record("rev-1", hive=hive)])
    with pytest.raises(operator_sources.OperatorSourceError) as duplicate:
        sources.locate_run("run-1")
    assert (duplicate.value.code, duplicate.value.status_code) == ("ambiguous_run_id", 409)

    with pytest.raises(operator_sources.OperatorSourceError) as unsafe:
        sources.locate_run("../run")
    assert (unsafe.value.code, unsafe.value.status_code) == ("invalid_run_id", 400)
