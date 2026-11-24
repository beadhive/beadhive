"""The CLI claim route refuses a bd older than the atomic ``--claim`` (bh-rwjxt)."""

from __future__ import annotations

import pytest

from beadhive import bd_floor


@pytest.mark.parametrize(
    "line",
    ["bd version 1.3.0 (abc123)", "bd version 1.3.4", "bd version 1.10.0", "bd version 2.0.0"],
)
def test_supported_release_passes(line):
    bd_floor.require_atomic_claim(line)


@pytest.mark.parametrize(
    "line", ["bd version 1.2.2", "bd version 1.1.0 (dev)", "bd version 0.63.3"]
)
def test_old_release_is_refused_with_the_floor_in_the_message(line):
    with pytest.raises(bd_floor.BdTooOldError, match=r"too old.*1\.3\.0"):
        bd_floor.require_atomic_claim(line)


@pytest.mark.parametrize("line", ["", None, "bd version HEAD-af076b6", "garbage"])
def test_unjudgeable_version_is_allowed(line, monkeypatch):
    from beadhive import dolt_health

    monkeypatch.setattr(dolt_health, "_local_bd_version_string", lambda: line)
    bd_floor.require_atomic_claim(line)
    bd_floor.require_atomic_claim()


def test_guarded_lease_refuses_an_old_bd_before_the_claim_write(monkeypatch, tmp_path):
    from beadhive import frame_eligibility, work_lifecycle

    class Leases:
        called = False

        def acquire(self, bead, *, actor):
            Leases.called = True

    monkeypatch.setattr(frame_eligibility, "require_intake", lambda **k: None)
    from beadhive import dolt_health

    monkeypatch.setattr(dolt_health, "_local_bd_version_string", lambda: "bd version 1.2.0")
    with pytest.raises(bd_floor.BdTooOldError):
        work_lifecycle.GuardedFrameLease(Leases(), tmp_path).acquire("bh-1", actor="dev/a")
    assert Leases.called is False
