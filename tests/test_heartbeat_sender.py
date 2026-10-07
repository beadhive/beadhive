"""bh-i6ggn: the conformance cache and the installable in-tree heartbeat sender units."""

from __future__ import annotations

import json
import plistlib
import re

import pytest

from beadhive import heartbeat_conformance as hc
from beadhive import heartbeat_sender as sender

ENV = {"BH_HOME": "/home/frame/.beadhive", "PATH": "/opt/bh/bin:/usr/bin"}
PY = "/opt/bh/venv/bin/python"


@pytest.fixture
def cache(monkeypatch, tmp_path):
    path = tmp_path / "heartbeat" / "conformance.json"
    from beadhive import heartbeat_report

    monkeypatch.setattr(heartbeat_report, "conformance_cache_path", lambda: path)
    return path


def _passing():
    return [{"id": name, "status": "pass"} for name in hc.CACHED_CHECK_IDS]


def test_refresh_writes_measured_at_and_reads_back(cache):
    cached = hc.refresh(_passing, path=cache, clock=iter([100.0, 330.0]).__next__)
    assert cached == hc.read(cache)
    assert cached.measured_at == 100.0 and cached.duration_seconds == 230.0
    document = json.loads(cache.read_text())
    assert document["format"] == hc.CACHE_FORMAT
    assert document["measured_at"] == hc.stamp(100.0)
    assert not list(cache.parent.glob(".conformance-*")), "atomic write leaves no temp file"


def test_refresh_forces_unknown_or_missing_checks_to_fail(cache):
    cached = hc.refresh(lambda: [{"id": "hives-ready", "status": "pass"}], path=cache)
    assert dict(cached.checks) == {"host-config-partition": "fail", "hives-ready": "pass"}


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        json.dumps({"format": "other"}),
        json.dumps({"format": hc.CACHE_FORMAT, "measured_at": "2026-10-06T00:00:00", "checks": []}),
        json.dumps(
            {
                "format": hc.CACHE_FORMAT,
                "measured_at": "2026-10-06T00:00:00+00:00",
                "checks": [{"id": "hives-ready", "status": "pass"}],
            }
        ),
    ],
    ids=["garbage", "format", "naive-time", "check-set"],
)
def test_malformed_cache_reads_as_absent(cache, content):
    cache.parent.mkdir(parents=True)
    cache.write_text(content)
    assert hc.read(cache) is None


def test_overlapping_conformance_runs_skip_instead_of_stacking(cache):
    inner = []

    def measure():
        inner.append(hc.refresh(lambda: pytest.fail("second run must not measure"), path=cache))
        return _passing()

    assert hc.refresh(measure, path=cache) is not None
    assert inner == [None]


def test_beat_checks_bound_and_future_skew():
    cached = hc.CachedConformance(1000.0, 1.0, tuple((n, "pass") for n in hc.CACHED_CHECK_IDS))
    age = hc.AGE_CHECK_ID
    assert hc.beat_checks(cached, now=1000.0 + hc.CONFORMANCE_MAX_AGE_SECONDS)[age]["status"] == (
        "pass"
    )
    assert hc.beat_checks(cached, now=1001.0 + hc.CONFORMANCE_MAX_AGE_SECONDS)[age]["status"] == (
        "fail"
    )
    skewed = 1000.0 - hc.FUTURE_SKEW_SECONDS - 1
    assert hc.beat_checks(cached, now=skewed)[age]["status"] == "fail"


def test_status_reports_age_against_bound(cache):
    hc.refresh(_passing, path=cache, clock=lambda: 5000.0)
    fresh = sender.status(now=5060.0)
    assert fresh["fresh"] and fresh["age_seconds"] == 60.0
    assert fresh["bound_seconds"] == hc.CONFORMANCE_MAX_AGE_SECONDS
    assert not sender.status(now=5001.0 + hc.CONFORMANCE_MAX_AGE_SECONDS)["fresh"]


# --- units: one TTL source, start/stop/verify documented ----------------------------------


def _ini(content: bytes) -> dict[str, str]:
    return dict(re.findall(r"^(\w+)=(.*)$", content.decode(), re.M))


def test_systemd_units_derive_timing_from_the_in_tree_constants():
    units = {unit.name: unit.content for unit in sender.systemd_units(PY, ENV)}
    assert set(units) == {
        "beadhive-heartbeat.service",
        "beadhive-heartbeat.timer",
        "beadhive-heartbeat-conformance.service",
        "beadhive-heartbeat-conformance.timer",
        "beadhive-session-renew.service",
        "beadhive-session-renew.timer",
    }
    beat, beat_timer = (
        _ini(units["beadhive-heartbeat.service"]),
        _ini(units["beadhive-heartbeat.timer"]),
    )
    conf = _ini(units["beadhive-heartbeat-conformance.service"])
    conf_timer = _ini(units["beadhive-heartbeat-conformance.timer"])
    assert int(beat_timer["OnUnitActiveSec"]) == hc.INTERVAL_SECONDS
    assert int(conf_timer["OnUnitActiveSec"]) == hc.CONFORMANCE_INTERVAL_SECONDS
    # A beat must finish (or be killed) well before the lease it renews can lapse.
    assert int(beat["TimeoutStartSec"]) < hc.LEASE_DURATION_SECONDS
    assert int(conf["TimeoutStartSec"]) == hc.CONFORMANCE_MAX_AGE_SECONDS
    assert beat["ExecStart"] == f'"{PY}" "-m" "beadhive.heartbeat_sender" "beat"'
    # The session renewal (bh-owqdg) has its own timer, independent of conformance.
    renew = _ini(units["beadhive-session-renew.service"])
    renew_timer = _ini(units["beadhive-session-renew.timer"])
    assert int(renew_timer["OnUnitActiveSec"]) == hc.RENEW_INTERVAL_SECONDS
    assert int(renew["TimeoutStartSec"]) <= hc.RENEW_INTERVAL_SECONDS
    assert renew["ExecStart"] == f'"{PY}" "-m" "beadhive.heartbeat_sender" "renew"'
    assert conf["ExecStart"].endswith('"conformance"')
    assert (
        'Environment="BH_HOME=/home/frame/.beadhive"'
        in units["beadhive-heartbeat.service"].decode()
    )


def test_launchd_units_derive_timing_from_the_in_tree_constants():
    units = {unit.name: plistlib.loads(unit.content) for unit in sender.launchd_units(PY, ENV)}
    beat = units["dev.beadhive.heartbeat.plist"]
    conf = units["dev.beadhive.heartbeat-conformance.plist"]
    assert beat["StartInterval"] == hc.INTERVAL_SECONDS
    assert conf["StartInterval"] == hc.CONFORMANCE_INTERVAL_SECONDS
    assert beat["ProgramArguments"] == [PY, "-m", "beadhive.heartbeat_sender", "beat"]
    assert conf["EnvironmentVariables"] == ENV
    renew = units["dev.beadhive.session-renew.plist"]
    assert renew["StartInterval"] == hc.RENEW_INTERVAL_SECONDS
    assert renew["ProgramArguments"] == [PY, "-m", "beadhive.heartbeat_sender", "renew"]


def test_ttl_has_one_in_tree_source():
    """No sender module restates the TTL: only heartbeat_conformance names the number."""
    from pathlib import Path

    import beadhive

    root = Path(beadhive.__file__).parent
    for name in ("heartbeat_report.py", "heartbeat_sender.py"):
        text = (root / name).read_text()
        assert 'leaseDurationSeconds": 300' not in text
        assert not re.search(r"\b900\b", text), name
    assert hc.CONFORMANCE_MAX_AGE_SECONDS == hc.LEASE_DURATION_SECONDS


def test_units_install_and_remove(tmp_path, monkeypatch):
    monkeypatch.setattr(sender, "_environment", lambda: ENV)
    for platform, count in (("systemd", 6), ("launchd", 3)):
        directory = tmp_path / platform
        written = sender.install(platform, directory)
        assert len(written) == count and all(path.is_file() for path in written)
        assert sender.remove(platform, directory) == written
        assert not any(directory.iterdir())


def test_module_entrypoint_prints_units_and_status(cache, monkeypatch, capsys):
    monkeypatch.setattr(sender, "_environment", lambda: ENV)
    assert sender.main(["units", "--platform", "systemd"]) == 0
    assert "beadhive-heartbeat.timer" in capsys.readouterr().out
    assert sender.main(["status"]) == 1  # no cache yet: not fresh
    hc.refresh(_passing, path=cache)
    assert sender.main(["status"]) == 0


def test_beat_refusal_is_redacted(monkeypatch, capsys):
    def boom(_free):
        raise RuntimeError("password=SECRET")

    monkeypatch.setattr(sender, "_beat", boom)
    assert sender.main(["beat"]) == 1
    captured = capsys.readouterr()
    assert "SECRET" not in captured.out + captured.err


def test_docstring_documents_start_stop_and_verify():
    doc = sender.__doc__
    for needle in ("enable --now", "disable --now", "list-timers", "bootstrap", "bootout"):
        assert needle in doc
    assert "status" in doc
