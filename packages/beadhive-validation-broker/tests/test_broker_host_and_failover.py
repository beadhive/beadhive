"""The stdlib host probes and the loud failover wrapper (validation-broker-adr, bh-qrixp).

The probes read files a test writes (a fake ``/proc/meminfo`` and ``/proc/<pid>/stat``); they
never read the real host's memory. The failover tests use the in-memory fake as both engines.
"""

from __future__ import annotations

import os

import pytest

from beadhive_validation_broker import (
    BrokerPort,
    BrokerUnavailable,
    Capacity,
    FailoverBroker,
    GrantRefused,
    GrantTimeout,
    InMemoryBroker,
    LeaseLost,
    MeminfoAvailability,
    Outcome,
    Owner,
    Usage,
    current_owner,
    pid_alive,
    statvfs_free,
)
from beadhive_validation_broker.contract import ManualClock, granted, job

GIB = 1024**3
OK, USED = Outcome(0), Usage(wall_s=1.0)

MEMINFO = """MemTotal:       49283072 kB
MemFree:         1234567 kB
MemAvailable:   10485760 kB
HugePages_Total:       0
"""


# ---- availability -------------------------------------------------------------------------


def test_meminfo_availability_reads_mem_available_and_the_cpu_count(tmp_path) -> None:
    path = tmp_path / "meminfo"
    path.write_text(MEMINFO, encoding="ascii")
    probe = MeminfoAvailability(path, cpu_count=lambda: 12)
    assert probe() == Capacity(12.0, 10 * GIB)
    assert probe.total_memory_bytes() == 49283072 * 1024
    assert probe.read()["HugePages_Total"] == 0


def test_meminfo_availability_raises_without_the_file_or_the_line(tmp_path) -> None:
    with pytest.raises(OSError):
        MeminfoAvailability(tmp_path / "missing", cpu_count=lambda: 1)()
    partial = tmp_path / "meminfo"
    partial.write_text("MemTotal: 1 kB\n", encoding="ascii")
    with pytest.raises(OSError):
        MeminfoAvailability(partial, cpu_count=lambda: 1)()


# ---- liveness -----------------------------------------------------------------------------


def _fake_stat(proc, pid: int, start: str) -> None:
    fields = ["S"] + ["0"] * 18 + [start, "0"]
    (proc / str(pid)).mkdir(parents=True)
    (proc / str(pid) / "stat").write_text(f"{pid} (a (weird) name) " + " ".join(fields))


def test_pid_alive_compares_the_start_token(tmp_path) -> None:
    me = os.getpid()
    _fake_stat(tmp_path, me, "424242")
    assert pid_alive(Owner(me, "424242"), proc=tmp_path)
    assert not pid_alive(Owner(me, "111"), proc=tmp_path), "a reused pid is a dead owner"
    assert pid_alive(Owner(me), proc=tmp_path), "no token: the pid alone decides"
    assert pid_alive(Owner(me, "111"), proc=tmp_path / "nowhere"), "unreadable token: alive"


def test_current_owner_is_this_process_with_a_token_pid_alive_accepts(tmp_path) -> None:
    me = os.getpid()
    _fake_stat(tmp_path, me, "777")
    owner = current_owner(proc=tmp_path)
    assert (owner.pid, owner.start_token, owner.host) == (me, "777", os.uname().nodename)
    assert pid_alive(owner, proc=tmp_path)


def test_statvfs_free_reads_the_filesystem_and_raises_for_a_missing_root(tmp_path) -> None:
    assert statvfs_free(str(tmp_path)) > 0
    with pytest.raises(OSError):
        statvfs_free(str(tmp_path / "missing"))


def test_pid_alive_never_reclaims_another_hosts_owner(tmp_path) -> None:
    assert pid_alive(Owner(2**22 + 12345, "x", host="elsewhere"), proc=tmp_path, hostname="here")


# ---- failover -----------------------------------------------------------------------------


class _Broken:
    """A primary that raises ``error`` from the operations named in ``failing``."""

    name = "local"

    def __init__(self, inner: InMemoryBroker, error: Exception, failing: set[str]) -> None:
        self.inner, self.error, self.failing = inner, error, failing

    def __getattr__(self, op: str):
        target = getattr(self.inner, op)

        def call(*args, **kwargs):
            if op in self.failing:
                raise self.error
            return target(*args, **kwargs)

        return call


def _pair(error: Exception, failing: set[str]):
    clock = ManualClock()
    primary_inner = InMemoryBroker(Capacity(4.0, 4 * GIB), clock)
    fallback = InMemoryBroker(Capacity(4.0, 4 * GIB), clock)
    fallback.name = "slots"  # type: ignore[misc]
    warnings: list[str] = []
    broker = FailoverBroker(
        _Broken(primary_inner, error, failing), lambda: fallback, warn=warnings.append
    )
    return broker, primary_inner, fallback, warnings


def test_failover_satisfies_the_port_and_serves_from_the_primary() -> None:
    broker, primary, fallback, warnings = _pair(RuntimeError("boom"), set())
    assert isinstance(broker, BrokerPort)
    handle = broker.submit(job("a"))
    assert broker.wait_grant(handle, timeout=0).job_id == "a"
    broker.report(handle, OK, USED)
    assert broker.name == "local" and not broker.tripped and warnings == []
    assert fallback.status().running == 0 and primary.status().queued == 0


@pytest.mark.parametrize("error", [RuntimeError("engine bug"), BrokerUnavailable("down")])
def test_a_failing_submit_falls_back_loudly_once(error) -> None:
    broker, _, fallback, warnings = _pair(error, {"submit"})
    first = broker.submit(job("a"))
    second = broker.submit(job("b"))
    assert broker.wait_grant(first, timeout=0).job_id == "a"
    assert broker.wait_grant(second, timeout=0).job_id == "b"
    assert broker.tripped and broker.name == "slots"
    assert len(warnings) == 1 and "falling back to the slots engine" in warnings[0]
    broker.report(first, OK, USED)
    broker.report(second, OK, USED)
    assert fallback.status().running == 0


def test_a_failing_wait_moves_a_queued_job_to_the_fallback() -> None:
    broker, primary, fallback, warnings = _pair(BrokerUnavailable("probe"), {"wait_grant"})
    handle = broker.submit(job("a"))
    grant = broker.wait_grant(handle, timeout=0)
    assert grant.job_id == "a" and fallback.status().running == 1
    assert primary.status().running == 0, "the primary's copy was cancelled"
    broker.report(handle, OK, USED)  # routed to the engine that granted it
    assert fallback.status().running == 0
    assert len(warnings) == 1


def test_contract_answers_pass_through_without_tripping() -> None:
    broker, _, _, warnings = _pair(RuntimeError("unused"), set())
    with pytest.raises(GrantRefused):
        broker.submit(job("huge", cpu_min=99))
    hog = broker.submit(job("hog", cpu_min=4))
    waiting = broker.submit(job("waiting", cpu_min=4))
    broker.wait_grant(hog, timeout=0)
    with pytest.raises(GrantTimeout):
        broker.wait_grant(waiting, timeout=0)
    with pytest.raises(LeaseLost):
        broker.submit(job("orphan", parent_lease="lease-missing"))
    assert not broker.tripped and warnings == []


def test_a_nested_request_stays_with_its_parents_engine() -> None:
    broker, primary_inner, _, _ = _pair(RuntimeError("late failure"), set())
    parent = broker.wait_grant(broker.submit(job("parent", cpu_min=2, cpu_max=2)), timeout=0)
    broker._primary.failing.add("submit")  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError):
        broker.submit(job("child", cpu_min=1, parent_lease=parent.lease_id))
    assert not broker.tripped, "a nested request is never re-queued on another engine"
    assert granted(primary_inner, primary_inner.submit(job("x", cpu_min=2))) is not None
