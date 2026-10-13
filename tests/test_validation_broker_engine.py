"""Engine selection: slots by default and unchanged, local opt-in, loud fallback (bh-qrixp).

The local engine reads a ``/proc/meminfo`` the test writes and a fake CPU count, never the real
host. The one slots grant waits on the engine's own admission event, bounded by ``GENEROUS``.
"""

from __future__ import annotations

import pytest

from beadhive.modules.config.application.partition import HOST, partition_of
from beadhive.modules.config.contracts import ValidationBrokerConfig, WorkConfig
from beadhive.validation_broker_engine import (
    ENGINE_ENV,
    broker_settings,
    configured_engine,
    execution_roots,
    select_engine,
)
from beadhive.validation_broker_slots import SlotsEngine
from beadhive_validation_broker import (
    Capacity,
    ExecutionRoot,
    FailoverBroker,
    FakeDiskSpace,
    MeminfoAvailability,
    Outcome,
    RootKind,
    Usage,
)
from beadhive_validation_broker.contract import ManualClock, job

GIB = 1024**3
GENEROUS = 10.0  # an upper bound on an event wait, never a delay the test depends on
OK, USED = Outcome(0), Usage(wall_s=1.0)


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch):
    monkeypatch.delenv(ENGINE_ENV, raising=False)
    monkeypatch.delenv("BH_VALIDATION_SLOTS", raising=False)
    monkeypatch.setenv("BH_VALIDATION_MEMORY_FLOOR", "0")


@pytest.fixture
def meminfo(tmp_path):
    path = tmp_path / "meminfo"
    path.write_text(f"MemTotal: {64 * 1024 * 1024} kB\nMemAvailable: {40 * 1024 * 1024} kB\n")
    return path


def _select(cfg, tmp_path, meminfo, environ=None, warnings=None, **extra):
    return select_engine(
        cfg,
        root=tmp_path / "slots",
        environ=environ or {},
        probe=MeminfoAvailability(meminfo, cpu_count=lambda: 16),
        clock=ManualClock(),
        owner_alive=lambda _owner: True,
        warn=(warnings if warnings is not None else []).append,
        **extra,
    )


# ---- slots: the default, byte-identical -------------------------------------------------


@pytest.mark.parametrize(
    "cfg",
    [{}, {"work": {}}, {"work": {"validation_broker": {"engine": "slots"}}}],
    ids=["no-config", "no-section", "explicit-slots"],
)
def test_slots_is_the_default_and_is_todays_engine(cfg, tmp_path, meminfo) -> None:
    warnings: list[str] = []
    engine = _select(cfg, tmp_path, meminfo, warnings=warnings)
    assert type(engine) is SlotsEngine
    assert (engine._cfg, engine._entry, engine._phase, engine._root) == (
        cfg,
        None,
        "validation",
        tmp_path / "slots",
    )
    assert warnings == []


def test_slots_never_reads_or_validates_the_local_settings(tmp_path, meminfo) -> None:
    cfg = {"work": {"validation_broker": {"engine": "slots", "budget": {"memory": "lots"}}}}
    warnings: list[str] = []
    assert type(_select(cfg, tmp_path, meminfo, warnings=warnings)) is SlotsEngine
    assert warnings == []


def test_the_environment_override_wins_over_config() -> None:
    cfg = {"work": {"validation_broker": {"engine": "local"}}}
    assert configured_engine(cfg, {}) == "local"
    assert configured_engine(cfg, {ENGINE_ENV: "slots"}) == "slots"
    assert configured_engine({}, {ENGINE_ENV: " LOCAL "}) == "local"
    with pytest.raises(ValueError, match=ENGINE_ENV):
        configured_engine({}, {ENGINE_ENV: "hyperqueue"})


# ---- local: opt-in, resolved against the host ---------------------------------------------


def test_local_is_selected_and_resolves_budget_and_floor(tmp_path, meminfo) -> None:
    warnings: list[str] = []
    engine = _select({}, tmp_path, meminfo, environ={ENGINE_ENV: "local"}, warnings=warnings)
    assert isinstance(engine, FailoverBroker) and engine.name == "local"
    status = engine.status()
    assert status.budget == Capacity(16.0, 64 * GIB, {"dolt-servers": 12})
    local = engine._primary
    assert local._floor == Capacity(0.0, int(64 * GIB * 0.085)), "8.5% of RAM beats 4G here"
    assert local.policy.headroom_cpu == 8.0 and local.policy.easy
    assert warnings == []


def test_local_budget_and_packer_knobs_come_from_config(tmp_path, meminfo) -> None:
    cfg = {
        "work": {
            "validation_broker": {
                "engine": "local",
                "budget": {"cpu": 12, "memory": "50%", "pools": {"dolt-servers": 6}},
                "floor": {"memory": "16G", "memory_percent": 1},
                "packer": {"headroom_cpu": 2, "easy": False},
            }
        }
    }
    engine = _select(cfg, tmp_path, meminfo)
    local = engine._primary
    assert local._budget == Capacity(12.0, 32 * GIB, {"dolt-servers": 6})
    assert local._floor.memory_bytes == 16 * GIB
    assert (local.policy.headroom_cpu, local.policy.easy) == (2.0, False)
    handle = engine.submit(job("a", cpu_min=2, memory_bytes=GIB))
    assert engine.wait_grant(handle, timeout=0).amounts.cpu == 2
    engine.report(handle, OK, USED)


# ---- loud fallback ------------------------------------------------------------------------


def test_an_invalid_engine_name_falls_back_loudly(tmp_path, meminfo) -> None:
    warnings: list[str] = []
    engine = _select({}, tmp_path, meminfo, environ={ENGINE_ENV: "bogus"}, warnings=warnings)
    assert type(engine) is SlotsEngine
    assert len(warnings) == 1 and "slots engine" in warnings[0]


def test_misconfigured_local_settings_fall_back_loudly(tmp_path, meminfo) -> None:
    cfg = {"work": {"validation_broker": {"engine": "local", "packer": {"headroom_cpu": -1}}}}
    warnings: list[str] = []
    assert type(_select(cfg, tmp_path, meminfo, warnings=warnings)) is SlotsEngine
    assert len(warnings) == 1 and "local engine is unavailable" in warnings[0]


def test_an_unmeasurable_host_falls_back_loudly(tmp_path) -> None:
    warnings: list[str] = []
    engine = _select(
        {}, tmp_path, tmp_path / "no-meminfo", environ={ENGINE_ENV: "local"}, warnings=warnings
    )
    assert type(engine) is SlotsEngine
    assert len(warnings) == 1 and "no-meminfo" in warnings[0]


def test_a_local_engine_error_at_runtime_falls_back_to_slots(tmp_path, meminfo) -> None:
    warnings: list[str] = []
    engine = _select({}, tmp_path, meminfo, environ={ENGINE_ENV: "local"}, warnings=warnings)
    meminfo.unlink()  # the host stops being measurable after selection
    handle = engine.submit(job("a"))
    grant = engine.wait_grant(handle, timeout=GENEROUS)
    assert grant.job_id == "a" and engine.name == "slots"
    assert len(warnings) == 1 and "falling back to the slots engine" in warnings[0]
    engine.report(handle, OK, USED)


# ---- the config contract -------------------------------------------------------------------


def test_config_defaults_are_opt_in_and_host_owned() -> None:
    settings = broker_settings({})
    assert settings == ValidationBrokerConfig()
    assert settings.engine == "slots"
    assert WorkConfig().validation_broker == settings
    assert partition_of("work.validation_broker.engine") == HOST
    assert partition_of("work.validation_broker.budget.pools") == HOST


def test_config_rejects_bad_values() -> None:
    for bad in (
        {"engine": "pueue"},
        {"budget": {"memory": "lots"}},
        {"budget": {"pools": {"dolt-servers": -1}}},
        {"floor": {"memory_percent": 101}},
        {"packer": {"aging": -1}},
        {"repoll_seconds": 0},
        {"unknown": 1},
    ):
        with pytest.raises(ValueError):
            ValidationBrokerConfig.model_validate(bad)


# ---- execution roots (bh-mf1o4) -------------------------------------------------------------


def _roots_cfg(*roots: dict) -> dict:
    return {"work": {"validation_broker": {"engine": "local", "roots": list(roots)}}}


def test_roots_default_to_empty_and_slots_ignores_them(tmp_path, meminfo) -> None:
    assert ValidationBrokerConfig().roots == []
    assert partition_of("work.validation_broker.roots") == HOST
    cfg = _roots_cfg({"path": str(tmp_path / "missing"), "kind": "disk", "max_bytes": "1G"})
    cfg["work"]["validation_broker"]["engine"] = "slots"
    warnings: list[str] = []
    assert type(_select(cfg, tmp_path, meminfo, warnings=warnings)) is SlotsEngine
    assert warnings == [], "slots never resolves roots"


def test_local_without_roots_grants_no_root(tmp_path, meminfo) -> None:
    engine = _select({}, tmp_path, meminfo, environ={ENGINE_ENV: "local"})
    assert engine._primary.roots == ()
    handle = engine.submit(job("a", disk_bytes=GIB))
    assert engine.wait_grant(handle, timeout=0).root is None
    engine.report(handle, OK, USED)


def test_local_places_runs_on_the_configured_roots(tmp_path, meminfo) -> None:
    disk, ram = tmp_path / "disk-root", tmp_path / "ram-root"
    disk.mkdir()
    ram.mkdir()
    cfg = _roots_cfg(
        {"path": str(disk), "kind": "disk", "max_bytes": "2G"},
        {"path": str(ram), "kind": "tmpfs", "max_bytes": "4G"},
    )
    space = FakeDiskSpace({str(disk.resolve()): 100 * GIB, str(ram.resolve()): 100 * GIB})
    engine = _select(cfg, tmp_path, meminfo, disk_space=space, memory_backed=lambda p: False)
    assert engine._primary.roots == (
        ExecutionRoot(str(disk.resolve()), RootKind.DISK, 2 * GIB),
        ExecutionRoot(str(ram.resolve()), RootKind.TMPFS, 4 * GIB),
    )
    first = engine.submit(job("a", disk_bytes=GIB, group="g1"))
    second = engine.submit(job("b", disk_bytes=3 * GIB, group="g2"))
    assert engine.wait_grant(first, timeout=0).root == str(disk.resolve())
    grant = engine.wait_grant(second, timeout=0)
    assert grant.root == str(ram.resolve()), "too big for the disk root's 2G bound"
    assert grant.amounts.memory_bytes == 3 * GIB, "tmpfs space is memory"
    engine.report(first, OK, USED)
    engine.report(second, OK, USED)


@pytest.mark.parametrize("problem", ["missing", "disk-on-ram"])
def test_an_unusable_root_falls_back_to_slots_loudly(problem, tmp_path, meminfo) -> None:
    path = tmp_path / "root"
    if problem != "missing":
        path.mkdir()
    cfg = _roots_cfg({"path": str(path), "kind": "disk", "max_bytes": "1G"})
    warnings: list[str] = []
    engine = _select(
        cfg,
        tmp_path,
        meminfo,
        warnings=warnings,
        disk_space=FakeDiskSpace(),
        memory_backed=lambda p: True,
    )
    assert type(engine) is SlotsEngine
    assert len(warnings) == 1 and "local engine is unavailable" in warnings[0]
    expected = "not an existing directory" if problem == "missing" else "RAM-backed"
    assert expected in warnings[0]


def test_a_tmpfs_root_may_be_ram_backed_and_paths_expand(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "runs").mkdir()
    settings = broker_settings(_roots_cfg({"path": "~/runs", "kind": "tmpfs", "max_bytes": "512M"}))
    assert execution_roots(settings, memory_backed=lambda p: True) == (
        ExecutionRoot(str((tmp_path / "runs").resolve()), RootKind.TMPFS, 512 * 1024**2),
    )


def test_config_rejects_bad_roots() -> None:
    for bad in (
        [{"path": "relative", "kind": "disk", "max_bytes": "1G"}],
        [{"path": "/r", "kind": "nfs", "max_bytes": "1G"}],
        [{"path": "/r", "kind": "disk", "max_bytes": "50%"}],
        [{"path": "/r", "kind": "disk", "max_bytes": "0"}],
        [{"path": "/r", "kind": "disk", "max_bytes": "none"}],
        [{"path": "/r", "kind": "disk"}],
        [{"path": "/r", "kind": "disk", "max_bytes": "1G", "extra": 1}],
        [
            {"path": "/r", "kind": "disk", "max_bytes": "1G"},
            {"path": "/r/inner", "kind": "tmpfs", "max_bytes": "1G"},
        ],
        [
            {"path": "/r", "kind": "disk", "max_bytes": "1G"},
            {"path": "/r/", "kind": "disk", "max_bytes": "1G"},
        ],
    ):
        with pytest.raises(ValueError):
            ValidationBrokerConfig.model_validate({"roots": bad})


# ---- local_engine: the side-by-side gate's engine (bh-qhrl6) ---------------------------------


def test_local_engine_is_none_under_slots_and_the_probed_engine_under_local(meminfo) -> None:
    from beadhive.validation_broker_engine import local_engine
    from beadhive_validation_broker import LocalEngine

    warnings: list[str] = []
    ports = {
        "probe": MeminfoAvailability(meminfo, cpu_count=lambda: 16),
        "clock": ManualClock(),
        "owner_alive": lambda _owner: True,
        "warn": warnings.append,
    }
    assert local_engine({}, environ={}, **ports) is None
    assert warnings == []  # slots: nothing to say
    assert isinstance(local_engine({}, environ={ENGINE_ENV: "local"}, **ports), LocalEngine)
    assert local_engine({}, environ={ENGINE_ENV: "bogus"}, **ports) is None
    assert len(warnings) == 1 and "bogus" in warnings[0]
