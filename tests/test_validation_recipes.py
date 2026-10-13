"""check-all-native's attest recipes side by side under engine local (bh-qhrl6).

The recipe executor and setup runner are fakes (nothing runs ``just`` or ``uv``); the broker is
the package's in-memory fake. Under engine slots the recipe never reaches this module (see the
justfile test below); with no engine the module itself runs the recipes exactly as the recipe's
loop does: one at a time, in declaration order, inheriting the environment, stopping at the
first failure.
"""

from __future__ import annotations

import re
import subprocess
import threading
from pathlib import Path

import pytest

from beadhive import validation_broker_engine, validation_recipes
from beadhive.validation_broker_engine import ENGINE_ENV
from beadhive_validation_broker import Capacity, InMemoryBroker
from beadhive_validation_broker.contract import ManualClock

ROOT = Path(__file__).resolve().parents[1]
GENEROUS = 10.0
RECIPES = ("attest-docs", "attest-unit", "attest-packages")
BIG = Capacity(64, 1 << 40, {"dolt-servers": 12})


class _Just:
    def __init__(self, rcs=None, script=None):
        self.rcs = rcs or {}
        self.script = script
        self.calls: list[tuple[str, Path, dict | None, str]] = []
        self.lock = threading.Lock()

    def __call__(self, recipe, cwd, env, label):
        with self.lock:
            self.calls.append((recipe, cwd, env, label))
        if self.script is not None:
            self.script(recipe)
        return self.rcs.get(recipe, 0)


def _run(tmp_path, just, engine, *, setup=None, fail_fast=False, cfg=None, recipes=RECIPES):
    ran_setup: list[str] = []
    rc = validation_recipes.run_recipes(
        recipes,
        cfg=cfg or {},
        cwd=tmp_path,
        engine=engine,
        run=just,
        setup_runner=setup or (lambda command, *_a: ran_setup.append(command) or 0),
        fail_fast=fail_fast,
    )
    return rc, ran_setup


def test_engine_local_runs_the_recipes_side_by_side_with_their_grants(tmp_path, capsys) -> None:
    rendezvous = threading.Barrier(len(RECIPES), timeout=GENEROUS)
    just = _Just(script=lambda _recipe: rendezvous.wait())

    rc, setup = _run(tmp_path, just, InMemoryBroker(BIG, ManualClock()))

    assert rc == 0
    assert setup == ["uv sync --all-packages --inexact"]  # the per-tree setup job, once
    assert sorted(call[0] for call in just.calls) == sorted(RECIPES)
    for recipe, cwd, env, label in just.calls:
        assert cwd == tmp_path  # the gate's own clean checkout, shared
        assert label == recipe.removeprefix("attest-")
        assert env is not None and "BH_GRANT_CPUS" in env and ENGINE_ENV not in env
    grants = {call[0]: call[2]["BH_GRANT_CPUS"] for call in just.calls}
    assert grants["attest-packages"] == "8"  # the packages key's cpu_max on an idle budget
    out = capsys.readouterr().out
    assert "3 attest recipe(s) side by side" in out
    assert "side-by-side group (local engine): 3 key(s), group wall" in out


def test_engine_local_finishes_the_group_and_exits_with_the_first_failure(tmp_path) -> None:
    just = _Just(rcs={"attest-unit": 3, "attest-packages": 4})
    rc, _ = _run(tmp_path, just, InMemoryBroker(BIG, ManualClock()))
    assert rc == 3  # the first failing recipe in declaration order
    assert sorted(call[0] for call in just.calls) == sorted(RECIPES)  # nothing cut short


def test_without_an_engine_it_is_the_recipe_loop(tmp_path) -> None:
    just = _Just(rcs={"attest-unit": 5})
    rc, setup = _run(tmp_path, just, None)
    assert rc == 5
    assert just.calls == [
        ("attest-docs", tmp_path, None, "docs"),
        ("attest-unit", tmp_path, None, "unit"),
    ]  # in order, inherited environment, stopped at the first failure
    assert setup == []


def test_a_failing_setup_runs_every_recipe_one_by_one(tmp_path, capsys) -> None:
    just = _Just()
    rc, _ = _run(tmp_path, just, InMemoryBroker(BIG, ManualClock()), setup=lambda *_a: 1)
    assert rc == 0
    assert [call[0] for call in just.calls] == list(RECIPES)
    assert all(call[2] is None for call in just.calls)
    err = capsys.readouterr().err
    assert "fall back to one-by-one runs" in err and "sequential slots path" in err


def test_a_recipe_the_engine_cannot_fit_runs_one_by_one_after_the_group(tmp_path) -> None:
    just = _Just()
    # attest-stateful reserves cpu_min 8 (bh-9oc32): a 4-CPU budget can never fit it.
    rc, _ = _run(
        tmp_path,
        just,
        InMemoryBroker(Capacity(4, 1 << 40), ManualClock()),
        recipes=("attest-docs", "attest-stateful"),
    )
    assert rc == 0
    assert [(call[0], call[2] is None) for call in just.calls] == [
        ("attest-docs", False),
        ("attest-stateful", True),
    ]


def test_main_resolves_the_engine_and_runs(tmp_path, monkeypatch) -> None:
    seen = {}
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(validation_recipes, "_context", lambda: ({"c": 1}, None))
    monkeypatch.setattr(
        validation_broker_engine,
        "local_engine",
        lambda cfg, **_k: seen.setdefault("cfg", cfg) and None,
    )

    def run_recipes(recipes, **kwargs):
        seen.update(recipes=recipes, **kwargs)
        return 7

    monkeypatch.setattr(validation_recipes, "run_recipes", run_recipes)
    assert validation_recipes.main(["attest-docs", "attest-unit"]) == 7
    assert seen["recipes"] == ["attest-docs", "attest-unit"]
    assert seen["cfg"] == {"c": 1} and seen["engine"] is None
    assert validation_recipes.main([]) == 1


# ---- the recipe: under slots it never leaves its own loop ----------------------------------


def _check_all_native() -> str:
    justfile = (ROOT / "justfile").read_text(encoding="utf-8")
    match = re.search(r"(?m)^check-all-native:[^\n]*\n((?:    .*\n|\n)+?)(?=^\S)", justfile)
    assert match
    return match.group(1)


def _body_script(tmp_path: Path, attest: str) -> Path:
    """The recipe body as a bash script, with ``just`` and ``uv`` on PATH as recorders."""
    body = "\n".join(line[4:] for line in _check_all_native().splitlines())
    script = tmp_path / "body.sh"
    script.write_text(body)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "just").write_text(
        "#!/usr/bin/env bash\n"
        f'if [ "$1" = --summary ]; then echo "{attest}"; exit 0; fi\n'
        'echo "just $*" >> "$LOG"\n'
    )
    (bin_dir / "uv").write_text('#!/usr/bin/env bash\necho "uv $*" >> "$LOG"\n')
    for tool in ("just", "uv"):
        (bin_dir / tool).chmod(0o755)
    return script


@pytest.mark.parametrize("engine", [None, "slots"], ids=["unset", "slots"])
def test_under_slots_check_all_native_runs_its_recipes_one_by_one(tmp_path, engine) -> None:
    script = _body_script(tmp_path, "build attest-docs check attest-unit")
    log = tmp_path / "log"
    env = {"PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin", "LOG": str(log)}
    if engine:
        env[ENGINE_ENV] = engine
    subprocess.run(["bash", str(script)], env=env, check=True, cwd=tmp_path)
    assert log.read_text().splitlines() == ["just attest-docs", "just attest-unit"]


def test_under_local_check_all_native_hands_the_recipes_to_the_broker_runner(tmp_path) -> None:
    script = _body_script(tmp_path, "build attest-docs check attest-unit")
    log = tmp_path / "log"
    env = {"PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin", "LOG": str(log), ENGINE_ENV: "local"}
    subprocess.run(["bash", str(script)], env=env, check=True, cwd=tmp_path)
    assert log.read_text().splitlines() == [
        "uv run python -m beadhive.validation_recipes attest-docs attest-unit"
    ]
