"""check-all-native's attest-* recipes side by side under engine local (bh-qhrl6).

``just check-all-native`` discovers its ``attest-*`` recipes and, only when
``BH_VALIDATION_BROKER_ENGINE`` names an engine other than ``slots``, hands them to
``python -m beadhive.validation_recipes <recipe>...`` instead of running them one by one. (A clean
checkout validation exports ``BH_VALIDATION_BROKER_ENGINE=local`` when the hive's config selects
the local engine, so the config reaches the recipe too.) Under ``slots`` the recipe never calls
this module.

Here the recipes are the gate's keys, submitted as one gate group through the same runner as the
evaluator's (:func:`beadhive.validation_side_by_side.run_group`): one shared checkout (the current
one, which is the gate's clean checkout, or a run directory in the granted execution root), the
same per-tree setup job (``work.validation_broker.setup``) under the first grant, each recipe
launched with :func:`~beadhive.validation_key_runner.key_environ` (``BH_GRANT_CPUS`` and
friends), the same failure policy (the group finishes; ``BH_VALIDATION_FAIL_FAST`` stops further
recipes starting) and the same per-key and group wall report. Each recipe's output lines are
prefixed with ``[<key>]``.

If the local engine is not selected, is unavailable, or fails, the recipes it did not run go
through ``just <recipe>`` one at a time in declaration order, stopping at the first failure, as
the recipe's own loop does; that is announced on stderr. Exit status: the first failing recipe's
(in declaration order), else 0.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from beadhive_validation_broker import BrokerPort, Grant, current_owner

from . import validation_broker_engine, validation_reservations, validation_run_root
from .validation_key_runner import SetupRunner, key_environ, run_setup, subprocess_setup
from .validation_side_by_side import (
    GroupKey,
    KeyOutcome,
    group_priority,
    run_group,
    summary_lines,
)

__all__ = ["RecipeExec", "main", "run_recipes", "subprocess_recipe"]

RECIPE_PREFIX = "attest-"

#: Runs ``just <recipe>`` in a checkout with an environment, labelling output; its exit code.
RecipeExec = Callable[[str, Path, Mapping[str, str] | None, str], int]

_PRINT = threading.Lock()


def _say(line: str, *, err: bool = False) -> None:
    with _PRINT:
        print(line, file=sys.stderr if err else sys.stdout, flush=True)


def subprocess_recipe(recipe: str, cwd: Path, env: Mapping[str, str] | None, label: str) -> int:
    """:data:`RecipeExec` over ``just``; ``env=None`` inherits this process's environment and
    output, exactly as the recipe's own loop runs a recipe."""
    if env is None:
        return subprocess.run(["just", recipe], cwd=cwd, check=False).returncode
    with subprocess.Popen(
        ["just", recipe],
        cwd=cwd,
        env=dict(env),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
    ) as child:
        assert child.stdout is not None
        for line in child.stdout:
            _say(f"[{label}] {line.rstrip()}")
        return child.wait()


@dataclass
class _RecipeWorkspace:
    root: str | None
    checkout: Path
    run: RecipeExec
    overlay: Mapping[str, str] = field(default_factory=dict)

    def execute(self, key: GroupKey, grant: Grant) -> int:
        return self.run(key.command, self.checkout, key_environ(grant, self.overlay), key.name)


def _sequential(recipes: Sequence[str], cwd: Path, run: RecipeExec) -> int:
    for recipe in recipes:
        rc = run(recipe, cwd, None, recipe.removeprefix(RECIPE_PREFIX))
        if rc != 0:
            return rc
    return 0


def run_recipes(
    recipes: Sequence[str],
    *,
    cfg: dict,
    entry=None,
    cwd: Path,
    engine: BrokerPort | None,
    run: RecipeExec = subprocess_recipe,
    setup_runner: SetupRunner = subprocess_setup,
    fail_fast: bool = False,
) -> int:
    """Run ``recipes`` side by side through ``engine``, or one by one when it is ``None``."""
    if engine is None:
        return _sequential(recipes, cwd, run)
    settings = validation_broker_engine.broker_settings(cfg, entry)
    setup = settings.setup
    keys = []
    for recipe in recipes:
        name = recipe.removeprefix(RECIPE_PREFIX)
        keys.append(
            GroupKey(
                name=name,
                command=recipe,
                reservation=validation_reservations.resolve_reservation(name, cfg, entry),
                expected_seconds=validation_reservations.expected_seconds(name, cfg, entry),
            )
        )
    group = f"check-all-native:{os.getpid()}"

    @contextmanager
    def workspace(grant: Grant) -> Iterator[_RecipeWorkspace]:
        if grant.root is None:
            run_setup(setup, cwd, key_environ(grant), setup_runner)
            yield _RecipeWorkspace(None, cwd, run)
            return
        with validation_run_root.run_directory(
            grant.root, source=cwd, commit="HEAD", job_id=group
        ) as space:
            overlay = {"TMPDIR": str(space.tmp)}
            run_setup(setup, space.checkout, key_environ(grant, overlay), setup_runner)
            yield _RecipeWorkspace(grant.root, space.checkout, run, overlay)

    def report(outcome: KeyOutcome) -> None:
        if outcome.rc is None:
            _say(f"  · {outcome.name}: not run — fail-fast")
            return
        mark = "✓" if outcome.rc == 0 else "✗"
        state = "green" if outcome.rc == 0 else f"red (exit {outcome.rc})"
        _say(f"  {mark} {outcome.name}: {state} ({outcome.wall_s:.3f}s)")

    _say(f"check-all-native: {len(keys)} attest recipe(s) side by side (local validation broker)")
    result = run_group(
        engine,
        keys,
        group=group,
        priority=group_priority("check-all-native"),
        owner=current_owner(),
        workspace=workspace,
        report=report,
        fail_fast=fail_fast,
        warn=lambda line: _say(line, err=True),
        laggard_first=settings.packer.laggard_first,
    )
    for line in summary_lines(result):
        _say(line)
    by_recipe = {key.command: key.name for key in keys}
    failed = [
        recipe
        for recipe in recipes
        if (outcome := result.outcomes.get(by_recipe[recipe])) is not None and outcome.rc
    ]
    if failed:
        return result.outcomes[by_recipe[failed[0]]].rc or 1
    if result.stopped_after:
        return 1
    fallback = [recipe for recipe in recipes if by_recipe[recipe] in result.fallback]
    if fallback:
        _say(
            f"! check-all-native: {len(fallback)} recipe(s) fall back to one-by-one runs: "
            f"{', '.join(fallback)}",
            err=True,
        )
    return _sequential(fallback, cwd, run)


def _context() -> tuple[dict, object]:
    """The host config and the hive owning the current directory; ``({}, None)`` when unknown."""
    try:
        from . import registry
        from .config_work_settings import load

        cfg = load()
    except Exception:  # noqa: BLE001 - no readable config: defaults, env override only
        return {}, None
    try:
        return cfg, registry.current_hive(cfg)
    except Exception:  # noqa: BLE001 - no hive: default reservations, no per-key override
        return cfg, None


def main(argv: Sequence[str] | None = None) -> int:
    recipes = list(sys.argv[1:] if argv is None else argv)
    if not recipes:
        _say("check-all-native: no attest-* recipes given", err=True)
        return 1
    cfg, entry = _context()
    engine = validation_broker_engine.local_engine(
        cfg, entry=entry, warn=lambda line: _say(line, err=True)
    )
    if engine is None:
        _say("check-all-native: running the attest recipes one by one (slots engine)", err=True)
    from .selective_validation import fail_fast_enabled

    return run_recipes(
        recipes,
        cfg=cfg,
        entry=entry,
        cwd=Path.cwd(),
        engine=engine,
        fail_fast=fail_fast_enabled(),
    )


if __name__ == "__main__":
    raise SystemExit(main())
