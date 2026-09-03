"""Exact authenticated factory and cross-hive run-directory coverage."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import jsonschema
import pytest

from beadhive import (
    daemon_auth,
    daemon_contract,
    daemon_factory,
    dolt_health,
    host_daemon,
    hq,
    operator_api,
    operator_sources,
    public_readers,
    run_journal,
)
from beadhive.daemon_config import HostDaemonConfig
from beadhive.daemon_contract import AuthScope
from beadhive.public_readers import Coverage, RunDirectoryEntry, RunDirectoryInventory

HIVE_ONE = "github/acme/one"
HIVE_TWO = "github/acme/two"
DOLT_MANIFEST = (
    "5:__DOLT__:h3tp99f0n49eadl9gve8ugchu51qhugn:"
    "svj5n97a8eqbt3dt8hg2atp99em05nod:00000000000000000000000000000000:"
    "vvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvv:4376\n"
)


class _Provider:
    def refresh(self, request):
        if request.hive == HIVE_TWO:
            raise OSError("private source failure")
        from beadhive.state_stream import ProviderSnapshot

        return ProviderSnapshot(scope="hive", revision="opaque:one", as_of="2026-09-02T00:00:00Z")


def _cfg() -> dict:
    return {
        "managed_repos": [
            {
                "provider": "github",
                "org": "acme",
                "repo": "one",
                "prefix": "one",
                "kind": "org-native",
            },
            {
                "provider": "github",
                "org": "acme",
                "repo": "two",
                "prefix": "two",
                "kind": "external",
                "upstream": "acme/one",
            },
        ]
    }


def _write_valid_journal(path: Path, *, run_id: str, hive: str = HIVE_ONE) -> None:
    path.write_text(
        json.dumps(
            {
                "version": run_journal.VERSION,
                "source_revision": "revision-1",
                "timestamp_ms": 1,
                "run_id": run_id,
                "hive": hive,
                "bead": None,
                "driver": "baml",
                "provider": "claude-code",
                "manifest_digest": "sha256:" + "a" * 64,
                "provider_continuation": None,
                "writer": run_journal.WRITER_LOCAL_LOOP,
                "activity": {"kind": "run.created", "phase": "planned"},
            }
        )
        + "\n"
    )


def test_factory_projects_exact_sources_and_explicit_degradation_without_private_data(
    tmp_path: Path,
) -> None:
    sources = operator_sources.OperatorSources(
        cfg=_cfg(), host_id="host-stable", provider=_Provider(), journal_base=tmp_path
    )
    inventory = RunDirectoryInventory(
        entries=(
            RunDirectoryEntry(
                hive_id=HIVE_ONE,
                run_id="run-private",
                path=tmp_path / "secret-token" / "run-private.jsonl",
                modified_at=100.0,
            ),
        ),
        coverage=Coverage.PARTIAL,
        coverage_reason="stale_data",
    )
    observed_names: list[str] = []

    def describe(hive):
        observed_names.append(hive.identity)
        return daemon_factory.HiveSourceObservation(
            readiness=("ready" if hive.identity == HIVE_ONE else "unavailable"),
            coverage=("complete" if hive.identity == HIVE_ONE else "unavailable"),
            reason_code=(None if hive.identity == HIVE_ONE else "hive_not_installed_on_host"),
        )

    def assignment(hive):
        return daemon_factory.AssignmentObservation(
            host_id="host-stable",
            role="executor",
            state="held" if hive.identity == HIVE_ONE else "unknown",
            lease_expires_at=2_000 if hive.identity == HIVE_ONE else None,
        )

    directory = daemon_factory.FactoryDirectory(
        sources=sources,
        host_id="host-stable",
        service_instance_id="instance-changing",
        started_at=1_000,
        clock_millis=lambda: 1_500,
        describe_hive=describe,
        load_run_directory=lambda _hives: inventory,
        load_assignment=assignment,
        hq_status=lambda: daemon_factory.DependencyObservation("ready"),
    )

    payload = directory.snapshot(ready=True, accepting_work=True)
    parsed = daemon_contract.FactoryResponse.model_validate(payload)

    assert parsed.host.host_id == "host-stable"
    assert parsed.host.service_instance_id == "instance-changing"
    assert observed_names == [HIVE_ONE, HIVE_TWO]
    assert [hive.hive_id for hive in parsed.hives] == [HIVE_ONE, HIVE_TWO]
    assert [hive.readiness for hive in parsed.hives] == ["ready", "unavailable"]
    assert parsed.relationships[0].to_hive_id == HIVE_ONE
    assert parsed.relationships[0].from_hive_id == HIVE_TWO
    assert [item.state for item in parsed.assignments] == ["held", "unknown"]
    dependencies = {item.name: item for item in parsed.status.dependencies}
    assert dependencies["bead-state"].status == "degraded"
    assert dependencies["run-journals"].reason_code == "stale_data"
    assert parsed.status.readiness == "degraded"
    assert parsed.coverage.state == "partial"
    assert {item.name: item.available for item in parsed.capabilities} == {
        "snapshot": True,
        "events": True,
        "activity-read": True,
        "activity-publish": False,
        "terminal": False,
    }

    encoded = json.dumps(payload)
    assert "run-private" not in encoded
    assert "secret-token" not in encoded
    assert str(tmp_path) not in encoded
    assert not ({"workspaceRoot", "worktrees", "edges", "ready"} & payload.keys())


@pytest.mark.parametrize(
    ("configured", "accepting", "inventory", "expected"),
    [
        (
            True,
            True,
            RunDirectoryInventory((), Coverage.COMPLETE, None),
            {"name": "activity-publish", "available": True, "reasonCode": None},
        ),
        (
            False,
            True,
            RunDirectoryInventory((), Coverage.COMPLETE, None),
            {
                "name": "activity-publish",
                "available": False,
                "reasonCode": "not_implemented",
            },
        ),
        (
            True,
            False,
            RunDirectoryInventory((), Coverage.COMPLETE, None),
            {
                "name": "activity-publish",
                "available": False,
                "reasonCode": "daemon_not_accepting",
            },
        ),
        (
            True,
            True,
            RunDirectoryInventory(
                (),
                Coverage.PARTIAL,
                "inventory_limit_exceeded",
                ("inventory_limit_exceeded",),
            ),
            {
                "name": "activity-publish",
                "available": False,
                "reasonCode": "inventory_limit_exceeded",
            },
        ),
    ],
)
def test_factory_activity_publish_capability_reports_runtime_truth(
    tmp_path: Path,
    configured: bool,
    accepting: bool,
    inventory: RunDirectoryInventory,
    expected: dict[str, object],
) -> None:
    sources = operator_sources.OperatorSources(
        cfg=_cfg(), host_id="host-stable", provider=_Provider(), journal_base=tmp_path
    )
    directory = daemon_factory.FactoryDirectory(
        sources=sources,
        host_id="host-stable",
        service_instance_id="instance-changing",
        started_at=1_000,
        describe_hive=lambda _hive: daemon_factory.HiveSourceObservation("ready", "complete"),
        load_run_directory=lambda _hives: inventory,
        load_assignment=lambda _hive: daemon_factory.AssignmentObservation(
            "host-stable", "executor", "held"
        ),
        hq_status=lambda: daemon_factory.DependencyObservation("ready"),
        dolt_status=lambda _hives: daemon_factory.DependencyObservation("ready"),
        activity_publish_configured=configured,
    )

    payload = directory.snapshot(ready=True, accepting_work=accepting)
    capability = next(
        item for item in payload["capabilities"] if item["name"] == "activity-publish"
    )
    assert capability == expected


def test_public_run_directory_resolves_exact_unknown_collision_invalid_and_partial(
    tmp_path: Path,
) -> None:
    roots = {HIVE_ONE: tmp_path / "one", HIVE_TWO: tmp_path / "two"}
    for root in roots.values():
        root.mkdir()
    _write_valid_journal(roots[HIVE_ONE] / "run-1.jsonl", run_id="run-1")
    _write_valid_journal(roots[HIVE_TWO] / "run-1.jsonl", run_id="run-1", hive=HIVE_TWO)
    _write_valid_journal(roots[HIVE_ONE] / "run-2.jsonl", run_id="run-2")
    (roots[HIVE_TWO] / "run-link.jsonl").symlink_to(roots[HIVE_ONE] / "run-2.jsonl")
    (roots[HIVE_TWO] / "not-a-journal.txt").write_text("ignored")

    inventory = public_readers.read_run_directory(
        ((HIVE_ONE, roots[HIVE_ONE]), (HIVE_TWO, roots[HIVE_TWO]))
    )

    assert inventory.coverage is Coverage.PARTIAL
    assert inventory.coverage_reason == "invalid_inventory_entry"
    assert inventory.resolve("run-2").hive_id == HIVE_ONE
    with pytest.raises(public_readers.RunDirectoryError, match="run_not_found"):
        inventory.resolve("run-20")
    with pytest.raises(public_readers.RunDirectoryError, match="ambiguous_run_id"):
        inventory.resolve("run-1")
    with pytest.raises(public_readers.RunDirectoryError, match="invalid_run_source"):
        inventory.resolve("run-link")

    missing = public_readers.read_run_directory(
        ((HIVE_ONE, roots[HIVE_ONE]), ("github/acme/missing", tmp_path / "missing"))
    )
    assert missing.coverage is Coverage.PARTIAL
    assert missing.coverage_reason == "partial_coverage"
    with pytest.raises(public_readers.RunDirectoryError, match="run_directory_unavailable"):
        missing.resolve("run-2")

    collision = public_readers.read_run_directory(
        ((HIVE_ONE, roots[HIVE_ONE]), (HIVE_TWO, roots[HIVE_ONE]))
    )
    assert collision.coverage_reason == "journal_root_collision"
    assert collision.exact_lookup_available is False
    with pytest.raises(public_readers.RunDirectoryError, match="journal_root_collision"):
        collision.resolve("run-2")


@pytest.mark.parametrize("replacement_shape", ["symlink", "directory"])
def test_run_directory_enumeration_is_anchored_and_closes_root_descriptors_during_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement_shape: str,
) -> None:
    root = tmp_path / "journals"
    root.mkdir()
    _write_valid_journal(root / "run-safe.jsonl", run_id="run-safe")
    external = tmp_path / "external"
    external.mkdir()
    _write_valid_journal(external / "run-external.jsonl", run_id="run-external")
    displaced = tmp_path / "displaced"
    real_scandir = os.scandir
    real_listdir = os.listdir
    swapped = False

    def swap_at_enumeration(target) -> None:
        nonlocal swapped
        if not swapped and (target in {root, str(root)} or isinstance(target, int)):
            swapped = True
            root.rename(displaced)
            if replacement_shape == "symlink":
                root.symlink_to(external, target_is_directory=True)
            else:
                external.rename(root)

    def racing_scandir(target):
        swap_at_enumeration(target)
        return real_scandir(target)

    def racing_listdir(target):
        swap_at_enumeration(target)
        return real_listdir(target)

    monkeypatch.setattr(public_readers.os, "scandir", racing_scandir)
    monkeypatch.setattr(public_readers.os, "listdir", racing_listdir)
    fd_root = Path("/proc/self/fd")
    before = len(tuple(fd_root.iterdir()))

    inventory = public_readers.read_run_directory(((HIVE_ONE, root),))

    assert swapped
    assert inventory.coverage is Coverage.DEGRADED
    assert inventory.coverage_reason == "source_unreadable"
    assert inventory.entries == ()
    assert "run-external" not in {entry.run_id for entry in inventory.entries}
    assert len(tuple(fd_root.iterdir())) == before


def test_multiply_linked_journal_is_an_explicit_invalid_identity(tmp_path: Path) -> None:
    root = tmp_path / "journals"
    root.mkdir()
    journal = root / "run-linked.jsonl"
    journal.write_text("{}\n")
    os.link(journal, tmp_path / "external-journal-link.jsonl")

    inventory = public_readers.read_run_directory(((HIVE_ONE, root),))

    assert inventory.coverage is Coverage.PARTIAL
    assert inventory.coverage_reason == "invalid_inventory_entry"
    assert inventory.entries[0].reason_code == "multiply_linked_run_source"
    with pytest.raises(public_readers.RunDirectoryError, match="multiply_linked_run_source"):
        inventory.resolve("run-linked")


def test_empty_real_journal_source_is_incomplete_and_never_authoritative_not_found(
    tmp_path: Path,
) -> None:
    root = tmp_path / "empty"
    root.mkdir()

    inventory = public_readers.read_run_directory(((HIVE_ONE, root),))

    assert inventory.coverage is Coverage.PARTIAL
    assert inventory.coverage_reason == "source_empty"
    with pytest.raises(public_readers.RunDirectoryError, match="run_directory_unavailable"):
        inventory.resolve("run-absent")


def test_unreadable_real_journal_source_is_unavailable_not_empty(tmp_path: Path) -> None:
    root = tmp_path / "unreadable"
    root.mkdir()
    root.chmod(0)
    try:
        inventory = public_readers.read_run_directory(((HIVE_ONE, root),))
    finally:
        root.chmod(0o700)

    assert inventory.coverage is Coverage.DEGRADED
    assert inventory.coverage_reason == "source_unreadable"


def test_unreadable_regular_journal_file_degrades_the_whole_inventory(tmp_path: Path) -> None:
    root = tmp_path / "journals"
    root.mkdir()
    journal = root / "run-unreadable.jsonl"
    journal.write_text("{}\n")
    journal.chmod(0)
    try:
        inventory = public_readers.read_run_directory(((HIVE_ONE, root),))
    finally:
        journal.chmod(0o600)

    assert inventory.coverage is Coverage.DEGRADED
    assert inventory.coverage_reason == "source_unreadable"
    assert inventory.entries[0].state == "invalid"
    assert inventory.entries[0].reason_code == "run_source_unavailable"
    with pytest.raises(public_readers.RunDirectoryError, match="run_directory_unavailable"):
        inventory.resolve("run-unreadable")


def test_zero_byte_real_journal_is_incomplete_and_never_ready(tmp_path: Path) -> None:
    root = tmp_path / "journals"
    root.mkdir()
    (root / "run-empty.jsonl").touch()

    inventory = public_readers.read_run_directory(((HIVE_ONE, root),))

    assert inventory.coverage is Coverage.PARTIAL
    assert inventory.coverage_reason == "source_empty"
    assert inventory.entries[0].state == "invalid"
    assert inventory.entries[0].reason_code == "empty_run_source"
    with pytest.raises(public_readers.RunDirectoryError, match="run_directory_unavailable"):
        inventory.resolve("run-empty")


def test_operator_run_lookup_uses_one_cross_hive_inventory_and_never_prefix_matches(
    tmp_path: Path,
) -> None:
    sources = operator_sources.OperatorSources(
        cfg=_cfg(), host_id="host-stable", provider=_Provider(), journal_base=tmp_path
    )
    exact = tmp_path / "github-acme-one" / "run-exact.jsonl"
    exact.parent.mkdir()
    _write_valid_journal(exact, run_id="run-exact")
    second_root = tmp_path / "github-acme-two"
    second_root.mkdir()
    _write_valid_journal(second_root / "run-other.jsonl", run_id="run-other", hive=HIVE_TWO)

    hive, located = sources.locate_run("run-exact")
    assert hive.identity == HIVE_ONE and located.path == exact
    for candidate in ("run", "run-ex", "unknown"):
        with pytest.raises(operator_sources.OperatorSourceError) as caught:
            sources.locate_run(candidate)
        assert caught.value.code == "run_not_found"


@pytest.mark.parametrize("swap_shape", ["symlink", "replacement"])
def test_located_run_rejects_path_swap_and_closes_every_descriptor(
    tmp_path: Path, swap_shape: str
) -> None:
    sources = operator_sources.OperatorSources(
        cfg={"managed_repos": [_cfg()["managed_repos"][0]]},
        host_id="host-stable",
        provider=_Provider(),
        journal_base=tmp_path / "journals",
    )
    path = run_journal.journal_path_for_hive(HIVE_ONE, "run-swap", base=tmp_path / "journals")
    path.parent.mkdir(parents=True)
    record = {
        "version": run_journal.VERSION,
        "source_revision": "revision-1",
        "timestamp_ms": 1,
        "run_id": "run-swap",
        "hive": HIVE_ONE,
        "bead": None,
        "driver": "baml",
        "provider": "claude-code",
        "manifest_digest": "sha256:" + "a" * 64,
        "provider_continuation": None,
        "writer": run_journal.WRITER_LOCAL_LOOP,
        "activity": {"kind": "run.created", "phase": "planned"},
    }
    path.write_text(json.dumps(record) + "\n")
    hive, located = sources.locate_run("run-swap")
    original = tmp_path / "original.jsonl"
    path.replace(original)
    external = tmp_path / "external.jsonl"
    external.write_text(json.dumps(record) + "\n")
    if swap_shape == "symlink":
        path.symlink_to(external)
    else:
        external.replace(path)
    fd_root = Path("/proc/self/fd")
    before = len(tuple(fd_root.iterdir()))

    for _ in range(3):
        with pytest.raises(operator_sources.OperatorSourceError) as caught:
            sources.read_run(hive, located, "run-swap")
        assert (caught.value.code, caught.value.status_code) == (
            "activity_source_changed",
            503,
        )

    assert len(tuple(fd_root.iterdir())) == before


def test_located_run_rejects_a_hard_link_added_after_discovery(tmp_path: Path) -> None:
    sources = operator_sources.OperatorSources(
        cfg={"managed_repos": [_cfg()["managed_repos"][0]]},
        host_id="host-stable",
        provider=_Provider(),
        journal_base=tmp_path / "journals",
    )
    path = run_journal.journal_path_for_hive(
        HIVE_ONE, "run-linked-late", base=tmp_path / "journals"
    )
    path.parent.mkdir(parents=True)
    record = {
        "version": run_journal.VERSION,
        "source_revision": "revision-1",
        "timestamp_ms": 1,
        "run_id": "run-linked-late",
        "hive": HIVE_ONE,
        "bead": None,
        "driver": "baml",
        "provider": "claude-code",
        "manifest_digest": "sha256:" + "a" * 64,
        "provider_continuation": None,
        "writer": run_journal.WRITER_LOCAL_LOOP,
        "activity": {"kind": "run.created", "phase": "planned"},
    }
    path.write_text(json.dumps(record) + "\n")
    hive, located = sources.locate_run("run-linked-late")
    os.link(path, tmp_path / "external-link.jsonl")
    fd_root = Path("/proc/self/fd")
    before = len(tuple(fd_root.iterdir()))

    with pytest.raises(operator_sources.OperatorSourceError) as caught:
        sources.read_run(hive, located, "run-linked-late")

    assert (caught.value.code, caught.value.status_code) == (
        "activity_source_changed",
        503,
    )
    assert len(tuple(fd_root.iterdir())) == before


def test_read_run_rejects_a_post_inventory_symlinked_root_and_closes_descriptors(
    tmp_path: Path,
) -> None:
    sources = operator_sources.OperatorSources(
        cfg={"managed_repos": [_cfg()["managed_repos"][0]]},
        host_id="host-stable",
        provider=_Provider(),
        journal_base=tmp_path / "journals",
    )
    path = run_journal.journal_path_for_hive(HIVE_ONE, "run-moved-root", base=tmp_path / "journals")
    path.parent.mkdir(parents=True)
    record = {
        "version": run_journal.VERSION,
        "source_revision": "revision-1",
        "timestamp_ms": 1,
        "run_id": "run-moved-root",
        "hive": HIVE_ONE,
        "bead": None,
        "driver": "baml",
        "provider": "claude-code",
        "manifest_digest": "sha256:" + "a" * 64,
        "provider_continuation": None,
        "writer": run_journal.WRITER_LOCAL_LOOP,
        "activity": {"kind": "run.created", "phase": "planned"},
    }
    path.write_text(json.dumps(record) + "\n")
    hive, located = sources.locate_run("run-moved-root")
    displaced_root = tmp_path / "displaced-root"
    path.parent.rename(displaced_root)
    path.parent.symlink_to(displaced_root, target_is_directory=True)
    fd_root = Path("/proc/self/fd")
    before = len(tuple(fd_root.iterdir()))

    for _ in range(3):
        with pytest.raises(operator_sources.OperatorSourceError) as caught:
            sources.read_run(hive, located, "run-moved-root")
        assert (caught.value.code, caught.value.status_code) == (
            "activity_source_changed",
            503,
        )

    assert len(tuple(fd_root.iterdir())) == before


def test_factory_reads_hq_dependency_without_fetching_legacy_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = {
        "managed_repos": [
            {
                "provider": "local",
                "org": "factory",
                "repo": "hq",
                "prefix": "hq",
                "kind": "hq",
            }
        ]
    }
    sources = operator_sources.OperatorSources(
        cfg=cfg, host_id="host-stable", provider=_Provider(), journal_base=tmp_path
    )
    hq_dir = tmp_path / "hq"
    hq_dir.mkdir()
    monkeypatch.setattr(daemon_factory.config, "hq_dir", lambda: hq_dir)
    monkeypatch.setattr(
        hq,
        "status_payload",
        lambda: pytest.fail("factory readiness must not fetch legacy HQ status"),
    )
    directory = daemon_factory.FactoryDirectory(
        sources=sources,
        host_id="host-stable",
        service_instance_id="instance-changing",
        started_at=1_000,
        clock_millis=lambda: 1_500,
        load_run_directory=lambda _hives: RunDirectoryInventory(
            entries=(), coverage=Coverage.UNKNOWN, coverage_reason="source_missing"
        ),
    )

    payload = directory.snapshot(ready=True, accepting_work=True)
    dependencies = {item["name"]: item for item in payload["status"]["dependencies"]}

    assert dependencies["hq"] == {
        "name": "hq",
        "status": "unavailable",
        "reasonCode": "hq_not_initialized",
    }
    assert str(hq_dir) not in json.dumps(payload)


def _isolated_hq_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    timeout: float,
) -> daemon_factory.FactoryDirectory:
    cfg = {
        "managed_repos": [
            {
                "provider": "local",
                "org": "factory",
                "repo": "hq",
                "prefix": "hq",
                "kind": "hq",
            }
        ]
    }
    sources = operator_sources.OperatorSources(
        cfg=cfg, host_id="host-stable", provider=_Provider(), journal_base=tmp_path / "journals"
    )
    hq_dir = tmp_path / "hq"
    (hq_dir / ".beads").mkdir(parents=True)
    monkeypatch.setattr(daemon_factory.config, "hq_dir", lambda: hq_dir)
    return daemon_factory.FactoryDirectory(
        sources=sources,
        host_id="host-stable",
        service_instance_id="instance-changing",
        started_at=1_000,
        clock_millis=lambda: 1_500,
        describe_hive=lambda _hive: daemon_factory.HiveSourceObservation("ready", "complete"),
        load_run_directory=lambda _hives: RunDirectoryInventory((), Coverage.COMPLETE, None),
        load_assignment=lambda _hive: daemon_factory.AssignmentObservation(
            "host-stable", "executor", "held"
        ),
        dolt_status=lambda _hives: daemon_factory.DependencyObservation("ready"),
        dolt_probe_timeout_seconds=timeout,
    )


@pytest.mark.parametrize(
    ("probe_code", "expected_reason"),
    [
        ("import time; time.sleep(60)", "hq_status_timeout"),
        ("raise SystemExit(3)", "hq_status_unavailable"),
    ],
)
def test_factory_hq_probe_is_bounded_redacted_and_reaps_the_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe_code: str,
    expected_reason: str,
) -> None:
    directory = _isolated_hq_directory(tmp_path, monkeypatch, timeout=0.05)
    children: list[subprocess.Popen[str]] = []
    real_popen = subprocess.Popen

    def tracked_popen(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(
        daemon_factory,
        "_hq_probe_command",
        lambda: (sys.executable, "-c", probe_code),
    )
    monkeypatch.setattr(daemon_factory.subprocess, "Popen", tracked_popen)
    started = time.monotonic()

    payload = directory.snapshot(ready=True, accepting_work=True)

    elapsed = time.monotonic() - started
    dependency = next(item for item in payload["status"]["dependencies"] if item["name"] == "hq")
    assert elapsed < 0.5
    assert dependency == {
        "name": "hq",
        "status": "unavailable",
        "reasonCode": expected_reason,
    }
    assert children and all(child.poll() is not None for child in children)
    assert "sleep" not in json.dumps(payload)


def test_cancelled_factory_request_reaps_slow_hq_probe_before_returning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _isolated_hq_directory(tmp_path, monkeypatch, timeout=2.0)
    children: list[subprocess.Popen[str]] = []
    real_popen = subprocess.Popen

    def tracked_popen(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(
        daemon_factory,
        "_hq_probe_command",
        lambda: (sys.executable, "-c", "import time; time.sleep(60)"),
    )
    monkeypatch.setattr(daemon_factory.subprocess, "Popen", tracked_popen)
    api = operator_api.OperatorAPI(
        sources=directory.sources,
        feed=object(),
        host_id="host-stable",
        instance_id="instance-changing",
        ready=lambda: True,
        factory_directory=directory,
    )

    async def exercise() -> None:
        task = asyncio.create_task(api.factory(None))
        for _ in range(100):
            if children:
                break
            await asyncio.sleep(0.005)
        assert children
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=0.5)

    asyncio.run(exercise())

    assert all(child.poll() is not None for child in children)
    assert not any(
        thread.name.startswith("daemon-hq-probe") and thread.is_alive()
        for thread in threading.enumerate()
    )


def test_factory_marks_a_real_old_journal_mtime_stale_under_the_configured_threshold(
    tmp_path: Path,
) -> None:
    cfg = {"managed_repos": [_cfg()["managed_repos"][0]]}
    sources = operator_sources.OperatorSources(
        cfg=cfg, host_id="host-stable", provider=_Provider(), journal_base=tmp_path
    )
    journal_root = run_journal.journal_root_for_hive(HIVE_ONE, base=tmp_path)
    journal_root.mkdir(parents=True)
    journal = journal_root / "run-old.jsonl"
    _write_valid_journal(journal, run_id="run-old")
    os.utime(journal, (100.0, 100.0))

    directory = daemon_factory.FactoryDirectory(
        sources=sources,
        host_id="host-stable",
        service_instance_id="instance-changing",
        started_at=1_000,
        clock_millis=lambda: 200_000,
        journal_stale_after_seconds=30.0,
        describe_hive=lambda _hive: daemon_factory.HiveSourceObservation("ready", "complete"),
        load_assignment=lambda _hive: daemon_factory.AssignmentObservation(
            "host-stable", "executor", "held"
        ),
        hq_status=lambda: daemon_factory.DependencyObservation("ready"),
        dolt_status=lambda _hives: daemon_factory.DependencyObservation("ready"),
    )

    payload = directory.snapshot(ready=True, accepting_work=True)
    dependencies = {item["name"]: item for item in payload["status"]["dependencies"]}

    assert dependencies["run-journals"] == {
        "name": "run-journals",
        "status": "degraded",
        "reasonCode": "run_journals_stale",
    }
    assert payload["coverage"]["state"] == "partial"
    assert payload["coverage"]["sources"]["run-journals"] == {
        "state": "partial",
        "detail": "run_journals_stale",
        "generatedAt": 200_000,
    }


@pytest.mark.parametrize(
    ("content", "expected_status", "expected_reason"),
    [
        (b"not-json\n", "unavailable", "invalid_complete_record"),
        (
            None,
            "degraded",
            "final_record_incomplete",
        ),
    ],
)
def test_factory_never_reports_corrupt_or_interrupted_journal_sources_ready(
    tmp_path: Path,
    content: bytes | None,
    expected_status: str,
    expected_reason: str,
) -> None:
    cfg = {"managed_repos": [_cfg()["managed_repos"][0]]}
    sources = operator_sources.OperatorSources(
        cfg=cfg, host_id="host-stable", provider=_Provider(), journal_base=tmp_path
    )
    journal_root = run_journal.journal_root_for_hive(HIVE_ONE, base=tmp_path)
    journal_root.mkdir(parents=True)
    journal = journal_root / "run-broken.jsonl"
    if content is None:
        _write_valid_journal(journal, run_id="run-broken")
        with journal.open("ab") as stream:
            stream.write(b'{"version":')
    else:
        journal.write_bytes(content)
    directory = daemon_factory.FactoryDirectory(
        sources=sources,
        host_id="host-stable",
        service_instance_id="instance-changing",
        started_at=1_000,
        describe_hive=lambda _hive: daemon_factory.HiveSourceObservation("ready", "complete"),
        load_assignment=lambda _hive: daemon_factory.AssignmentObservation(
            "host-stable", "executor", "held"
        ),
        hq_status=lambda: daemon_factory.DependencyObservation("ready"),
        dolt_status=lambda _hives: daemon_factory.DependencyObservation("ready"),
    )

    payload = directory.snapshot(ready=True, accepting_work=True)
    dependency = next(
        item for item in payload["status"]["dependencies"] if item["name"] == "run-journals"
    )

    assert dependency == {
        "name": "run-journals",
        "status": expected_status,
        "reasonCode": expected_reason,
    }


def test_factory_mixed_old_and_fresh_journals_surfaces_the_stale_source(
    tmp_path: Path,
) -> None:
    cfg = {"managed_repos": [_cfg()["managed_repos"][0]]}
    sources = operator_sources.OperatorSources(
        cfg=cfg, host_id="host-stable", provider=_Provider(), journal_base=tmp_path
    )
    journal_root = run_journal.journal_root_for_hive(HIVE_ONE, base=tmp_path)
    journal_root.mkdir(parents=True)
    old = journal_root / "run-old.jsonl"
    fresh = journal_root / "run-fresh.jsonl"
    _write_valid_journal(old, run_id="run-old")
    _write_valid_journal(fresh, run_id="run-fresh")
    os.utime(old, (100.0, 100.0))
    os.utime(fresh, (195.0, 195.0))
    directory = daemon_factory.FactoryDirectory(
        sources=sources,
        host_id="host-stable",
        service_instance_id="instance-changing",
        started_at=1_000,
        clock_millis=lambda: 200_000,
        journal_stale_after_seconds=30.0,
        describe_hive=lambda _hive: daemon_factory.HiveSourceObservation("ready", "complete"),
        load_assignment=lambda _hive: daemon_factory.AssignmentObservation(
            "host-stable", "executor", "held"
        ),
        hq_status=lambda: daemon_factory.DependencyObservation("ready"),
        dolt_status=lambda _hives: daemon_factory.DependencyObservation("ready"),
    )

    payload = directory.snapshot(ready=True, accepting_work=True)
    dependency = next(
        item for item in payload["status"]["dependencies"] if item["name"] == "run-journals"
    )

    assert dependency == {
        "name": "run-journals",
        "status": "degraded",
        "reasonCode": "run_journals_stale",
    }
    assert payload["coverage"]["sources"]["run-journals"] == {
        "state": "partial",
        "detail": "run_journals_stale",
        "generatedAt": 200_000,
    }


@pytest.mark.parametrize(
    ("source_shape", "expected_status", "expected_reason", "expected_coverage"),
    [
        ("empty", "degraded", "source_empty", "partial"),
        ("unreadable", "unavailable", "source_unreadable", "unavailable"),
        ("unreadable-file", "unavailable", "source_unreadable", "unavailable"),
        ("empty-file", "degraded", "source_empty", "partial"),
    ],
)
def test_factory_projects_empty_and_unreadable_real_journal_truth(
    tmp_path: Path,
    source_shape: str,
    expected_status: str,
    expected_reason: str,
    expected_coverage: str,
) -> None:
    cfg = {"managed_repos": [_cfg()["managed_repos"][0]]}
    sources = operator_sources.OperatorSources(
        cfg=cfg, host_id="host-stable", provider=_Provider(), journal_base=tmp_path
    )
    journal_root = run_journal.journal_root_for_hive(HIVE_ONE, base=tmp_path)
    journal_root.mkdir(parents=True)
    chmod_target = journal_root
    if source_shape == "unreadable":
        journal_root.chmod(0)
    elif source_shape == "unreadable-file":
        chmod_target = journal_root / "run-unreadable.jsonl"
        chmod_target.write_text("{}\n")
        chmod_target.chmod(0)
    elif source_shape == "empty-file":
        (journal_root / "run-empty.jsonl").touch()
    directory = daemon_factory.FactoryDirectory(
        sources=sources,
        host_id="host-stable",
        service_instance_id="instance-changing",
        started_at=1_000,
        describe_hive=lambda _hive: daemon_factory.HiveSourceObservation("ready", "complete"),
        load_assignment=lambda _hive: daemon_factory.AssignmentObservation(
            "host-stable", "executor", "held"
        ),
        hq_status=lambda: daemon_factory.DependencyObservation("ready"),
        dolt_status=lambda _hives: daemon_factory.DependencyObservation("ready"),
    )

    try:
        payload = directory.snapshot(ready=True, accepting_work=True)
    finally:
        chmod_target.chmod(0o700)
    dependencies = {item["name"]: item for item in payload["status"]["dependencies"]}

    assert dependencies["run-journals"] == {
        "name": "run-journals",
        "status": expected_status,
        "reasonCode": expected_reason,
    }
    assert payload["coverage"]["sources"]["run-journals"]["state"] == expected_coverage
    assert payload["coverage"]["sources"]["run-journals"]["detail"] == expected_reason


def test_factory_degrades_when_journal_record_disagrees_with_authoritative_hive_root(
    tmp_path: Path,
) -> None:
    cfg = {"managed_repos": [_cfg()["managed_repos"][0]]}
    sources = operator_sources.OperatorSources(
        cfg=cfg, host_id="host-stable", provider=_Provider(), journal_base=tmp_path
    )
    journal_root = run_journal.journal_root_for_hive(HIVE_ONE, base=tmp_path)
    journal_root.mkdir(parents=True)
    _write_valid_journal(journal_root / "run-wrong.jsonl", run_id="run-wrong", hive=HIVE_TWO)
    directory = daemon_factory.FactoryDirectory(
        sources=sources,
        host_id="host-stable",
        service_instance_id="instance-changing",
        started_at=1_000,
        clock_millis=lambda: 1_500,
        describe_hive=lambda _hive: daemon_factory.HiveSourceObservation("ready", "complete"),
        load_assignment=lambda _hive: daemon_factory.AssignmentObservation(
            "host-stable", "executor", "held"
        ),
        hq_status=lambda: daemon_factory.DependencyObservation("ready"),
        dolt_status=lambda _hives: daemon_factory.DependencyObservation("ready"),
    )

    payload = directory.snapshot(ready=True, accepting_work=True)
    dependency = next(
        item for item in payload["status"]["dependencies"] if item["name"] == "run-journals"
    )

    assert dependency == {
        "name": "run-journals",
        "status": "unavailable",
        "reasonCode": "hive_identity_mismatch",
    }
    assert payload["coverage"]["sources"]["run-journals"] == {
        "state": "unavailable",
        "detail": "hive_identity_mismatch",
        "generatedAt": 1_500,
    }


def test_factory_disables_activity_read_when_inventory_limit_hides_identity_truth(
    tmp_path: Path,
) -> None:
    cfg = {"managed_repos": [_cfg()["managed_repos"][0]]}
    sources = operator_sources.OperatorSources(
        cfg=cfg,
        host_id="host-stable",
        provider=_Provider(),
        journal_base=tmp_path,
        max_inventory_entries=1,
    )
    root = run_journal.journal_root_for_hive(HIVE_ONE, base=tmp_path)
    root.mkdir(parents=True)
    _write_valid_journal(root / "run-one.jsonl", run_id="run-one")
    _write_valid_journal(root / "run-two.jsonl", run_id="run-two")
    directory = daemon_factory.FactoryDirectory(
        sources=sources,
        host_id="host-stable",
        service_instance_id="instance-changing",
        started_at=1_000,
        clock_millis=lambda: 1_500,
        describe_hive=lambda _hive: daemon_factory.HiveSourceObservation("ready", "complete"),
        load_assignment=lambda _hive: daemon_factory.AssignmentObservation(
            "host-stable", "executor", "held"
        ),
        hq_status=lambda: daemon_factory.DependencyObservation("ready"),
        dolt_status=lambda _hives: daemon_factory.DependencyObservation("ready"),
    )

    payload = directory.snapshot(ready=True, accepting_work=True)
    capabilities = {item["name"]: item for item in payload["capabilities"]}
    dependency = next(
        item for item in payload["status"]["dependencies"] if item["name"] == "run-journals"
    )

    assert dependency == {
        "name": "run-journals",
        "status": "degraded",
        "reasonCode": "inventory_limit_exceeded",
    }
    assert capabilities["activity-read"] == {
        "name": "activity-read",
        "available": False,
        "reasonCode": "inventory_limit_exceeded",
    }


def test_factory_mixed_valid_and_empty_roots_disables_activity_read(tmp_path: Path) -> None:
    sources = operator_sources.OperatorSources(
        cfg=_cfg(), host_id="host-stable", provider=_Provider(), journal_base=tmp_path
    )
    first_root = run_journal.journal_root_for_hive(HIVE_ONE, base=tmp_path)
    second_root = run_journal.journal_root_for_hive(HIVE_TWO, base=tmp_path)
    first_root.mkdir(parents=True)
    second_root.mkdir(parents=True)
    _write_valid_journal(first_root / "run-visible.jsonl", run_id="run-visible")
    directory = daemon_factory.FactoryDirectory(
        sources=sources,
        host_id="host-stable",
        service_instance_id="instance-changing",
        started_at=1_000,
        clock_millis=lambda: 1_500,
        describe_hive=lambda _hive: daemon_factory.HiveSourceObservation("ready", "complete"),
        load_assignment=lambda _hive: daemon_factory.AssignmentObservation(
            "host-stable", "executor", "held"
        ),
        hq_status=lambda: daemon_factory.DependencyObservation("ready"),
        dolt_status=lambda _hives: daemon_factory.DependencyObservation("ready"),
    )

    payload = directory.snapshot(ready=True, accepting_work=True)
    capabilities = {item["name"]: item for item in payload["capabilities"]}
    dependency = next(
        item for item in payload["status"]["dependencies"] if item["name"] == "run-journals"
    )

    assert dependency == {
        "name": "run-journals",
        "status": "degraded",
        "reasonCode": "source_empty",
    }
    assert capabilities["activity-read"] == {
        "name": "activity-read",
        "available": False,
        "reasonCode": "source_empty",
    }


@pytest.mark.parametrize(
    ("shared_mode", "configured_endpoint"),
    [(True, ("127.0.0.1", 4308)), (False, ("192.0.2.44", 5308))],
    ids=["shared", "external"],
)
def test_factory_observes_configured_server_loss_bounded_without_fallback_or_shadow_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shared_mode: bool,
    configured_endpoint: tuple[str, int],
) -> None:
    cfg = {"managed_repos": [_cfg()["managed_repos"][0]]}
    sources = operator_sources.OperatorSources(
        cfg=cfg, host_id="host-stable", provider=_Provider(), journal_base=tmp_path / "journals"
    )
    hive_dir = tmp_path / "hive"
    metadata = hive_dir / ".beads" / "metadata.json"
    metadata.parent.mkdir(parents=True)
    metadata.write_text(json.dumps({"dolt_mode": "server"}))
    monkeypatch.setattr(daemon_factory.registry, "hive_dir", lambda _entry: hive_dir)
    monkeypatch.setenv(dolt_health.ENV_SERVER_HOST, configured_endpoint[0])
    monkeypatch.setenv(dolt_health.ENV_SERVER_PORT, str(configured_endpoint[1]))
    if shared_mode:
        monkeypatch.setenv(dolt_health.ENV_SHARED_SERVER, "true")
    else:
        monkeypatch.delenv(dolt_health.ENV_SHARED_SERVER, raising=False)
    probes: list[tuple[str, int, float]] = []

    def unavailable(host: str, port: int, *, timeout: float) -> dolt_health.ProbeResult:
        probes.append((host, port, timeout))
        return dolt_health.ProbeResult(False, "private endpoint detail")

    monkeypatch.setattr(daemon_factory.dolt_health, "probe_endpoint", unavailable)
    before = tuple(sorted(path.relative_to(hive_dir) for path in hive_dir.rglob("*")))
    directory = daemon_factory.FactoryDirectory(
        sources=sources,
        host_id="host-stable",
        service_instance_id="instance-changing",
        started_at=1_000,
        dolt_probe_timeout_seconds=0.125,
        describe_hive=lambda _hive: daemon_factory.HiveSourceObservation("ready", "complete"),
        load_run_directory=lambda _hives: RunDirectoryInventory((), Coverage.COMPLETE, None),
        load_assignment=lambda _hive: daemon_factory.AssignmentObservation(
            "host-stable", "executor", "held"
        ),
        hq_status=lambda: daemon_factory.DependencyObservation("ready"),
    )

    payload = directory.snapshot(ready=True, accepting_work=True)
    dependencies = {item["name"]: item for item in payload["status"]["dependencies"]}
    after = tuple(sorted(path.relative_to(hive_dir) for path in hive_dir.rglob("*")))

    assert probes == [(*configured_endpoint, 0.125)]
    assert dependencies["dolt"] == {
        "name": "dolt",
        "status": "unavailable",
        "reasonCode": "dolt_server_unavailable",
    }
    assert dependencies["bead-state"]["status"] == "ready"
    assert before == after
    assert not (hive_dir / ".beads" / "embeddeddolt").exists()
    assert "private endpoint detail" not in json.dumps(payload)


@pytest.mark.parametrize(
    ("metadata_shape", "expected_status", "expected_reason"),
    [
        ("missing", "unavailable", "dolt_embedded_metadata_missing"),
        ("malformed", "unavailable", "dolt_embedded_metadata_invalid"),
        ("valid", "ready", None),
    ],
)
def test_factory_requires_valid_embedded_dolt_repository_metadata_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    metadata_shape: str,
    expected_status: str,
    expected_reason: str | None,
) -> None:
    cfg = {"managed_repos": [_cfg()["managed_repos"][0]]}
    sources = operator_sources.OperatorSources(
        cfg=cfg, host_id="host-stable", provider=_Provider(), journal_base=tmp_path / "journals"
    )
    hive_dir = tmp_path / "hive"
    metadata = hive_dir / ".beads" / "metadata.json"
    metadata.parent.mkdir(parents=True)
    metadata.write_text(json.dumps({"dolt_mode": "embedded", "dolt_database": "store"}))
    database = hive_dir / ".beads" / "embeddeddolt" / "store"
    dolt_dir = database / ".dolt"
    dolt_dir.mkdir(parents=True)
    if metadata_shape != "missing":
        state = dolt_dir / "repo_state.json"
        state.write_text(
            "not-json"
            if metadata_shape == "malformed"
            else json.dumps(
                {"head": "refs/heads/main", "branches": {}, "remotes": {}, "backups": {}}
            )
        )
        noms = dolt_dir / "noms"
        noms.mkdir()
        (noms / "manifest").write_text(DOLT_MANIFEST)
    monkeypatch.setattr(daemon_factory.registry, "hive_dir", lambda _entry: hive_dir)
    before = tuple(sorted(path.relative_to(hive_dir) for path in hive_dir.rglob("*")))
    directory = daemon_factory.FactoryDirectory(
        sources=sources,
        host_id="host-stable",
        service_instance_id="instance-changing",
        started_at=1_000,
        describe_hive=lambda _hive: daemon_factory.HiveSourceObservation("ready", "complete"),
        load_run_directory=lambda _hives: RunDirectoryInventory((), Coverage.COMPLETE, None),
        load_assignment=lambda _hive: daemon_factory.AssignmentObservation(
            "host-stable", "executor", "held"
        ),
        hq_status=lambda: daemon_factory.DependencyObservation("ready"),
    )

    payload = directory.snapshot(ready=True, accepting_work=True)
    dependencies = {item["name"]: item for item in payload["status"]["dependencies"]}
    after = tuple(sorted(path.relative_to(hive_dir) for path in hive_dir.rglob("*")))

    expected = {"name": "dolt", "status": expected_status, "reasonCode": expected_reason}
    assert dependencies["dolt"] == expected
    assert before == after


@pytest.mark.parametrize(
    "metadata_shape",
    [
        "symlink-repository",
        "symlink-noms",
        "special-noms",
        "symlink-state",
        "special-state",
        "symlink-manifest",
        "special-manifest",
    ],
)
def test_factory_rejects_symlinked_or_special_embedded_dolt_metadata_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    metadata_shape: str,
) -> None:
    cfg = {"managed_repos": [_cfg()["managed_repos"][0]]}
    sources = operator_sources.OperatorSources(
        cfg=cfg, host_id="host-stable", provider=_Provider(), journal_base=tmp_path / "journals"
    )
    hive_dir = tmp_path / "hive"
    metadata = hive_dir / ".beads" / "metadata.json"
    metadata.parent.mkdir(parents=True)
    metadata.write_text(json.dumps({"dolt_mode": "embedded", "dolt_database": "store"}))
    database = hive_dir / ".beads" / "embeddeddolt" / "store"
    database.mkdir(parents=True)
    real_dolt = tmp_path / "real-dolt"
    real_dolt.mkdir()
    state_payload = json.dumps(
        {"head": "refs/heads/main", "branches": {}, "remotes": {}, "backups": {}}
    )

    if metadata_shape == "symlink-repository":
        (real_dolt / "repo_state.json").write_text(state_payload)
        (real_dolt / "noms").mkdir()
        (real_dolt / "noms" / "manifest").write_text(DOLT_MANIFEST)
        (database / ".dolt").symlink_to(real_dolt, target_is_directory=True)
    else:
        dolt_dir = database / ".dolt"
        dolt_dir.mkdir()
        real_state = tmp_path / "repo-state.json"
        real_state.write_text(state_payload)
        if metadata_shape == "symlink-state":
            (dolt_dir / "repo_state.json").symlink_to(real_state)
        elif metadata_shape == "special-state":
            (dolt_dir / "repo_state.json").mkdir()
        else:
            (dolt_dir / "repo_state.json").write_text(state_payload)
        if metadata_shape == "symlink-noms":
            real_noms = tmp_path / "real-noms"
            real_noms.mkdir()
            (real_noms / "manifest").write_text(DOLT_MANIFEST)
            (dolt_dir / "noms").symlink_to(real_noms, target_is_directory=True)
        elif metadata_shape == "special-noms":
            (dolt_dir / "noms").write_text("not a directory\n")
        else:
            (dolt_dir / "noms").mkdir()
            manifest = dolt_dir / "noms" / "manifest"
            if metadata_shape == "symlink-manifest":
                real_manifest = tmp_path / "manifest"
                real_manifest.write_text(DOLT_MANIFEST)
                manifest.symlink_to(real_manifest)
            elif metadata_shape == "special-manifest":
                manifest.mkdir()
            else:
                manifest.write_text(DOLT_MANIFEST)

    monkeypatch.setattr(daemon_factory.registry, "hive_dir", lambda _entry: hive_dir)
    before = tuple(sorted(path.relative_to(hive_dir) for path in hive_dir.rglob("*")))
    directory = daemon_factory.FactoryDirectory(
        sources=sources,
        host_id="host-stable",
        service_instance_id="instance-changing",
        started_at=1_000,
        describe_hive=lambda _hive: daemon_factory.HiveSourceObservation("ready", "complete"),
        load_run_directory=lambda _hives: RunDirectoryInventory((), Coverage.COMPLETE, None),
        load_assignment=lambda _hive: daemon_factory.AssignmentObservation(
            "host-stable", "executor", "held"
        ),
        hq_status=lambda: daemon_factory.DependencyObservation("ready"),
    )

    payload = directory.snapshot(ready=True, accepting_work=True)
    dependency = next(item for item in payload["status"]["dependencies"] if item["name"] == "dolt")
    after = tuple(sorted(path.relative_to(hive_dir) for path in hive_dir.rglob("*")))

    assert dependency["status"] == "unavailable"
    assert dependency["reasonCode"] == "dolt_embedded_metadata_invalid"
    assert before == after


def _write_valid_dolt_metadata(database: Path) -> None:
    dolt_dir = database / ".dolt"
    noms = dolt_dir / "noms"
    noms.mkdir(parents=True)
    (dolt_dir / "repo_state.json").write_text(
        json.dumps({"head": "refs/heads/main", "branches": {}, "remotes": {}, "backups": {}})
    )
    (noms / "manifest").write_text(DOLT_MANIFEST)


@pytest.mark.parametrize("metadata_name", ["repo_state.json", "manifest"])
def test_embedded_dolt_rejects_multiply_linked_metadata(tmp_path: Path, metadata_name: str) -> None:
    database = tmp_path / "store"
    _write_valid_dolt_metadata(database)
    metadata = (
        database / ".dolt" / "repo_state.json"
        if metadata_name == "repo_state.json"
        else database / ".dolt" / "noms" / "manifest"
    )
    os.link(metadata, tmp_path / f"external-{metadata_name}")

    result = dolt_health.probe_embedded_schema_version(database, metadata_only=True)

    assert result.metadata_valid is False
    assert result.reason_code == "dolt_embedded_metadata_multiply_linked"


@pytest.mark.parametrize("symlink_shape", ["database", "ancestor"])
def test_embedded_dolt_metadata_rejects_symlink_database_or_ancestor(
    tmp_path: Path, symlink_shape: str
) -> None:
    real_parent = tmp_path / "real-parent"
    real_database = real_parent / "store"
    _write_valid_dolt_metadata(real_database)
    if symlink_shape == "database":
        candidate = tmp_path / "store-link"
        candidate.symlink_to(real_database, target_is_directory=True)
    else:
        linked_parent = tmp_path / "parent-link"
        linked_parent.symlink_to(real_parent, target_is_directory=True)
        candidate = linked_parent / "store"

    result = dolt_health.probe_embedded_schema_version(candidate, metadata_only=True)

    assert result.metadata_valid is False
    assert result.reason_code == "dolt_embedded_metadata_invalid"


def test_embedded_dolt_metadata_reads_only_from_no_follow_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "store"
    _write_valid_dolt_metadata(database)

    def forbid_path_reopen(_path: Path) -> bytes:
        raise AssertionError("metadata must be read from the validated descriptor")

    monkeypatch.setattr(Path, "read_bytes", forbid_path_reopen)

    result = dolt_health.probe_embedded_schema_version(database, metadata_only=True)

    assert result.metadata_valid is True


@pytest.mark.parametrize(
    ("metadata_shape", "expected_reason"),
    [
        ("garbage-manifest", "dolt_embedded_metadata_invalid"),
        ("unreadable-state", "dolt_embedded_metadata_unavailable"),
        ("unreadable-manifest", "dolt_embedded_metadata_unavailable"),
    ],
)
def test_embedded_dolt_metadata_rejects_garbage_and_mode_zero_files(
    tmp_path: Path, metadata_shape: str, expected_reason: str
) -> None:
    database = tmp_path / "store"
    _write_valid_dolt_metadata(database)
    state = database / ".dolt" / "repo_state.json"
    manifest = database / ".dolt" / "noms" / "manifest"
    target = manifest if metadata_shape != "unreadable-state" else state
    if metadata_shape == "garbage-manifest":
        manifest.write_text("not a Dolt manifest\n")
    else:
        target.chmod(0)
    try:
        result = dolt_health.probe_embedded_schema_version(database, metadata_only=True)
    finally:
        target.chmod(0o600)

    assert result.metadata_valid is False
    assert result.reason_code == expected_reason


@pytest.mark.parametrize("database_shape", ["absolute", "traversal"])
def test_factory_rejects_embedded_database_outside_the_hive_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, database_shape: str
) -> None:
    external = tmp_path / "external"
    _write_valid_dolt_metadata(external)
    hive_dir = tmp_path / "hive"
    metadata = hive_dir / ".beads" / "metadata.json"
    metadata.parent.mkdir(parents=True)
    configured = str(external) if database_shape == "absolute" else "../../../external"
    metadata.write_text(json.dumps({"dolt_mode": "embedded", "dolt_database": configured}))
    sources = operator_sources.OperatorSources(
        cfg={"managed_repos": [_cfg()["managed_repos"][0]]},
        host_id="host-stable",
        provider=_Provider(),
        journal_base=tmp_path / "journals",
    )
    monkeypatch.setattr(daemon_factory.registry, "hive_dir", lambda _entry: hive_dir)
    directory = daemon_factory.FactoryDirectory(
        sources=sources,
        host_id="host-stable",
        service_instance_id="instance-changing",
        started_at=1_000,
        describe_hive=lambda _hive: daemon_factory.HiveSourceObservation("ready", "complete"),
        load_run_directory=lambda _hives: RunDirectoryInventory((), Coverage.COMPLETE, None),
        load_assignment=lambda _hive: daemon_factory.AssignmentObservation(
            "host-stable", "executor", "held"
        ),
        hq_status=lambda: daemon_factory.DependencyObservation("ready"),
    )

    payload = directory.snapshot(ready=True, accepting_work=True)
    dependency = next(item for item in payload["status"]["dependencies"] if item["name"] == "dolt")

    assert dependency == {
        "name": "dolt",
        "status": "unavailable",
        "reasonCode": "dolt_embedded_database_invalid",
    }


def _settings(path: Path) -> HostDaemonConfig:
    return HostDaemonConfig(
        enabled=True,
        auth={"credential_file": path.absolute()},
        http={"allowed_hosts": ["127.0.0.1"]},
        cors={"allowed_origins": ["https://operator.example"]},
        activity={
            "max_body_bytes": 4_096,
            "max_records_per_read": 7,
            "max_inventory_roots": 11,
            "max_inventory_entries": 13,
            "max_inventory_bytes": 65_536,
        },
        status={
            "dependency_probe_timeout_seconds": 0.25,
            "run_journal_stale_after_seconds": 45.0,
        },
    )


def test_product_factory_is_authenticated_and_health_stays_public_minimal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credential_path = (tmp_path / "credentials.json").absolute()
    credential = daemon_auth.provision_credential_file(
        credential_path,
        credential_id="operator",
        audience="beadhive-host",
        principal="operator:one",
        scopes=(AuthScope.OPERATOR_READ,),
        expires_at=int(time.time()) + 3_600,
    )
    wrong_scope = daemon_auth.add_credential(
        credential_path,
        credential_id="mcp-only",
        audience="beadhive-host",
        principal="operator:mcp-only",
        scopes=(AuthScope.MCP_CONTROL,),
        expires_at=int(time.time()) + 3_600,
    )
    record = host_daemon.ControlRecord(
        contract=host_daemon.CONTRACT_VERSION,
        account_id="uid:1",
        bh_home=str(tmp_path),
        host_id="host-stable",
        instance_id="instance-changing",
        pid=os.getpid(),
        process_start="test:1",
        listener_host="127.0.0.1",
        listener_port=8737,
        started_at="2026-09-02T00:00:00Z",
    )
    runtime = host_daemon.DaemonRuntime()
    app = host_daemon.build_product_application(
        runtime=runtime,
        control_record=record,
        cfg={"managed_repos": []},
        settings=_settings(credential_path),
    )

    async def exercise():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5000))
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1:8737"
            ) as client:
                health = await client.get("/health")
                missing = await client.get("/api/v1/factory")
                factory = await client.get(
                    "/api/v1/factory",
                    headers={
                        "Authorization": (f"Bearer {credential.bearer.reveal_for_authority()}")
                    },
                )
                factory_preflight = await client.options(
                    "/api/v1/factory",
                    headers={
                        "Origin": "https://operator.example",
                        "Access-Control-Request-Method": "GET",
                        "Access-Control-Request-Headers": "Content-Type, Authorization",
                    },
                )
                unknown_preflight = await client.options(
                    "/not-a-route",
                    headers={
                        "Origin": "https://operator.example",
                        "Access-Control-Request-Method": "GET",
                        "Access-Control-Request-Headers": "Authorization",
                    },
                )

                def fail_factory(**_kwargs):
                    raise OSError("private downstream detail")

                monkeypatch.setattr(
                    app.state.operator_api.factory_directory,
                    "snapshot",
                    fail_factory,
                )
                unavailable = await client.get(
                    "/api/v1/factory",
                    headers={
                        "Authorization": (f"Bearer {credential.bearer.reveal_for_authority()}")
                    },
                )
                read_only = await client.post(
                    "/api/v1/factory",
                    headers={
                        "Authorization": (f"Bearer {credential.bearer.reveal_for_authority()}")
                    },
                )
                runtime.begin_shutdown()
                drain_missing = await client.get("/api/v1/factory")
                drain_wrong_scope = await client.get(
                    "/api/v1/factory",
                    headers={
                        "Authorization": (f"Bearer {wrong_scope.bearer.reveal_for_authority()}")
                    },
                )
                drain = await client.get(
                    "/api/v1/factory",
                    headers={
                        "Authorization": (f"Bearer {credential.bearer.reveal_for_authority()}")
                    },
                )
                drain_read_only = await client.post(
                    "/not-a-route",
                    headers={
                        "Authorization": (f"Bearer {credential.bearer.reveal_for_authority()}")
                    },
                )
                return (
                    health,
                    missing,
                    factory,
                    factory_preflight,
                    unknown_preflight,
                    unavailable,
                    read_only,
                    drain_missing,
                    drain_wrong_scope,
                    drain,
                    drain_read_only,
                )

    (
        health,
        missing,
        factory,
        factory_preflight,
        unknown_preflight,
        unavailable,
        read_only,
        drain_missing,
        drain_wrong_scope,
        drain,
        drain_read_only,
    ) = asyncio.run(exercise())
    assert health.status_code == 200
    assert health.json() == {
        "schemaVersion": 1,
        "status": "live",
        "ready": True,
        "contract": daemon_contract.CONTRACT_VERSION,
    }
    assert missing.status_code == 401
    assert factory.status_code == 200
    assert daemon_contract.FactoryResponse.model_validate(factory.json())
    assert set(health.json()) == {"schemaVersion", "status", "ready", "contract"}
    checked = operator_api.openapi_document()
    schemas = checked["components"]["schemas"]
    for name, payload in (
        ("HealthResponse", health.json()),
        ("FactoryResponse", factory.json()),
        ("Error", missing.json()),
        ("Error", unavailable.json()),
    ):
        jsonschema.Draft202012Validator(
            {"components": {"schemas": schemas}, "$ref": f"#/components/schemas/{name}"}
        ).validate(payload)
    assert missing.json() == {
        "schemaVersion": 1,
        "error": {
            "code": "unauthorized",
            "message": "Authentication is required.",
            "retryable": False,
            "action": None,
            "requestId": None,
        },
    }
    assert factory_preflight.status_code == unknown_preflight.status_code == 204
    assert factory_preflight.headers["access-control-allow-headers"] == (
        "authorization, content-type"
    )
    assert unknown_preflight.headers["access-control-allow-headers"] == "authorization"
    assert unavailable.status_code == 503
    assert unavailable.json() == {
        "schemaVersion": 1,
        "error": {
            "code": "factory_source_unavailable",
            "message": "The authoritative factory source is unavailable.",
            "retryable": True,
            "action": None,
            "requestId": None,
        },
    }
    assert "private downstream detail" not in unavailable.text
    assert drain_missing.status_code == 401
    assert drain_wrong_scope.status_code == 403
    for response in (read_only, drain_read_only):
        assert response.status_code == 405
        assert response.headers["content-type"] == "application/json"
        jsonschema.Draft202012Validator(
            {"components": {"schemas": schemas}, "$ref": "#/components/schemas/Error"}
        ).validate(response.json())
        assert response.json()["error"]["code"] == "read_only_profile"
    assert drain.status_code == 503
    assert drain.headers["content-type"] == "application/json"
    jsonschema.Draft202012Validator(
        {"components": {"schemas": schemas}, "$ref": "#/components/schemas/Error"}
    ).validate(drain.json())
    assert drain.json() == {
        "schemaVersion": 1,
        "error": {
            "code": "daemon_draining",
            "message": "The daemon is draining and cannot accept new work.",
            "retryable": True,
            "action": "retry",
            "requestId": None,
        },
    }
    assert checked["security"] == [{"BearerAuth": []}]
    assert (
        checked["components"]["securitySchemes"]["BearerAuth"]["x-beadhive-required-scope"]
        == "operator:read"
    )
    for path, item in checked["paths"].items():
        effective_security = item["get"].get("security", checked["security"])
        assert effective_security == ([] if path == "/health" else [{"BearerAuth": []}])
        if path != "/health":
            assert item["get"]["responses"]["401"] == {
                "$ref": "#/components/responses/Unauthorized"
            }
    assert checked["paths"]["/health"]["get"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ] == {"$ref": "#/components/schemas/HealthResponse"}
    assert checked["paths"]["/api/v1/factory"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"] == {"$ref": "#/components/schemas/FactoryResponse"}
    assert checked["paths"]["/api/v1/factory"]["options"]["responses"]["204"]
    assert checked["x-beadhive-secure-network-preflight"] == {
        "pathScope": "all request paths, including paths absent from this document",
        "successStatus": 204,
        "allowedHeaders": [
            "accept",
            "authorization",
            "content-type",
            "last-event-id",
            "mcp-protocol-version",
            "mcp-session-id",
        ],
        "responseHeaderSpelling": "lower-case, lexical, comma-space separated",
    }
    assert checked["paths"]["/api/v1/hives/{hive_id}/events"]["options"]["responses"]["204"][
        "headers"
    ]["Access-Control-Allow-Headers"]["schema"] == {"const": "last-event-id"}
    assert app.state.operator_api.factory_directory.dolt_probe_timeout_seconds == 0.25
    assert app.state.operator_api.factory_directory.dependency_probe_timeout_seconds == 0.25
    assert app.state.operator_api.factory_directory.journal_stale_after_seconds == 45.0
    assert app.state.operator_api.sources.max_records_per_read == 7
    assert app.state.operator_api.sources.max_record_bytes == 4_096
    assert app.state.operator_api.sources.max_inventory_roots == 11
    assert app.state.operator_api.sources.max_inventory_entries == 13
    assert app.state.operator_api.sources.max_inventory_bytes == 65_536
    assert app.state.operator_api.feed.max_cached_activity_runs == 7
    assert app.state.operator_api.feed.max_cached_activity_bytes == 7 * 4_096
