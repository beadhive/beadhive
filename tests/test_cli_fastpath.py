"""The external-report verbs import only their own path (bh-lwz8a).

``bh work outbox|thread|labels <verb>`` is composed by :mod:`beadhive.cli_fastpath` instead of
the whole command tree. These tests pin the structure, never a timing: which modules a real
process loads (in a subprocess, so this test session's imports cannot mask them), that the fast
tree projects exactly the catalog leaves the full tree does, with byte-identical help, and that
everything else still reaches :mod:`beadhive.cli`.
"""

from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.main import get_command
from typer.testing import CliRunner

from beadhive import cli, cli_entrypoint, cli_fastpath, cli_root

ROOT = Path(__file__).resolve().parents[1]

#: Modules of the unrelated command tree a fast-path process must not load.
UNRELATED = (
    "beadhive.cli",
    "beadhive.work",
    "beadhive.alerts",
    "beadhive.doctor",
    "beadhive.host_cli",
    "beadhive.backfill_cli",
    "beadhive.plan",
    "beadhive.release",
    "beadhive.checkpoint",
    "beadhive.toolchain",
    "beadhive.herdr_views",
    "beadhive.worktree",
)

#: The verbs the bead names (single send, batch send, label draft and approve), and a read.
VERBS = (
    ("work", "outbox", "send"),
    ("work", "outbox", "send-approved"),
    ("work", "labels", "sync"),
    ("work", "labels", "approve"),
    ("work", "outbox", "show"),
)


def _loaded_after(argv: list[str], cwd: Path, home: Path) -> dict:
    script = f"""
import json, sys
from beadhive.cli_entrypoint import main
sys.argv = ["bh", *{argv!r}]
code = None
try:
    main()
except SystemExit as exc:
    code = exc.code
unrelated = {list(UNRELATED)!r}
print(json.dumps({{"code": code, "loaded": sorted(m for m in unrelated if m in sys.modules),
                  "fastpath": "beadhive.cli_fastpath" in sys.modules}}))
"""
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT / "src"),
        "HOME": str(home),
        "BH_HOME": str(home / ".beadhive"),
        "BH_SKIP_SETUP_CHECK": "1",
    }
    env = {k: v for k, v in env.items() if not (k.startswith("_") and k.endswith("_COMPLETE"))}
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    lines = result.stdout.strip().splitlines()
    assert lines, result.stderr
    return json.loads(lines[-1])


@pytest.mark.parametrize("verb", VERBS, ids=" ".join)
def test_verb_help_process_skips_the_unrelated_tree(tmp_path, verb):
    outcome = _loaded_after([*verb, "--help"], tmp_path, tmp_path)

    assert outcome == {"code": 0, "loaded": [], "fastpath": True}


def test_verb_run_process_skips_the_unrelated_tree(tmp_path):
    """A real run (not help) goes through the root callback and the handler, whatever the
    verb's outcome on an unconfigured host; it still never loads the unrelated tree."""
    outcome = _loaded_after(["work", "outbox", "show", "--json"], tmp_path, tmp_path)

    assert outcome["fastpath"] is True
    assert outcome["loaded"] == []


@pytest.mark.parametrize(
    "argv",
    [
        ["--help"],
        ["work", "--help"],
        ["work", "outbox", "--help"],
        ["work", "claim", "--help"],
        ["hq", "authority", "--help"],
    ],
    ids=" ".join,
)
def test_everything_else_still_composes_the_full_tree(tmp_path, argv):
    outcome = _loaded_after(argv, tmp_path, tmp_path)

    assert outcome["code"] == 0
    assert "beadhive.cli" in outcome["loaded"]


@pytest.mark.parametrize(
    ("argv", "selected"),
    [
        (["work", "outbox", "send", "ob1_x"], True),
        (["work", "labels", "approve", "--batch", "abc"], True),
        (["work", "thread", "show", "r-1"], True),
        (["work", "outbox", "nope"], False),  # not a catalog leaf: the full tree reports it
        (["work", "outbox", "--help"], False),  # group help lists the group: full tree
        (["work", "outbox"], False),
        (["work", "triage", "review"], False),
        (["--hive", "x", "work", "outbox", "send", "ob1_x"], False),  # a root option
        (["work", "claim", "x"], False),
        ([], False),
    ],
)
def test_selection(argv, selected):
    assert (cli_fastpath.app_for(argv) is not None) is selected


def test_shell_completion_never_takes_the_fast_path(monkeypatch):
    monkeypatch.setenv("_BH_COMPLETE", "bash_complete")
    assert cli_entrypoint._completing() is True
    monkeypatch.delenv("_BH_COMPLETE")
    assert cli_entrypoint._completing() is False


def _leaves(command, path=()):
    for name, child in command.commands.items():
        if getattr(child, "commands", None) is not None:
            yield from _leaves(child, (*path, name))
        else:
            yield " ".join((*path, name)), child


def _full_leaves() -> dict:
    return {
        path: leaf
        for path, leaf in _leaves(get_command(cli.app))
        if path.split()[:2] in [["work", group] for group in cli_fastpath.GROUPS]
    }


def test_fast_tree_projects_exactly_the_catalog_leaves_of_the_full_tree():
    fast = dict(_leaves(get_command(cli_fastpath.build_app())))
    full = _full_leaves()

    assert set(fast) == set(full) == set(cli_fastpath.leaf_paths())
    for path, leaf in fast.items():
        twin = full[path]
        assert [p.name for p in leaf.params] == [p.name for p in twin.params], path
        assert [p.opts for p in leaf.params] == [p.opts for p in twin.params], path
        assert leaf.hidden == twin.hidden, path
        assert inspect.unwrap(leaf.callback) is inspect.unwrap(twin.callback), path


@pytest.mark.parametrize("path", sorted(cli_fastpath.leaf_paths()))
def test_leaf_help_is_identical_on_both_trees(monkeypatch, path):
    argv = [*path.split(), "--help"]
    monkeypatch.setattr(sys, "argv", ["bh", *argv])
    runner = CliRunner()

    fast = runner.invoke(cli_fastpath.build_app(), argv, prog_name="bh")
    full = runner.invoke(cli.app, argv, prog_name="bh")

    assert fast.exit_code == full.exit_code == 0, fast.output + full.output
    assert fast.stdout == full.stdout


def test_both_trees_share_one_root_callback():
    assert cli.app.registered_callback.callback is cli_root._root
    assert cli_fastpath.build_app().registered_callback.callback is cli_root._root
