"""The clean-checkout key runner every gate hands the attest-key evaluator (bh-qhrl6).

:class:`CheckoutKeyRunner` is a drop-in for the ``lambda key_cmd: worktree.clean_checkout(entry,
ref, key_cmd, **kwargs)`` each lifecycle boundary used to pass to
:func:`beadhive.selective_validation.run`: calling it does exactly that, with exactly the caller's
keyword arguments, so under engine ``slots`` nothing changes.

It can also run a gate's pending keys side by side (:meth:`CheckoutKeyRunner.run_group`), which
the evaluator does only under engine ``local``. Then the keys share one clean checkout of the
tree, created when the first grant arrives:

* in the granted execution root (``Grant.root``): a ``run_directory`` there, whose ``TMPDIR``
  every key's hermetic fence puts its scratch under;
* without roots: one ``verify-*`` worktree, today's clean-checkout location.

Its per-tree setup job runs once, under that first grant: the hive's verify init rules (what each
key's own checkout ran before) and then ``work.validation_broker.setup`` (by default
``uv sync --all-packages --inexact``, so no key's own ``uv sync --inexact`` changes the shared
``.venv`` under a reader). A failing setup command sends the gate's keys back to the sequential
slots path. Each key then runs through the same :func:`worktree.clean_checkout`, with
``shared=`` naming the checkout and its environment, so it records the same run record and the
same ledger verdict under the same ``cmd_hash``; its environment is :func:`grant_environ` (the
grant reaches the key only there, never its command).
"""

from __future__ import annotations

import os
import secrets
import shlex
import subprocess
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import typer

from beadhive_validation_broker import LEASE_ENV, BrokerPort, Grant, current_owner

from . import otel, validation_broker_engine, validation_run_root, validation_side_by_side
from .validation_reservations import grant_environ
from .validation_side_by_side import GroupKey, GroupResult, KeyOutcome

__all__ = [
    "CheckoutKeyRunner",
    "SetupFailed",
    "SetupRunner",
    "key_environ",
    "run_setup",
    "subprocess_setup",
]

#: Runs one setup command in a checkout with an environment; returns its exit code.
SetupRunner = Callable[[str, Path, Mapping[str, str]], int]


class SetupFailed(RuntimeError):
    """A per-tree setup command failed: the gate's keys fall back to the slots path."""


def subprocess_setup(command: str, cwd: Path, env: Mapping[str, str]) -> int:
    """:data:`SetupRunner` over a real subprocess (output streams to this process's)."""
    try:
        return subprocess.run(shlex.split(command), cwd=cwd, env=dict(env), check=False).returncode
    except OSError as exc:
        typer.echo(f"✗ setup `{command}` could not run: {exc}", err=True)
        return 127


def run_setup(
    commands: Sequence[str], cwd: Path, env: Mapping[str, str], runner: SetupRunner
) -> None:
    """Run each setup command in order; raise :class:`SetupFailed` at the first that fails."""
    for command in commands:
        typer.echo(f"  → side-by-side setup in {cwd}: {command}")
        rc = runner(command, cwd, env)
        if rc != 0:
            raise SetupFailed(f"setup `{command}` exited {rc}")


#: Passed through from the parent for parity with the sequential slots path (whose keys inherit
#: the whole environment): bh, git and ssh-agent settings. The grant's own variables never come
#: from the parent; everything else stays behind :func:`grant_environ`'s allow-list.
PASSTHROUGH_PREFIXES = ("BH_", "GIT_")
PASSTHROUGH_NAMES = frozenset({"SSH_AUTH_SOCK", "SSH_AGENT_PID"})
_GRANT_ONLY_PREFIXES = ("BH_GRANT_",)
_GRANT_ONLY_NAMES = frozenset({LEASE_ENV})
#: Never reach a key: keys do not choose an engine, and a nested ``check-all-native`` must not
#: recurse into the local runner.
_KEY_DROPPED_NAMES = frozenset({validation_broker_engine.ENGINE_ENV})


def _passthrough(parent: Mapping[str, str]) -> dict[str, str]:
    return {
        name: value
        for name, value in parent.items()
        if (name in PASSTHROUGH_NAMES or name.startswith(PASSTHROUGH_PREFIXES))
        and name not in _GRANT_ONLY_NAMES
        and name not in _KEY_DROPPED_NAMES
        and not name.startswith(_GRANT_ONLY_PREFIXES)
    }


def key_environ(grant: Grant, overlay: Mapping[str, str] | None = None) -> dict[str, str]:
    """A key's environment: :func:`grant_environ` (the allow-listed environment plus the grant),
    the parent's ``BH_*`` / ``GIT_*`` / ssh-agent variables (never ``BH_GRANT_*`` or
    ``BH_VALIDATION_LEASE``, which only the grant sets, nor ``BH_VALIDATION_BROKER_ENGINE``,
    which no key chooses), then the run directory's overlay (``TMPDIR``), with the
    same telemetry and colour neutrality every validation child gets
    (``OTEL_SDK_DISABLED=true``, ``NO_COLOR=1``)."""
    from . import worktree_verify

    parent = otel.telemetry_neutral_env()
    env = grant_environ(grant, base=parent)
    env.update(_passthrough(parent))
    env.update(overlay or {})
    env["OTEL_SDK_DISABLED"] = "true"
    return worktree_verify.impl__color_neutral_env(env)


@dataclass
class _GateWorkspace:
    runner: CheckoutKeyRunner
    root: str | None
    checkout: Path
    overlay: Mapping[str, str] = field(default_factory=dict)

    def execute(self, key: GroupKey, grant: Grant) -> int:
        from . import worktree_verify

        shared = worktree_verify.SharedCheckout(self.checkout, key_environ(grant, self.overlay))
        return self.runner.run_key(key.command, shared)


class CheckoutKeyRunner:
    """``runner(cmd)`` is ``worktree.clean_checkout(entry, ref, cmd, **kwargs)``, unchanged.

    ``worktree`` is the caller's worktree module (or its test double): ``clean_checkout`` is
    looked up on it at call time, exactly as the lambda it replaces did. ``refuse`` names one
    command the runner answers with exit 1 instead of running (the push hook's legacy full gate,
    which only the hook script itself may run).
    """

    def __init__(
        self,
        worktree,
        entry,
        ref: str,
        *,
        refuse: str | None = None,
        setup_runner: SetupRunner = subprocess_setup,
        **clean_checkout_kwargs,
    ) -> None:
        self.worktree = worktree
        self.entry = entry
        self.ref = ref
        self.refuse = refuse
        self.setup_runner = setup_runner
        self.kwargs = clean_checkout_kwargs

    def __call__(self, cmd: str) -> int:
        return self.run_key(cmd, None)

    def run_key(self, cmd: str, shared) -> int:
        if self.refuse is not None and cmd.strip() == self.refuse.strip():
            return 1
        extra = {"shared": shared} if shared is not None else {}
        return self.worktree.clean_checkout(self.entry, self.ref, cmd, **self.kwargs, **extra)

    # ---- side by side (engine local) -----------------------------------------------------

    def _cfg(self) -> dict:
        cfg = self.kwargs.get("cfg")
        if cfg is None:
            from .config_work_settings import load

            try:
                cfg = load()
            except FileNotFoundError:
                cfg = {}
        return cfg

    def run_group(
        self,
        engine: BrokerPort,
        keys: Sequence[GroupKey],
        *,
        report: Callable[[KeyOutcome], None],
        fail_fast: bool = False,
        warn: Callable[[str], None],
    ) -> GroupResult:
        """Run ``keys`` side by side in one shared checkout of ``ref`` (module docstring)."""
        from . import registry, worktree

        cfg = self._cfg()
        phase = str(self.kwargs.get("phase", "validation"))
        sha = worktree._branch_sha(self.entry, self.ref) or self.ref
        group = f"{phase}:{sha[:12]}:{os.getpid()}:{secrets.token_hex(3)}"
        setup = validation_broker_engine.broker_settings(cfg).setup
        main = registry.hive_dir(self.entry)

        @contextmanager
        def workspace(grant: Grant) -> Iterator[_GateWorkspace]:
            with self._checkout(grant, main=main, sha=sha, group=group) as (checkout, overlay):
                worktree.run_init(cfg, self.entry, checkout, verify_only=True)
                run_setup(setup, checkout, key_environ(grant, overlay), self.setup_runner)
                yield _GateWorkspace(self, grant.root, checkout, overlay)

        return validation_side_by_side.run_group(
            engine,
            keys,
            group=group,
            priority=validation_side_by_side.group_priority(phase),
            owner=current_owner(),
            workspace=workspace,
            report=report,
            fail_fast=fail_fast,
            warn=warn,
        )

    @contextmanager
    def _checkout(
        self, grant: Grant, *, main: Path, sha: str, group: str
    ) -> Iterator[tuple[Path, dict[str, str]]]:
        if grant.root is not None:
            with validation_run_root.run_directory(
                grant.root, source=main, commit=sha, job_id=group
            ) as run:
                yield run.checkout, {"TMPDIR": str(run.tmp)}
            return
        from . import worktree_verify

        with worktree_verify.impl_shared_verify_checkout(
            self.entry, self.ref, f"side-by-side gate {group}"
        ) as checkout:
            yield checkout, {}
