"""The ONE `bh host dispatch status` computation (bh-e7r9q.5), reused by `bh doctor`'s dispatch
section (bh-e7r9q.6). This is where THE DISTINCTION bh-e7r9q.6 exists to draw is actually
computed — `bh host list` prints one word (`stale`) for both a dead loop and a lapsed lease;
these tests assert the four states never collapse into each other.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

from beadhive import dispatch_status as dstat
from beadhive import dispatch_supervisor as ds
from beadhive import guard, registry

_ENTRY = {"provider": "github", "org": "acme", "repo": "widgets", "prefix": "acme"}


@dataclass(frozen=True)
class _FakeLease:
    host_id: str
    expires_at: str = "2099-01-01T00:00:00Z"
    is_tombstone: bool = False

    def held_by(self, host_id, at=None):  # noqa: ARG002
        return self.host_id == host_id

    def describe(self):
        return f"{self.host_id} until {self.expires_at}"


class _FixedBackend:
    name = "fixed"

    def __init__(self, state: ds.SupervisorState):
        self._state = state

    def enable(self, *a, **k):  # pragma: no cover - unused by status tests
        return self._state

    def disable(self, *a, **k):  # pragma: no cover - unused by status tests
        return self._state

    def status(self, hive_slug):  # noqa: ARG002
        return self._state


def _patch_hive(monkeypatch, tmp_path):
    monkeypatch.setattr(registry, "hive_dir_for", lambda cfg, hive: tmp_path)
    monkeypatch.setattr(registry, "entry_for_dir", lambda cfg, cwd: _ENTRY)


def test_not_enabled_when_backend_never_installed(monkeypatch, tmp_path):
    _patch_hive(monkeypatch, tmp_path)
    monkeypatch.setattr(guard, "primary_state", lambda *a, **k: None)
    backend = _FixedBackend(ds.SupervisorState(installed=False, running=False, persisted=False))

    status = dstat.compute_status("acme/widgets", cfg={}, backend=backend)

    assert status.state == dstat.STATE_NOT_ENABLED


def test_dead_loop_is_enabled_stopped_not_lease_lost(monkeypatch, tmp_path):
    """THE core distinction: installed + persisted but not running, with the lease still fine
    -- must classify as the DEAD-LOOP state, never as a lease problem."""
    _patch_hive(monkeypatch, tmp_path)
    monkeypatch.setattr(guard, "primary_state", lambda *a, **k: None)  # no lease in force
    backend = _FixedBackend(ds.SupervisorState(installed=True, running=False, persisted=True))

    status = dstat.compute_status("acme/widgets", cfg={}, backend=backend)

    assert status.state == dstat.STATE_ENABLED_STOPPED
    assert status.state != dstat.STATE_RUNNING_WITHOUT_LEASE


def test_running_without_lease_is_distinct_from_dead(monkeypatch, tmp_path):
    """The other half of the same distinction: RUNNING but the lease is held by someone else —
    must classify as the lease state, never as dead."""
    _patch_hive(monkeypatch, tmp_path)
    lease = _FakeLease(host_id="some-other-host")
    monkeypatch.setattr(guard, "primary_state", lambda *a, **k: ("acme", "this-host", lease))
    backend = _FixedBackend(ds.SupervisorState(installed=True, running=True, persisted=True))

    status = dstat.compute_status("acme/widgets", cfg={}, backend=backend)

    assert status.state == dstat.STATE_RUNNING_WITHOUT_LEASE
    assert status.lease_held is False
    assert status.state != dstat.STATE_ENABLED_STOPPED


def test_running_healthy_when_running_and_leased(monkeypatch, tmp_path):
    _patch_hive(monkeypatch, tmp_path)
    lease = _FakeLease(host_id="this-host")
    monkeypatch.setattr(guard, "primary_state", lambda *a, **k: ("acme", "this-host", lease))
    backend = _FixedBackend(ds.SupervisorState(installed=True, running=True, persisted=True))

    status = dstat.compute_status("acme/widgets", cfg={}, backend=backend)

    assert status.state == dstat.STATE_RUNNING_HEALTHY


def test_single_host_default_treated_as_leased(monkeypatch, tmp_path):
    """No lease in force at all (never adopted / no HQ clone) is the single-host default —
    running should read as healthy, not as a lease problem."""
    _patch_hive(monkeypatch, tmp_path)
    monkeypatch.setattr(guard, "primary_state", lambda *a, **k: None)
    backend = _FixedBackend(ds.SupervisorState(installed=True, running=True, persisted=True))

    status = dstat.compute_status("acme/widgets", cfg={}, backend=backend)

    assert status.lease_in_force is False
    assert status.state == dstat.STATE_RUNNING_HEALTHY


def test_last_pass_and_seats_in_flight_read_from_the_aggregate_sink(monkeypatch, tmp_path):
    _patch_hive(monkeypatch, tmp_path)
    monkeypatch.setattr(guard, "primary_state", lambda *a, **k: None)
    backend = _FixedBackend(ds.SupervisorState(installed=True, running=True, persisted=True))

    slug = "github-acme-widgets"
    sink_dir = tmp_path / "sink"
    sink_dir.mkdir()
    sink_path = sink_dir / f"{slug}.jsonl"
    monkeypatch.setattr(dstat.dispatch_log, "sink_path_for_slug", lambda s: sink_path)
    monkeypatch.setattr(dstat.dispatch_log, "hive_slug", lambda entry: slug)
    records = [
        {"event": "hive_dispatch_pass", "timestamp": "2026-01-01T00:00:00Z", "in_flight": ["bh-1"]},
        {"event": "dispatch_cause_recorded", "cause": "escalated", "reason": "stuck"},
    ]
    sink_path.write_text("\n".join(json.dumps(r) for r in records) + "\n")

    status = dstat.compute_status("acme/widgets", cfg={}, backend=backend)

    assert status.last_pass_at == "2026-01-01T00:00:00Z"
    assert status.seats_in_flight == 1
    assert status.last_escalation["reason"] == "stuck"


def test_lease_expiring_soon_flags_close_to_ttl(monkeypatch, tmp_path):
    _patch_hive(monkeypatch, tmp_path)
    soon = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 30))
    lease = _FakeLease(host_id="this-host", expires_at=soon)
    monkeypatch.setattr(guard, "primary_state", lambda *a, **k: ("acme", "this-host", lease))
    backend = _FixedBackend(ds.SupervisorState(installed=True, running=True, persisted=True))

    status = dstat.compute_status("acme/widgets", cfg={}, backend=backend)

    assert status.lease_expiring_soon is True


def test_lease_far_from_expiry_is_not_flagged(monkeypatch, tmp_path):
    _patch_hive(monkeypatch, tmp_path)
    far = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 100000))
    lease = _FakeLease(host_id="this-host", expires_at=far)
    monkeypatch.setattr(guard, "primary_state", lambda *a, **k: ("acme", "this-host", lease))
    backend = _FixedBackend(ds.SupervisorState(installed=True, running=True, persisted=True))

    status = dstat.compute_status("acme/widgets", cfg={}, backend=backend)

    assert status.lease_expiring_soon is False
