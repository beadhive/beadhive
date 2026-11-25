"""The pytest face of the kit: subclass :class:`RuntimeConformanceSuite` and supply a factory.

    # tests/test_my_runtime_conformance.py in YOUR package
    from beadhive.runtime.conformance import RuntimeConformanceSuite, RuntimeEnv

    class TestMyRuntime(RuntimeConformanceSuite):
        def make_runtime(self, env: RuntimeEnv):
            return MyRuntime(seat_command=env.seat_command)

Pytest collects the inherited ``test_*`` methods under your ``Test*`` class, one per ADR
invariant. Override :meth:`make_hive` to run against your own :class:`DisposableHive`. The default
hive is a real scratch beads store when ``bd`` is on PATH.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from .bdhive import default_hive
from .checks import CHECKS, RuntimeEnv
from .hive import DisposableHive


class RuntimeConformanceSuite:
    """Not collected itself (the name has no ``Test`` prefix); subclass it as ``Test*``."""

    #: Seconds a turn may take to reach a terminal outcome once released.
    turn_timeout: float = 60.0

    def make_runtime(self, env: RuntimeEnv):  # pragma: no cover - supplied by the subclass
        raise NotImplementedError("override make_runtime(env) to return your Runtime")

    #: Close the hive after each test (set False when :meth:`make_hive` shares one).
    close_hive: bool = True

    def make_hive(self, root: Path) -> DisposableHive:
        """A real scratch beads store when ``bd`` is available (so a runtime's writes through
        ``bd`` are seen), else a :class:`FileHive`."""
        return default_hive(root)

    @pytest.fixture
    def conformance_env(self, tmp_path: Path) -> Iterator[RuntimeEnv]:
        hive = self.make_hive(tmp_path)
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        instructions = tmp_path / "instructions.md"
        instructions.write_text("conformance turn\n", encoding="utf-8")
        yield RuntimeEnv(
            hive=hive,
            seat_command=hive.seat_command(),
            workspace=workspace,
            instructions=instructions,
            timeout=self.turn_timeout,
        )
        close = getattr(hive, "close", None)
        if self.close_hive and callable(close):
            close()


def _bind(name: str, check):
    def test(self: RuntimeConformanceSuite, conformance_env: RuntimeEnv) -> None:
        check(conformance_env, self.make_runtime)

    test.__name__ = f"test_{name}"
    test.__doc__ = (check.__doc__ or "").strip() or None
    return test


for _name, _check in CHECKS.items():
    setattr(RuntimeConformanceSuite, f"test_{_name}", _bind(_name, _check))
