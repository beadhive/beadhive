"""Atomic snapshot/activity installation and cursor tests."""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

from beadhive import (
    operator_contract,
    operator_feed,
    operator_sources,
    run_journal,
    state_stream,
)
from beadhive.agent_run_summary import Freshness
from beadhive.public_readers import AgentRunSnapshot, Coverage

NOW = datetime(2026, 8, 24, tzinfo=UTC).isoformat().replace("+00:00", "Z")
DIGEST = "sha256:" + "a" * 64
HIVE = "github/beadhive/beadhive"


def _cfg() -> dict:
    return {
        "managed_repos": [
            {
                "provider": "github",
                "org": "beadhive",
                "repo": "beadhive",
                "prefix": "bh",
                "kind": "org-native",
            }
        ]
    }


def _snapshot(revision: str, status: str) -> state_stream.ProviderSnapshot:
    return state_stream.ProviderSnapshot(
        scope="hive",
        revision=revision,
        as_of=NOW,
        issues=(
            state_stream.StreamIssue(
                id="bh-1",
                hive=HIVE,
                issue_type="task",
                status=status,
                priority="P1",
                title="Atomic feed",
                updated_at=NOW,
            ),
        ),
    )


def _runtime(host: str, source: str) -> AgentRunSnapshot:
    return AgentRunSnapshot(
        host_id=host,
        source_id=source,
        revision="runtime-1",
        summaries=(),
        coverage=Coverage.UNKNOWN,
        coverage_reason="source_missing",
        freshness=Freshness(),
    )


class MutableProvider:
    def __init__(self) -> None:
        self.current = _snapshot("beads-1", "open")
        self.captured: threading.Event | None = None
        self.release: threading.Event | None = None

    def refresh(self, _request):
        captured = self.current
        if self.captured is not None:
            self.captured.set()
        if self.release is not None:
            assert self.release.wait(2)
        return captured


def _sources(tmp_path: Path, provider: MutableProvider) -> operator_sources.OperatorSources:
    return operator_sources.OperatorSources(
        cfg=_cfg(),
        host_id="host-1",
        provider=provider,
        summary_reader=lambda _path, host, source: _runtime(host, source),
        journal_base=tmp_path,
        dispatch_sink_for_entry=lambda _cfg, _entry: tmp_path / "dispatch.jsonl",
    )


def _record(revision: str, timestamp: int, *, run_id: str = "run-1") -> dict:
    return {
        "version": run_journal.VERSION,
        "source_revision": revision,
        "timestamp_ms": timestamp,
        "run_id": run_id,
        "hive": HIVE,
        "bead": "bh-1",
        "driver": "baml",
        "provider": "claude-code",
        "manifest_digest": DIGEST,
        "provider_continuation": None,
        "writer": run_journal.WRITER_LOCAL_LOOP,
        "activity": {"kind": "run.created" if timestamp == 1 else "process.spawned"},
    }


def _journal(tmp_path: Path, records: list[dict], *, run_id: str = "run-1") -> Path:
    path = run_journal.journal_path_for_hive(HIVE, run_id, base=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    return path


def test_snapshot_is_direct_canonical_and_same_revision_keeps_cursor(tmp_path: Path) -> None:
    provider = MutableProvider()
    feed = operator_feed.OperatorFeed(_sources(tmp_path, provider), now_millis=lambda: 1000)
    first = feed.snapshot_with_cursor(HIVE)
    second = feed.snapshot_with_cursor(HIVE)

    assert first is second
    assert first["hive"]["prefix"] == HIVE
    assert first["workItems"][0]["ref"]["hiveId"] == HIVE
    assert first["cursor"] == {
        "subscriptionId": operator_contract.hive_subscription_id(HIVE),
        "producerEpoch": first["cursor"]["producerEpoch"],
        "sequence": 0,
        "observedAt": 1000,
    }


def test_hive_subscription_id_is_stable_opaque_and_cross_hive_isolated() -> None:
    subscription = operator_contract.hive_subscription_id(HIVE)

    assert subscription == operator_contract.hive_subscription_id(HIVE)
    assert subscription.startswith("hive-sha256-")
    assert ":" not in subscription and "/" not in subscription
    assert subscription != operator_contract.hive_subscription_id("github/beadhive/second")


def test_concurrent_change_is_old_snapshot_then_strictly_later_install(tmp_path: Path) -> None:
    provider = MutableProvider()
    captured = provider.captured = threading.Event()
    release = provider.release = threading.Event()
    feed = operator_feed.OperatorFeed(_sources(tmp_path, provider), now_millis=lambda: 1000)
    installs: list[operator_feed.FeedInstall] = []
    feed.register_install_observer(installs.append)
    responses = []

    worker = threading.Thread(target=lambda: responses.append(feed.snapshot_with_cursor(HIVE)))
    worker.start()
    assert captured.wait(2)
    provider.current = _snapshot("beads-2", "closed")
    release.set()
    worker.join(2)
    assert not worker.is_alive()

    old = responses[0]
    provider.captured = provider.release = None
    new = feed.snapshot_with_cursor(HIVE)
    assert old["workItems"][0]["record"]["status"] == "open"
    assert new["workItems"][0]["record"]["status"] == "closed"
    assert old["cursor"]["sequence"] == 0
    assert new["cursor"]["sequence"] == 1
    assert installs[-1].current["cursor"]["sequence"] > old["cursor"]["sequence"]


def test_hive_admissions_are_bounded_without_coupling_unrelated_removal(tmp_path: Path) -> None:
    provider = MutableProvider()
    provider.captured = threading.Event()
    provider.release = threading.Event()
    feed = operator_feed.OperatorFeed(
        _sources(tmp_path, provider),
        max_cached_activity_runs=1,
        max_cached_activity_bytes=16 * 1_048_576,
    )
    responses: list[dict[str, object]] = []
    failures: list[BaseException] = []

    def blocked_snapshot() -> None:
        try:
            responses.append(feed.snapshot_with_cursor(HIVE))
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=blocked_snapshot)
    worker.start()
    assert provider.captured.wait(2)
    assert feed.hive_admissions == feed.max_hive_admissions == 1

    with pytest.raises(operator_sources.OperatorSourceError) as saturated:
        feed.snapshot_with_cursor("github/unknown/saturated")
    assert (saturated.value.code, saturated.value.status_code) == (
        "hive_admission_capacity",
        503,
    )
    assert feed.hive_admission_rejections == 1

    unrelated_removal = threading.Thread(target=feed.remove_hive, args=("github/beadhive/other",))
    unrelated_removal.start()
    unrelated_removal.join(2)
    assert not unrelated_removal.is_alive()

    provider.release.set()
    worker.join(2)
    assert not worker.is_alive()
    assert not failures
    assert len(responses) == 1
    assert feed.hive_admissions == 0

    old_epoch = responses[0]["cursor"]["producerEpoch"]
    feed.remove_hive(HIVE)
    replacement = feed.snapshot_with_cursor(HIVE)
    assert replacement["cursor"]["producerEpoch"] != old_epoch


def test_relay_transition_handler_allocates_exact_event_count_before_snapshot_returns(
    tmp_path: Path,
) -> None:
    provider = MutableProvider()
    feed = operator_feed.OperatorFeed(_sources(tmp_path, provider), now_millis=lambda: 1000)
    first = feed.snapshot_with_cursor(HIVE)
    transitions: list[operator_feed.FeedTransition] = []
    finalized: list[operator_feed.FeedInstall] = []
    feed.register_transition_handler(lambda transition: transitions.append(transition) or 3)
    feed.register_install_observer(finalized.append)

    provider.current = _snapshot("beads-2", "closed")
    second = feed.snapshot_with_cursor(HIVE)
    assert transitions[0].base_sequence == first["cursor"]["sequence"] == 0
    assert transitions[0].current["cursor"]["sequence"] == 3
    assert second["cursor"]["sequence"] == 3
    assert finalized[0].current["cursor"]["sequence"] == 3


def test_activity_snapshot_and_delta_keep_epoch_and_sequence_separate_from_revision(
    tmp_path: Path,
) -> None:
    provider = MutableProvider()
    feed = operator_feed.OperatorFeed(_sources(tmp_path, provider))
    first_record = _record("opaque:first", 1)
    _journal(tmp_path, [first_record])

    first = feed.activity_with_cursor("run-1")
    epoch = first["producerEpoch"]
    assert first["kind"] == "snapshot"
    assert first["sequence"] == 1
    assert first["sourceRevision"] == "opaque:first"
    assert first["activities"][0]["sequence"] == 1
    assert first["activities"][0]["payload"]["text"] is None

    second_record = _record("opaque:second", 2)
    _journal(tmp_path, [first_record, second_record])
    delta = feed.activity_with_cursor("run-1", after=(epoch, 1))
    assert delta["kind"] == "delta"
    assert delta["producerEpoch"] == epoch
    assert (delta["baseSequence"], delta["sequence"]) == (1, 2)
    assert [item["sequence"] for item in delta["activities"]] == [2]
    assert delta["activities"][0]["sourceRevision"] == "opaque:second"


def test_activity_observers_run_after_the_publication_pin_is_released(tmp_path: Path) -> None:
    provider = MutableProvider()
    feed = operator_feed.OperatorFeed(_sources(tmp_path, provider))
    _journal(tmp_path, [_record("opaque:first", 1)])
    observer_owned_publication_pin: list[bool] = []

    feed.register_activity_observer(
        lambda _install: observer_owned_publication_pin.append(
            feed._admission_condition._is_owned()
        )
    )

    feed.activity_with_cursor("run-1")

    assert observer_owned_publication_pin == [False]


@pytest.mark.parametrize("durable", [False, True])
def test_moved_run_cannot_overtake_its_previous_owner_observer(
    tmp_path: Path,
    durable: bool,
) -> None:
    other_hive = "github/beadhive/other"
    provider = MutableProvider()
    sources = _sources(tmp_path, provider)
    sources.cfg["managed_repos"].append(
        {
            "provider": "github",
            "org": "beadhive",
            "repo": "other",
            "prefix": "other",
            "kind": "org-native",
        }
    )
    feed = operator_feed.OperatorFeed(sources)
    if durable:
        feed.configure_durable_activity_reader(
            lambda _run_id, _journal, _offset: ((), "durable:empty", True, 0)
        )
    hive_journal = _journal(tmp_path, [_record("opaque:first", 1)])
    (hive_journal.parent / "run-hive-sentinel.jsonl").write_text(
        json.dumps(_record("opaque:hive-sentinel", 1, run_id="run-hive-sentinel")) + "\n"
    )
    other_journal = run_journal.journal_path_for_hive(other_hive, "run-1", base=tmp_path)
    other_journal.parent.mkdir(parents=True)
    sentinel_record = {
        **_record("opaque:sentinel", 1, run_id="run-other"),
        "hive": other_hive,
    }
    (other_journal.parent / "run-other.jsonl").write_text(json.dumps(sentinel_record) + "\n")
    observer_started = threading.Event()
    release_observer = threading.Event()
    other_completed = threading.Event()
    observer_order: list[str] = []

    def observe(install: operator_feed.ActivityInstall) -> None:
        if install.hive_id == HIVE:
            observer_order.append("H-start")
            observer_started.set()
            assert release_observer.wait(2)
            observer_order.append("H-end")
            return
        observer_order.append("OTHER-complete")
        other_completed.set()

    feed.register_activity_observer(observe)
    responses: list[dict[str, object]] = []
    failures: list[BaseException] = []

    def read_activity() -> None:
        try:
            responses.append(feed.activity_with_cursor("run-1"))
        except BaseException as exc:
            failures.append(exc)

    hive_reader = threading.Thread(target=read_activity)
    hive_reader.start()
    assert observer_started.wait(2), [
        (type(failure).__name__, getattr(failure, "code", None), str(failure))
        for failure in failures
    ]
    hive_journal.unlink()
    other_record = {**_record("opaque:moved", 2), "hive": other_hive}
    other_journal.write_text(json.dumps(other_record) + "\n")

    other_reader = threading.Thread(target=read_activity)
    other_reader.start()
    try:
        overtook_previous_observer = other_completed.wait(0.5)
    finally:
        release_observer.set()
        hive_reader.join(2)
        other_reader.join(2)

    assert not overtook_previous_observer
    assert not hive_reader.is_alive()
    assert not other_reader.is_alive()
    assert not failures
    assert [response["hiveId"] for response in responses] == [HIVE, other_hive]
    assert observer_order == ["H-start", "H-end", "OTHER-complete"]


@pytest.mark.parametrize("durable", [False, True])
def test_activity_observer_failure_releases_install_ownership(
    tmp_path: Path,
    durable: bool,
) -> None:
    feed = operator_feed.OperatorFeed(_sources(tmp_path, MutableProvider()))
    if durable:
        feed.configure_durable_activity_reader(
            lambda _run_id, _journal, _offset: ((), "durable:empty", True, 0)
        )
    _journal(tmp_path, [_record("opaque:first", 1)])

    def fail_observer(_install: operator_feed.ActivityInstall) -> None:
        raise RuntimeError("observer unavailable")

    remove_observer = feed.register_activity_observer(fail_observer)
    with pytest.raises(RuntimeError, match="observer unavailable"):
        feed.activity_with_cursor("run-1")
    remove_observer()

    assert feed.hive_admissions == 0
    assert not feed._admission_condition._is_owned()
    assert feed.activity_with_cursor("run-1")["hiveId"] == HIVE


def test_activity_rewrite_rotates_epoch_and_expires_old_cursor(tmp_path: Path) -> None:
    provider = MutableProvider()
    feed = operator_feed.OperatorFeed(_sources(tmp_path, provider))
    _journal(tmp_path, [_record("opaque:first", 1)])
    first = feed.activity_with_cursor("run-1")

    _journal(tmp_path, [_record("opaque:replacement", 3)])
    with pytest.raises(operator_sources.OperatorSourceError) as expired:
        feed.activity_with_cursor(
            "run-1", after=(str(first["producerEpoch"]), int(first["sequence"]))
        )
    assert (expired.value.code, expired.value.status_code) == (
        "activity_cursor_expired",
        410,
    )
    replacement = feed.activity_with_cursor("run-1")
    assert replacement["producerEpoch"] != first["producerEpoch"]
    assert replacement["sequence"] == 1


def test_activity_cache_evicts_deterministically_and_expires_the_old_cursor(
    tmp_path: Path,
) -> None:
    provider = MutableProvider()
    feed = operator_feed.OperatorFeed(
        _sources(tmp_path, provider),
        max_cached_activity_runs=64,
        max_cached_activity_bytes=16 * 1_048_576,
    )
    first_cursor: tuple[str, int] | None = None
    for index in range(65):
        run_id = f"run-{index}"
        _journal(
            tmp_path,
            [_record(f"opaque:{index}", index + 1, run_id=run_id)],
            run_id=run_id,
        )
        snapshot = feed.activity_with_cursor(run_id)
        if index == 0:
            first_cursor = (str(snapshot["producerEpoch"]), int(snapshot["sequence"]))

    assert first_cursor is not None
    assert len(feed._activities) == 64
    assert (HIVE, "run-0") not in feed._activities
    assert list(feed._activities) == [(HIVE, f"run-{index}") for index in range(1, 65)]
    assert feed.cached_activity_bytes <= feed.max_cached_activity_bytes
    assert feed.cached_run_ownerships == feed.max_run_ownerships

    with pytest.raises(operator_sources.OperatorSourceError) as expired:
        feed.activity_with_cursor("run-0", after=first_cursor)
    assert (expired.value.code, expired.value.status_code) == (
        "activity_cursor_expired",
        410,
    )
    replacement = feed.activity_with_cursor("run-0")
    assert replacement["kind"] == "snapshot"
    assert replacement["producerEpoch"] != first_cursor[0]


def test_activity_cache_byte_budget_and_concurrent_many_run_reads_stay_bounded(
    tmp_path: Path,
) -> None:
    provider = MutableProvider()
    feed = operator_feed.OperatorFeed(
        _sources(tmp_path, provider),
        max_cached_activity_runs=64,
        max_cached_activity_bytes=4_096,
    )
    run_ids = [f"run-{index}" for index in range(80)]
    for index, run_id in enumerate(run_ids):
        _journal(
            tmp_path,
            [_record(f"opaque:{index}", index + 1, run_id=run_id)],
            run_id=run_id,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = tuple(pool.map(feed.activity_with_cursor, run_ids))

    assert len(results) == 80
    assert len(feed._activities) <= 64
    assert feed.cached_activity_bytes <= 4_096
    assert feed.cached_run_ownerships <= feed.max_run_ownerships
    assert all(state.retained_bytes > 0 for state in feed._activities.values())


def test_full_pinned_cache_coordinates_one_exact_run_cursor_domain(tmp_path: Path) -> None:
    provider = MutableProvider()
    sources = _sources(tmp_path, provider)
    feed = operator_feed.OperatorFeed(
        sources,
        max_cached_activity_runs=1,
        max_cached_activity_bytes=16 * 1_048_576,
    )
    _journal(tmp_path, [_record("opaque:first", 1)])
    entered = threading.Event()
    release = threading.Event()
    original_read = sources.read_run

    def blocked_read(hive, source, run_id):
        entered.set()
        assert release.wait(2)
        return original_read(hive, source, run_id)

    sources.read_run = blocked_read  # type: ignore[method-assign]
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(feed.activity_with_cursor, "run-1")
        assert entered.wait(2)
        second = pool.submit(feed.activity_with_cursor, "run-1")
        deadline = time.monotonic() + 2
        while feed._activities[(HIVE, "run-1")].users != 2 and time.monotonic() < deadline:
            time.sleep(0.001)
        assert feed._activities[(HIVE, "run-1")].users == 2
        release.set()
        results = (first.result(timeout=2), second.result(timeout=2))

    assert results[0]["producerEpoch"] == results[1]["producerEpoch"]
    assert len(feed._activities) == 1
    assert feed._activities[(HIVE, "run-1")].users == 0


def test_full_pinned_cache_rejects_a_different_run_before_minting_a_cursor(
    tmp_path: Path,
) -> None:
    provider = MutableProvider()
    sources = _sources(tmp_path, provider)
    feed = operator_feed.OperatorFeed(
        sources,
        max_cached_activity_runs=1,
        max_cached_activity_bytes=16 * 1_048_576,
    )
    _journal(tmp_path, [_record("opaque:first", 1)])
    _journal(
        tmp_path,
        [_record("opaque:second", 2, run_id="run-2")],
        run_id="run-2",
    )
    entered = threading.Event()
    release = threading.Event()
    observed_reads: list[str] = []
    original_read = sources.read_run

    def blocked_read(hive, source, run_id):
        observed_reads.append(run_id)
        if run_id == "run-1":
            entered.set()
            assert release.wait(2)
        return original_read(hive, source, run_id)

    sources.read_run = blocked_read  # type: ignore[method-assign]
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(feed.activity_with_cursor, "run-1")
        assert entered.wait(2)
        with pytest.raises(operator_sources.OperatorSourceError) as saturated:
            feed.activity_with_cursor("run-2")
        assert (saturated.value.code, saturated.value.status_code) == (
            "activity_cache_saturated",
            503,
        )
        assert saturated.value.retryable is True
        assert observed_reads == ["run-1"]
        assert list(feed._activities) == [(HIVE, "run-1")]
        release.set()
        first.result(timeout=2)

    replacement = feed.activity_with_cursor("run-2")
    assert replacement["kind"] == "snapshot"
    assert list(feed._activities) == [(HIVE, "run-2")]


def test_activity_cache_releases_its_pin_when_a_source_read_is_cancelled(
    tmp_path: Path,
) -> None:
    provider = MutableProvider()
    sources = _sources(tmp_path, provider)
    feed = operator_feed.OperatorFeed(
        sources,
        max_cached_activity_runs=1,
        max_cached_activity_bytes=16 * 1_048_576,
    )
    _journal(tmp_path, [_record("opaque:first", 1)])
    _journal(
        tmp_path,
        [_record("opaque:second", 2, run_id="run-2")],
        run_id="run-2",
    )
    original_read = sources.read_run

    def cancelled_read(_hive, _source, _run_id):
        raise operator_sources.OperatorSourceError(
            "activity_read_cancelled",
            "The activity read was cancelled.",
            status_code=503,
            retryable=True,
        )

    sources.read_run = cancelled_read  # type: ignore[method-assign]
    with pytest.raises(operator_sources.OperatorSourceError, match="cancelled"):
        feed.activity_with_cursor("run-1")
    assert feed._activities[(HIVE, "run-1")].users == 0

    sources.read_run = original_read  # type: ignore[method-assign]
    response = feed.activity_with_cursor("run-2")
    assert response["kind"] == "snapshot"
    assert list(feed._activities) == [(HIVE, "run-2")]
