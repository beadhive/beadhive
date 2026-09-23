from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "benchmark_xdist.py"
SPEC = importlib.util.spec_from_file_location("benchmark_xdist", SCRIPT)
assert SPEC and SPEC.loader
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


def test_parse_pytest_output_captures_counts_failures_and_slow_phases():
    output = """
============================= slowest 25 durations =============================
2.50s call     tests/test_something.py::test_slow
0.25s setup    tests/test_something.py::test_slow
================== 1 failed, 7 passed, 2 skipped in 4.75s ==================
"""
    assert benchmark.parse_pytest_output(output, 1) == {
        "passed": 7,
        "skipped": 2,
        "failed": 1,
        "errors": 0,
        "pytest_elapsed_seconds": 4.75,
        "slow_phases": [
            {"seconds": 2.5, "phase": "call", "test": "tests/test_something.py::test_slow"},
            {"seconds": 0.25, "phase": "setup", "test": "tests/test_something.py::test_slow"},
        ],
    }


def test_parse_pytest_output_records_unclassified_nonzero_as_error():
    assert benchmark.parse_pytest_output("===== 3 passed in 1.20s =====", 2)["errors"] == 1


def test_aggregate_runs_uses_true_median_and_counts_successes():
    runs = [
        {
            "selection": "unit",
            "workers": 16,
            "exit_code": exit_code,
            "pytest_elapsed_seconds": pytest_time,
            "external_wall_seconds": wall_time,
        }
        for exit_code, pytest_time, wall_time in [
            (0, 9.0, 10.0),
            (1, 1.0, 2.0),
            (0, 5.0, 6.0),
            (0, 3.0, 4.0),
        ]
    ]
    assert benchmark.aggregate_runs(runs) == [
        {
            "selection": "unit",
            "workers": 16,
            "repetitions": 4,
            "successful_repetitions": 3,
            "median_pytest_elapsed_seconds": 4.0,
            "median_external_wall_seconds": 5.0,
        }
    ]


def test_cache_locality_separates_capability_from_unobserved_use(monkeypatch, tmp_path):
    cache = tmp_path / "cache"
    target = tmp_path / "environment"
    cache.mkdir()
    target.mkdir()
    monkeypatch.setattr(benchmark, "command_output", lambda command: str(cache))
    monkeypatch.setattr(benchmark.sys, "prefix", str(target))
    real_stat = benchmark.Path.stat

    def fake_stat(path, *, follow_symlinks=True):
        values = list(real_stat(path, follow_symlinks=follow_symlinks))
        values[2] = 101 if Path(path) == cache else 202
        return os.stat_result(values)

    monkeypatch.setattr(benchmark.Path, "stat", fake_stat)
    monkeypatch.setattr(
        benchmark,
        "observe_uv_materialization",
        lambda scratch, cache: {
            "observed": False,
            "link_mode": "unknown",
            "hardlinks_used": "unknown",
            "fallback_copies_used": "unknown",
        },
    )
    monkeypatch.setattr(
        benchmark.os,
        "statvfs",
        lambda path: type("Capacity", (), {"f_bavail": 10, "f_frsize": 4096, "f_favail": 20})(),
    )
    locality = benchmark.cache_locality(tmp_path)
    assert locality["expected_auto_mode"] == "copy-fallback"
    assert locality["hardlinks_supported_by_device_layout"] is False
    assert locality["materialization_observation"] == {
        "observed": False,
        "link_mode": "unknown",
        "hardlinks_used": "unknown",
        "fallback_copies_used": "unknown",
    }
    assert locality["cache"]["device_id"] == 101
    assert locality["target"]["device_id"] == 202
    assert locality["cache"]["available_bytes"] == 40960
    assert locality["cache"]["available_inodes"] == 20


def test_materialization_parser_records_observed_copy_fallback():
    output = """
DEBUG Failed to hard link `/home/bees/.cache/uv/archive-v0/abc/pkg.py` to
`/tmp/probe/target/pkg.py`: Invalid cross-device link; falling back to copy
warning: Failed to hardlink files; falling back to full copy.
"""
    assert benchmark.parse_materialization_observation(output, 0, Path("/home/bees/.cache/uv")) == {
        "observed": True,
        "link_mode": "copy-fallback",
        "hardlinks_used": False,
        "fallback_copies_used": True,
        "evidence": "uv cache archive -> isolated target: hardlink failed; copied",
    }


def test_materialization_parser_keeps_failed_probe_unknown():
    observed = benchmark.parse_materialization_observation(
        "offline wheel unavailable", 1, Path("/cache-override")
    )
    assert observed["observed"] is False
    assert observed["link_mode"] == "unknown"
    assert observed["hardlinks_used"] == "unknown"
    assert observed["fallback_copies_used"] == "unknown"
    assert observed["diagnostic"] == "offline wheel unavailable"


def test_materialization_parser_does_not_infer_use_from_generic_warning():
    observed = benchmark.parse_materialization_observation(
        "warning: Failed to hardlink files; falling back to full copy.",
        0,
        Path("/cache-override"),
    )
    assert observed["observed"] is False
    assert observed["link_mode"] == "unknown"


def test_materialization_parser_honors_resolved_cache_override():
    output = (
        "DEBUG Failed to hard link `/cache-override/archive-v0/abc/pkg.py` to "
        "`/tmp/target/pkg.py`; falling back to copy"
    )
    observed = benchmark.parse_materialization_observation(output, 0, Path("/cache-override"))
    assert observed["observed"] is True
    assert observed["link_mode"] == "copy-fallback"


def test_human_table_includes_locality_capacity_and_observed_mode():
    aggregates = [
        {
            "selection": "unit",
            "workers": 16,
            "successful_repetitions": 1,
            "repetitions": 1,
            "median_pytest_elapsed_seconds": 2.0,
            "median_external_wall_seconds": 3.0,
        }
    ]
    provenance = {
        "cache_locality": {
            "cache": {"device_id": 1, "available_bytes": 100, "available_inodes": 10},
            "target": {"device_id": 2, "available_bytes": 200, "available_inodes": 20},
            "requested_link_mode": "auto",
            "expected_auto_mode": "copy-fallback",
            "materialization_observation": {
                "link_mode": "copy-fallback",
                "hardlinks_used": False,
                "fallback_copies_used": True,
                "target": {"device_id": 3, "available_bytes": 300, "available_inodes": 30},
            },
        }
    }
    rendered = benchmark.render_table(aggregates, provenance)
    assert "| uv cache | 1 | 100 | 10 |" in rendered
    assert "| interpreter target | 2 | 200 | 20 |" in rendered
    assert "| uv probe target | 3 | 300 | 30 |" in rendered
    assert "observed `copy-fallback`" in rendered


def test_validation_slots_reports_environment_override(monkeypatch):
    monkeypatch.setenv("BH_VALIDATION_SLOTS", "3")
    assert benchmark.validation_slots() == {"effective": 3, "source": "BH_VALIDATION_SLOTS"}


def test_validation_slots_reports_host_config_source(monkeypatch):
    monkeypatch.delenv("BH_VALIDATION_SLOTS", raising=False)
    completed = subprocess.CompletedProcess([], 0, stdout="2\n", stderr="")
    monkeypatch.setattr(benchmark.subprocess, "run", lambda *args, **kwargs: completed)
    assert benchmark.validation_slots() == {"effective": 2, "source": "host-config"}


def test_validation_slots_reports_default_when_host_key_is_absent(monkeypatch):
    monkeypatch.delenv("BH_VALIDATION_SLOTS", raising=False)
    completed = subprocess.CompletedProcess([], 1, stdout="", stderr="missing")
    monkeypatch.setattr(benchmark.subprocess, "run", lambda *args, **kwargs: completed)
    assert benchmark.validation_slots() == {"effective": 1, "source": "default"}


def test_repo_nested_scratch_is_refused_before_pytest(monkeypatch, tmp_path):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="/checkout\n", stderr="")

    monkeypatch.setattr(benchmark.subprocess, "run", fake_run)
    with pytest.raises(benchmark.BenchmarkError, match="unsafe scratch root"):
        benchmark.ensure_external_scratch(tmp_path)
    assert calls == [["git", "-C", str(tmp_path.resolve()), "rev-parse", "--show-toplevel"]]
