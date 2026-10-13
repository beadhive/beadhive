"""The attest-key evaluator under each engine (bh-qhrl6).

Under engine ``local`` :func:`selective_validation.run` hands every pending key to the runner's
``run_group`` as one gate; under ``slots`` (the default) it runs exactly as before. The engine is
the package's in-memory fake behind a monkeypatched ``local_engine``; the clean checkout is a
recording double, so no command runs. Concurrency is proven with a barrier every key must reach
while the others still run (a sequential evaluator can never pass it).
"""

from __future__ import annotations

import re
import threading
from contextlib import contextmanager

import pytest
from tests.test_selective_validation import _Resolver
from tests.test_validation_high_water_carry import (
    Hive,
    _keys_cfg,
    _land_api,
    _postland,
)

from beadhive import host, selective_validation, validation_broker_engine, worktree_verify
from beadhive.modules.config.contracts import AttestConfig
from beadhive.validation_broker_engine import ENGINE_ENV
from beadhive.validation_key_runner import CheckoutKeyRunner
from beadhive_validation_broker import Capacity, InMemoryBroker
from beadhive_validation_broker.contract import ManualClock

GENEROUS = 10.0
CATALOG = (
    {"name": "docs", "cmd": "just docs"},
    {"name": "unit", "cmd": "just unit"},
    {"name": "stateful", "cmd": "just stateful"},
    {"name": "integration", "cmd": "just integration"},
)
COSTS = {"docs": 9.0, "unit": 3.0, "stateful": 380.0, "integration": 325.0}
BIG = Capacity(64, 1 << 40, {"dolt-servers": 12})


@pytest.fixture(autouse=True)
def _host():
    host.mint_if_needed()


class _Checkouts:
    """``worktree.clean_checkout`` as a recording double; ``script(cmd)`` gives each rc."""

    def __init__(self, script):
        self.script = script
        self.calls: list[tuple[str, dict]] = []
        self.lock = threading.Lock()

    def clean_checkout(self, entry, ref, cmd, **kwargs):
        with self.lock:
            self.calls.append((cmd, kwargs))
        return self.script(cmd)


@pytest.fixture
def gate(tmp_path, monkeypatch):
    """The evaluator over CATALOG, every key invalidated, in a hermetic environment."""
    monkeypatch.delenv(selective_validation.FAIL_FAST_ENV, raising=False)
    monkeypatch.delenv(ENGINE_ENV, raising=False)
    monkeypatch.setenv("GIT_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setattr(selective_validation, "historical_costs", lambda *_a: dict(COSTS))
    attest = AttestConfig.model_validate({"keys": list(CATALOG)})
    monkeypatch.setattr(selective_validation.config, "attest_config", lambda *_: attest)
    monkeypatch.setattr(selective_validation, "impact_resolver", lambda *_a, **_k: _Resolver())
    shared_dirs: list[str] = []

    @contextmanager
    def shared_verify(entry, branch, label):
        path = tmp_path / f"verify-{len(shared_dirs)}"
        path.mkdir()
        shared_dirs.append(str(path))
        yield path

    monkeypatch.setattr(worktree_verify, "impl_shared_verify_checkout", shared_verify)
    entry = {"provider": "github", "org": "o", "repo": "r"}

    def run(runner, cfg=None):
        return selective_validation.run(
            entry, cfg or {}, base_rev="base", head_rev="head", repo_path="/repo", runner=runner
        )

    def engine(broker):
        monkeypatch.setenv(ENGINE_ENV, "local")
        monkeypatch.setattr(validation_broker_engine, "local_engine", lambda *_a, **_k: broker)

    return run, engine, entry, shared_dirs


def _runner(checkouts, entry, **kwargs):
    return CheckoutKeyRunner(
        checkouts, entry, "head", setup_runner=lambda *_a: 0, cfg={}, reuse=True, **kwargs
    )


def _plain(output: str) -> str:
    return re.sub(r"\d+\.\d{3}s", "<s>", output)


def test_engine_local_runs_every_pending_key_side_by_side_in_one_checkout(gate, capsys) -> None:
    run, engine, entry, shared_dirs = gate
    engine(InMemoryBroker(BIG, ManualClock()))
    rendezvous = threading.Barrier(len(CATALOG), timeout=GENEROUS)

    def script(cmd):
        rendezvous.wait()  # breaks unless all four keys run at once
        return 0

    checkouts = _Checkouts(script)
    assert run(_runner(checkouts, entry)) == 0

    assert sorted(cmd for cmd, _ in checkouts.calls) == sorted(k["cmd"] for k in CATALOG)
    assert len(shared_dirs) == 1  # one shared checkout for the gate
    assert {str(kwargs["shared"].path) for _, kwargs in checkouts.calls} == set(shared_dirs)
    out = capsys.readouterr().out
    assert "running 4 key(s) side by side through the local validation broker" in out
    for key in CATALOG:
        assert f"✓ {key['name']}: ran green (" in out  # one verdict line per key, as before
    assert "side-by-side group (local engine): 4 key(s), group wall" in out


def test_a_red_key_still_blocks_the_gate_and_every_key_gets_its_verdict(gate, capsys) -> None:
    run, engine, entry, _ = gate
    engine(InMemoryBroker(BIG, ManualClock()))
    rcs = {"just unit": 1, "just docs": 0, "just stateful": 75, "just integration": 0}
    checkouts = _Checkouts(rcs.__getitem__)

    assert run(_runner(checkouts, entry)) == 1  # the same aggregate the sequential path gives
    assert sorted(cmd for cmd, _ in checkouts.calls) == sorted(rcs)  # nothing was cut short
    out = capsys.readouterr().out
    assert "✗ unit: ran red (exit 1)" in out
    assert "? stateful: unknown" in out
    assert "✓ integration: ran green" in out


@pytest.mark.parametrize(
    "rcs",
    [
        {"just unit": 0, "just docs": 0, "just stateful": 0, "just integration": 0},
        {"just unit": 1, "just docs": 0, "just stateful": 0, "just integration": 0},
        {"just unit": 0, "just docs": 0, "just stateful": 75, "just integration": 2},
    ],
    ids=["green", "red", "unknown-and-red"],
)
def test_the_aggregate_is_the_same_under_either_engine(gate, rcs) -> None:
    run, engine, entry, _ = gate
    sequential = run(_runner(_Checkouts(rcs.__getitem__), entry))
    engine(InMemoryBroker(BIG, ManualClock()))
    assert run(_runner(_Checkouts(rcs.__getitem__), entry)) == sequential


def test_engine_slots_is_todays_sequential_path_byte_for_byte(gate, capsys) -> None:
    run, _engine, entry, shared_dirs = gate
    calls: list[str] = []

    def lam(cmd):
        calls.append(cmd)
        return 1 if cmd == "just docs" else 0

    assert run(lam) == 1
    before = _plain(capsys.readouterr().out)

    class NoGroups(CheckoutKeyRunner):
        def run_group(self, *_a, **_k):
            raise AssertionError("engine slots never runs a group")

    checkouts = _Checkouts(lambda cmd: 1 if cmd == "just docs" else 0)
    assert run(NoGroups(checkouts, entry, "head", cfg={}, reuse=True)) == 1
    after = _plain(capsys.readouterr().out)

    assert after == before
    assert [cmd for cmd, _ in checkouts.calls] == calls  # same keys, same cheapest-first order
    assert all("shared" not in kwargs for _, kwargs in checkouts.calls)
    assert shared_dirs == []


def test_a_runner_without_run_group_stays_sequential_under_engine_local(gate) -> None:
    run, engine, _entry, shared_dirs = gate
    engine(InMemoryBroker(BIG, ManualClock()))
    calls: list[str] = []
    assert run(lambda cmd: calls.append(cmd) or 0) == 0
    assert calls == ["just unit", "just docs", "just integration", "just stateful"]
    assert shared_dirs == []


def test_an_unavailable_local_engine_is_todays_sequential_path(gate, monkeypatch) -> None:
    run, _engine, entry, shared_dirs = gate
    monkeypatch.setenv(ENGINE_ENV, "local")
    monkeypatch.setattr(validation_broker_engine, "local_engine", lambda *_a, **_k: None)
    checkouts = _Checkouts(lambda cmd: 0)
    assert run(_runner(checkouts, entry)) == 0
    assert [cmd for cmd, _ in checkouts.calls] == [
        "just unit",
        "just docs",
        "just integration",
        "just stateful",
    ]
    assert all("shared" not in kwargs for _, kwargs in checkouts.calls)


def test_a_key_the_engine_cannot_fit_runs_afterwards_on_the_slots_path(gate, capsys) -> None:
    run, engine, entry, _ = gate
    # stateful reserves cpu_min 8 (bh-9oc32): it can never fit a 6-CPU budget.
    engine(InMemoryBroker(Capacity(6, 1 << 40, {"dolt-servers": 12}), ManualClock()))
    checkouts = _Checkouts(lambda cmd: 0)

    assert run(_runner(checkouts, entry)) == 0
    shared = [cmd for cmd, kwargs in checkouts.calls if "shared" in kwargs]
    sequential = [cmd for cmd, kwargs in checkouts.calls if "shared" not in kwargs]
    assert sorted(shared) == ["just docs", "just integration", "just unit"]
    assert sequential == ["just stateful"]
    assert checkouts.calls[-1][0] == "just stateful"  # after the group
    captured = capsys.readouterr()
    assert "1 key(s) fall back to the sequential slots path: stateful" in captured.err
    assert "✓ stateful: ran green" in captured.out


def test_fail_fast_after_the_group_starts_no_fallback_key(gate, monkeypatch, capsys) -> None:
    run, engine, entry, _ = gate
    monkeypatch.setenv(selective_validation.FAIL_FAST_ENV, "1")
    engine(InMemoryBroker(Capacity(6, 1 << 40, {"dolt-servers": 12}), ManualClock()))
    checkouts = _Checkouts(lambda cmd: 1 if cmd == "just unit" else 0)

    assert run(_runner(checkouts, entry)) == 1
    assert "just stateful" not in [cmd for cmd, _ in checkouts.calls]
    assert "stateful: not run — fail-fast after unit" in capsys.readouterr().out


# ---- the boundary's rollback rule is unchanged under engine local ---------------------------


@pytest.fixture
def hive(tmp_path, monkeypatch):
    return Hive(tmp_path, monkeypatch)


def test_a_red_side_by_side_key_rolls_the_land_back_with_the_existing_message(
    hive, tmp_path, monkeypatch
) -> None:
    cfg = _keys_cfg(hive)
    cfg["work"]["validation_broker"] = {"setup": []}
    mol = hive.commit({"docs/r.md": "1"}, "molecule")
    assert hive.land(hive.c0, mol, cfg)[0] == 0
    moved = hive.commit({"j/a.txt": "red"}, "main moved underneath")
    api, ran, events = _land_api(hive, cfg, red=frozenset({"just attest-j"}))
    recorded = api.worktree.clean_checkout
    shared_seen: list[object] = []

    def clean_checkout(entry, rev, cmd, **kwargs):
        shared_seen.append(kwargs.get("shared"))
        return recorded(entry, rev, cmd, **kwargs)

    api.worktree.clean_checkout = clean_checkout

    @contextmanager
    def shared_verify(entry, branch, label):
        yield tmp_path

    monkeypatch.setattr(worktree_verify, "impl_shared_verify_checkout", shared_verify)
    monkeypatch.setenv(ENGINE_ENV, "local")
    monkeypatch.setattr(
        validation_broker_engine,
        "local_engine",
        lambda *_a, **_k: InMemoryBroker(BIG, ManualClock()),
    )

    with pytest.raises(api.typer.Exit):
        _postland(hive, api, cfg, mol, moved)

    assert ran == ["just attest-j"]
    assert shared_seen and all(shared is not None for shared in shared_seen)  # side by side
    assert events[0] == "rollback"
    assert "post-land validation failed (exit 1)" in events[1]
    assert "Rolled tip back to" in events[1]
