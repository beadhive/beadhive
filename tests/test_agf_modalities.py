"""AGF harness: supervised + agent-local modalities × work-graph shapes, on real bd.

Validation = the integration branch's git history (authors, verified signatures, branch
names, merge structure).
"""

from __future__ import annotations

import pytest

from harness import graph
from harness.beads import skip_if_no_bd
from harness.hive import make_hive
from harness.modalities import AgentLocalModality, SupervisedModality, run_flow
from harness.render import Timeline, diff_report

pytestmark = [pytest.mark.integration, skip_if_no_bd]

MODALITIES = {"supervised": SupervisedModality, "agent-local": AgentLocalModality}
SHAPES = {
    "independent": lambda r: graph.independent(r, 3),
    "chain": lambda r: graph.chain(r, 3),
    "diamond": graph.diamond,
}


@pytest.mark.parametrize("shape_name", list(SHAPES))
@pytest.mark.parametrize("mod_name", list(MODALITIES))
def test_modality_shape(world, mod_name, shape_name):
    modality = MODALITIES[mod_name](world)
    hive = make_hive(world, work=modality.work_block())
    ids = SHAPES[shape_name](hive)

    label = f"{mod_name}/{shape_name}"
    order = run_flow(hive, ids, modality, label=label)
    assert set(order) == set(ids)  # everything landed, no deadlock

    # The integration history (authors, signatures, branch names, --no-ff merge structure)
    # must match the fixture's expectation. AGF_RENDER=all|diff renders it; see `just`.
    expected = Timeline.from_expected(label, world, modality, order)
    actual = Timeline.from_actual(label, hive)
    assert diff_report(expected, actual), "history diverged — run `just render-int diff` to see"
