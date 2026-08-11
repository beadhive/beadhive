"""`bh doctor`'s unattended-dispatch section (bh-e7r9q.6).

"Prove the check FIRES on a broken fixture, not merely that it passes on a clean one" is this
bead's own acceptance bar (vacuous guards are a repeated failure mode in this factory, bh-gd443).
Every state test below therefore has a paired assertion: the broken fixture trips the flag, and
a nearby healthy fixture does NOT — so a guard that always returns True could not sneak through.

Reads through `dispatch_status.compute_status_all` (patched here) rather than re-deriving
health, per the bead's other acceptance criterion — these tests would not even notice a
doctor-side re-derivation bug, which is the point: there is nothing here TO re-derive.
"""

from __future__ import annotations

from beadhive import config, dispatch_status, doctor


def _status(
    hive="acme/widgets",
    *,
    installed=True,
    running=True,
    state=dispatch_status.STATE_RUNNING_HEALTHY,
    lease_in_force=True,
    lease_held=True,
    lease_expires_at="2099-01-01T00:00:00Z",
    last_pass_at="",
):
    return dispatch_status.DispatchStatus(
        hive=hive,
        hive_slug="acme-widgets",
        backend="systemd",
        installed=installed,
        running=running,
        persisted=True,
        lease_in_force=lease_in_force,
        lease_held=lease_held,
        lease_expires_at=lease_expires_at,
        lease_detail="",
        last_pass_at=last_pass_at,
        seats_in_flight=0,
        last_escalation=None,
        state=state,
        detail="",
    )


def test_silent_when_no_dispatch_ever_enabled(monkeypatch):
    monkeypatch.setattr(
        dispatch_status, "compute_status_all", lambda cfg: [_status(installed=False)]
    )
    data = doctor._data_dispatch({})
    assert data["relevant"] is False
    assert data["hives"] == []


def test_dead_loop_check_fires_on_a_supervised_but_stopped_fixture(monkeypatch):
    broken = _status(running=False, state=dispatch_status.STATE_ENABLED_STOPPED)
    healthy = _status(hive="ok/hive")
    monkeypatch.setattr(dispatch_status, "compute_status_all", lambda cfg: [broken, healthy])

    data = doctor._data_dispatch({})

    rows = {r["hive"]: r for r in data["hives"]}
    assert rows["acme/widgets"]["dead"] is True
    assert rows["ok/hive"]["dead"] is False  # the healthy fixture must NOT also trip


def test_lease_lost_check_fires_on_a_running_but_unleased_fixture(monkeypatch):
    broken = _status(
        state=dispatch_status.STATE_RUNNING_WITHOUT_LEASE, lease_held=False, lease_in_force=True
    )
    healthy = _status(hive="ok/hive")
    monkeypatch.setattr(dispatch_status, "compute_status_all", lambda cfg: [broken, healthy])

    data = doctor._data_dispatch({})

    rows = {r["hive"]: r for r in data["hives"]}
    assert rows["acme/widgets"]["lease_lost"] is True
    assert rows["acme/widgets"]["dead"] is False  # must not ALSO read as dead
    assert rows["ok/hive"]["lease_lost"] is False


def test_dead_and_lease_lost_are_mutually_exclusive_the_bug_this_bead_prevents(monkeypatch):
    """`bh host list` prints one word (`stale`) for both; this section must not repeat that."""
    dead = _status(running=False, state=dispatch_status.STATE_ENABLED_STOPPED)
    lease_lost = _status(
        hive="lease-lost/hive",
        state=dispatch_status.STATE_RUNNING_WITHOUT_LEASE,
        lease_held=False,
    )
    monkeypatch.setattr(dispatch_status, "compute_status_all", lambda cfg: [dead, lease_lost])

    data = doctor._data_dispatch({})
    rows = {r["hive"]: r for r in data["hives"]}

    assert rows["acme/widgets"]["dead"] and not rows["acme/widgets"]["lease_lost"]
    assert rows["lease-lost/hive"]["lease_lost"] and not rows["lease-lost/hive"]["dead"]


def test_lease_expiring_soon_check_fires_close_to_ttl(monkeypatch):
    import time

    ttl = config.host_lease_ttl(None)
    soon = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + ttl * 0.01))
    far = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + ttl * 10))
    broken = _status(lease_expires_at=soon)
    healthy = _status(hive="ok/hive", lease_expires_at=far)
    monkeypatch.setattr(dispatch_status, "compute_status_all", lambda cfg: [broken, healthy])

    data = doctor._data_dispatch({})

    rows = {r["hive"]: r for r in data["hives"]}
    assert rows["acme/widgets"]["lease_expiring_soon"] is True
    assert rows["ok/hive"]["lease_expiring_soon"] is False


def test_stalled_check_fires_when_no_pass_within_the_configured_window(monkeypatch):
    import time

    stale_after = config.dispatch_stale_after_seconds({})
    long_ago = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - stale_after * 3))
    recent = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 5))
    broken = _status(last_pass_at=long_ago)
    healthy = _status(hive="ok/hive", last_pass_at=recent)
    monkeypatch.setattr(dispatch_status, "compute_status_all", lambda cfg: [broken, healthy])

    data = doctor._data_dispatch({})

    rows = {r["hive"]: r for r in data["hives"]}
    assert rows["acme/widgets"]["stalled"] is True
    assert rows["ok/hive"]["stalled"] is False


def test_render_dispatch_is_silent_when_not_relevant(capsys):
    doctor._render_dispatch({"relevant": False, "hives": []})
    assert capsys.readouterr().out == ""


def test_render_dispatch_prints_a_line_per_flagged_hive(capsys):
    doctor._render_dispatch(
        {
            "relevant": True,
            "hives": [
                {
                    "hive": "acme/widgets",
                    "dead": True,
                    "lease_lost": False,
                    "lease_expiring_soon": False,
                    "stalled": False,
                    "stale_seconds": None,
                    "last_pass_at": "",
                    "detail": "",
                }
            ],
        }
    )
    out = capsys.readouterr().out
    assert "acme/widgets" in out
    assert "DEAD" in out
