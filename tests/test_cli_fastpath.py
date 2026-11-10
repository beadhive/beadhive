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


_LOGGING_PROBE = """
import json, logging, sys
from beadhive import work_intake
seen = {{}}
def _at_entry(*args, **kwargs):  # the handler's view, before anything it does configures logging
    root = logging.getLogger()
    seen.update(handlers=[type(h).__name__ for h in root.handlers], level=root.level)
work_intake.outbox_show = _at_entry
argv = {argv!r}
sys.argv = ["bh", *argv]
if {full!r}:
    from beadhive import cli
    run = cli.main
else:
    from beadhive.cli_entrypoint import main as run
try:
    run()
except SystemExit:
    pass
print(json.dumps({{**seen, "full": "beadhive.cli" in sys.modules}}))
"""


def _logging_after(argv: list[str], full: bool, tmp_path: Path) -> dict:
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT / "src"),
        "HOME": str(tmp_path),
        "BH_HOME": str(tmp_path / ".beadhive"),
        "BH_SKIP_SETUP_CHECK": "1",
    }
    result = subprocess.run(
        [sys.executable, "-c", _LOGGING_PROBE.format(argv=argv, full=full)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    lines = result.stdout.strip().splitlines()
    assert lines, result.stderr
    return json.loads(lines[-1])


def test_both_trees_configure_logging_the_same_way(tmp_path):
    """The full tree configures logging as it imports; the fast tree's root callback does it, so
    stdlib records (httpx and kin) render at log.level / log.format on both (bh-lwz8a)."""
    argv = ["work", "outbox", "show", "--json"]
    fast = _logging_after(argv, full=False, tmp_path=tmp_path)
    full = _logging_after(argv, full=True, tmp_path=tmp_path)

    assert (fast["full"], full["full"]) == (False, True)
    assert fast["handlers"] == full["handlers"] == ["_LiveStderrHandler"]
    assert fast["level"] == full["level"]


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
    """With Click's completion variable set, ``main`` composes the full tree even for a leaf the
    fast path would serve: completion walks the whole command tree."""
    ran: list[str] = []
    monkeypatch.setenv("_BH_COMPLETE", "bash_complete")
    monkeypatch.setattr(cli, "main", lambda: ran.append("full"))
    monkeypatch.setattr(cli_root, "run", lambda app: ran.append("fast"))
    monkeypatch.setattr(cli_entrypoint.sys, "argv", ["bh", "work", "outbox", "send", "ob1_x"])

    cli_entrypoint.main()

    assert ran == ["full"]
    monkeypatch.delenv("_BH_COMPLETE")
    cli_entrypoint.main()
    assert ran == ["full", "fast"]


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


def _registered(app, prefix=()):
    for info in app.registered_commands:
        yield " ".join((*prefix, info.name)), info.callback
    for group in app.registered_groups:
        yield from _registered(group.typer_instance, (*prefix, group.name))


def test_every_fast_leaf_is_traced_like_the_full_tree():
    """Each fast-path leaf is the otel.trace_verb wrapper for its catalog operation, not the
    bare handler: a verb keeps its span whichever tree carried it."""
    fast = dict(_registered(cli_fastpath.build_app()))
    operations = {spec.surfaces["cli"]["path"]: spec.name for spec in cli_fastpath._operations()}

    assert set(fast) == set(operations)
    for path, callback in fast.items():
        assert getattr(callback, "__otel_verb__", None) == operations[path], path


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
