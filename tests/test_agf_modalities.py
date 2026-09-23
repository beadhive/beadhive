"""AGF harness: supervised + agent-local modalities × work-graph shapes, on real bd.

Validation = the integration branch's git history (authors, verified signatures, branch
names, merge structure).
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import time
from collections import Counter
from pathlib import Path

import pytest

from harness import graph
from harness.beads import skip_if_no_bd
from harness.hive import empty_beads_template, make_hive, template_digest
from harness.modalities import AgentLocalModality, SupervisedModality, run_flow
from harness.render import Timeline, diff_report

pytestmark = [pytest.mark.integration, skip_if_no_bd]

MODALITIES = {"supervised": SupervisedModality, "agent-local": AgentLocalModality}
SHAPES = {
    "independent": lambda r: graph.independent(r, 3),
    "chain": lambda r: graph.chain(r, 3),
    "diamond": graph.diamond,
}


@pytest.fixture(scope="session")
def agf_beads_template(tmp_path_factory):
    """One read-only-by-contract seed; every case receives a deep, independently mutable copy."""
    shared_root = tmp_path_factory.getbasetemp().parent / "agf-beads-template"
    with _profile_stage("_shared", "hive_template_setup"):
        template = empty_beads_template(shared_root)
    before = template_digest(template)
    yield template
    assert template_digest(template) == before, "an AGF case mutated the shared bd template"


def _command_name(args) -> str:
    """Stable, low-cardinality command name for the opt-in AGF benchmark profile."""
    argv = args.split() if isinstance(args, str) else [str(arg) for arg in (args or [])]
    if not argv:
        return "unknown"
    tool = Path(argv[0]).name
    options_with_values = {"-C", "-c", "--actor", "--config-env", "--git-dir", "--work-tree"}
    operation = ""
    index = 1
    while index < len(argv):
        arg = argv[index]
        if arg in options_with_values:
            index += 2
        elif arg.startswith("-"):
            index += 1
        else:
            operation = arg
            break
    return " ".join(part for part in (tool, operation) if part)


@contextlib.contextmanager
def _profile_stage(label: str, stage: str):
    """Append one stage sample when ``AGF_PROFILE`` names an NDJSON output file.

    Patching ``subprocess.Popen`` at this outer seam counts every direct command made by the
    harness and product during the stage. The profile is opt-in, process-local, and each stage
    writes one append-only line, so focused xdist runs remain independently attributable.
    """
    output = os.environ.get("AGF_PROFILE")
    if not output:
        yield
        return

    original = subprocess.Popen
    commands: Counter[str] = Counter()

    def counted_popen(*popenargs, **kwargs):
        args = kwargs.get("args", popenargs[0] if popenargs else [])
        commands[_command_name(args)] += 1
        return original(*popenargs, **kwargs)

    started = time.monotonic()
    subprocess.Popen = counted_popen
    try:
        yield
    finally:
        subprocess.Popen = original
        sample = {
            "label": label,
            "stage": stage,
            "seconds": round(time.monotonic() - started, 6),
            "command_count": sum(commands.values()),
            "commands": dict(sorted(commands.items())),
        }
        with Path(output).open("a") as stream:
            stream.write(json.dumps(sample, sort_keys=True) + "\n")


@pytest.mark.parametrize("shape_name", list(SHAPES))
@pytest.mark.parametrize("mod_name", list(MODALITIES))
def test_modality_shape(world, agf_beads_template, mod_name, shape_name):
    modality = MODALITIES[mod_name](world)
    label = f"{mod_name}/{shape_name}"
    with _profile_stage(label, "hive_creation"):
        hive = make_hive(
            world,
            work=modality.work_block(),
            batch_bd_writes=True,
            beads_template=agf_beads_template,
        )
    with _profile_stage(label, "graph_construction"):
        ids = SHAPES[shape_name](hive)
    with _profile_stage(label, "workflow"):
        order = run_flow(hive, ids, modality, label=label)
    assert set(order) == set(ids)  # everything landed, no deadlock

    # The integration history (authors, signatures, branch names, --no-ff merge structure)
    # must match the fixture's expectation. AGF_RENDER=all|diff renders it; see `just`.
    with _profile_stage(label, "timeline_rendering"):
        expected = Timeline.from_expected(label, world, modality, order)
        actual = Timeline.from_actual(label, hive)
        assert diff_report(expected, actual), "history diverged — run `just render-int diff` to see"
