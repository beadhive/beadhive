"""Exact identity and real public-source coverage for the operator API."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from beadhive import operator_contract, operator_sources, run_journal, state_stream
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
    assert (hive.identity, located) == ("github/beadhive/beadhive", path)
    frame = sources.read_run(hive, path, "run-1")
    assert frame.records[0]["source_revision"] == "rev-1"

    _write_run(
        tmp_path,
        "github/beadhive/beadhive",
        "run-1",
        [_record("rev-wrong", hive="github/other/repo")],
    )
    with pytest.raises(operator_sources.OperatorSourceError) as mismatch:
        sources.read_run(hive, path, "run-1")
    assert (mismatch.value.code, mismatch.value.status_code) == (
        "activity_identity_mismatch",
        409,
    )


def test_missing_duplicate_and_unsafe_runs_are_deterministic(tmp_path: Path) -> None:
    cfg = _cfg(_entry(), _entry(repo="second", prefix="second"))
    sources = _sources(tmp_path, cfg=cfg)
    with pytest.raises(operator_sources.OperatorSourceError) as missing:
        sources.locate_run("run-missing")
    assert missing.value.status_code == 404

    for hive in ("github/beadhive/beadhive", "github/beadhive/second"):
        _write_run(tmp_path, hive, "run-1", [_record("rev-1", hive=hive)])
    with pytest.raises(operator_sources.OperatorSourceError) as duplicate:
        sources.locate_run("run-1")
    assert (duplicate.value.code, duplicate.value.status_code) == ("ambiguous_run_id", 409)

    with pytest.raises(operator_sources.OperatorSourceError) as unsafe:
        sources.locate_run("../run")
    assert (unsafe.value.code, unsafe.value.status_code) == ("invalid_run_id", 400)
