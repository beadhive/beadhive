"""`ws.molecule` self-checks — the spec loader + validator.

Pure unit tests: a small inline config (so `registry.closed_dimensions` has model +
harness as closed dims, component open) and in-memory spec dicts. Each invalid case
flips exactly one thing on a known-good spec so the asserted problem is isolated.
The loader is exercised against ruamel-on-disk to confirm pyyaml is not required.
"""

from __future__ import annotations

import pytest

from ws import molecule

# Inline config: model + harness are closed dimensions; component is open (no `values`).
CFG = {
    "dimensions": {
        "model": {"values": ["opus", "sonnet", "haiku"]},
        "harness": {"values": ["claude", "codex"]},
        "component": {"description": "open dim, anything goes"},
    }
}


def _valid_spec() -> dict:
    """A known-good molecule: epic + three issues forming an acyclic dep chain."""
    return {
        "epic": {"title": "Add widgets", "description": "why", "design": "how"},
        "issues": [
            {
                "handle": "a",
                "title": "scaffold",
                "acceptance": "module exists",
                "model": "opus",
                "harness": "claude",
                "component": "runtime",
                "deps": [],
            },
            {
                "handle": "b",
                "title": "implement",
                "acceptance": "feature works",
                "model": "sonnet",
                "deps": ["a"],
            },
            {
                "handle": "c",
                "title": "test",
                "acceptance": "tests pass",
                "deps": ["a", "b"],
            },
        ],
    }


# ---- valid -----------------------------------------------------------------


def test_valid_spec_passes():
    assert molecule.validate_spec(_valid_spec(), CFG) == []
    # validate_or_raise returns the spec unchanged on success
    spec = _valid_spec()
    assert molecule.validate_or_raise(spec, CFG) is spec


# ---- one problem each ------------------------------------------------------


def test_dependency_cycle_flags_one_problem():
    spec = _valid_spec()
    spec["issues"][0]["deps"] = ["c"]  # a -> c -> b -> a
    problems = molecule.validate_spec(spec, CFG)
    assert len(problems) == 1
    assert "cycle" in problems[0]


def test_missing_acceptance_flags_one_problem():
    spec = _valid_spec()
    del spec["issues"][1]["acceptance"]
    problems = molecule.validate_spec(spec, CFG)
    assert len(problems) == 1
    assert "acceptance" in problems[0]
    assert "issue 'b'" in problems[0]


def test_missing_epic_flags_one_problem():
    spec = _valid_spec()
    del spec["epic"]
    problems = molecule.validate_spec(spec, CFG)
    assert len(problems) == 1
    assert "missing epic" in problems[0]


def test_orphan_dep_flags_one_problem():
    spec = _valid_spec()
    spec["issues"][2]["deps"] = ["a", "zzz"]  # zzz is not a real handle
    problems = molecule.validate_spec(spec, CFG)
    assert len(problems) == 1
    assert "zzz" in problems[0]
    assert "unknown handle" in problems[0]


def test_bad_closed_dimension_flags_one_problem():
    spec = _valid_spec()
    spec["issues"][0]["model"] = "gpt4"  # not in the closed model set
    problems = molecule.validate_spec(spec, CFG)
    assert len(problems) == 1
    assert "gpt4" in problems[0]
    assert "model" in problems[0]


def test_open_dimension_accepts_anything():
    spec = _valid_spec()
    spec["issues"][0]["component"] = "literally-anything"  # component is open
    assert molecule.validate_spec(spec, CFG) == []


def test_validate_or_raise_raises_on_invalid():
    spec = _valid_spec()
    del spec["epic"]
    with pytest.raises(molecule.MoleculeError) as exc:
        molecule.validate_or_raise(spec, CFG)
    assert exc.value.problems  # carries the problem list


# ---- batch grouping --------------------------------------------------------


def _batched_spec(**overrides) -> dict:
    """Two issues in the same batch 'g': same component, b depends on a (cohesive + valid)."""
    spec = {
        "epic": {"title": "Add widgets"},
        "issues": [
            {
                "handle": "a",
                "title": "scaffold",
                "acceptance": "exists",
                "model": "opus",
                "component": "runtime",
                "batch": "g",
                "deps": [],
            },
            {
                "handle": "b",
                "title": "extend",
                "acceptance": "works",
                "model": "opus",
                "component": "runtime",
                "batch": "g",
                "deps": ["a"],
            },
        ],
    }
    for issue in spec["issues"]:
        issue.update(overrides)
    return spec


def test_valid_batch_passes():
    assert molecule.validate_spec(_batched_spec(), CFG) == []


def test_unbatched_spec_has_no_batch_problems():
    # _valid_spec declares no batch field at all → batch checks are a no-op.
    assert molecule.validate_spec(_valid_spec(), CFG) == []


def test_mixed_model_batch_flags_one_problem():
    spec = _batched_spec()
    spec["issues"][1]["model"] = "sonnet"  # a=opus, b=sonnet, same batch
    problems = molecule.validate_spec(spec, CFG)
    assert len(problems) == 1
    assert "batch 'g'" in problems[0]
    assert "model" in problems[0]


def test_batch_members_may_omit_model_to_inherit():
    spec = _batched_spec()
    del spec["issues"][1]["model"]  # only one explicit model in the batch → no conflict
    assert molecule.validate_spec(spec, CFG) == []


def test_oversized_batch_flags_one_problem():
    spec = _batched_spec()
    # Default cap is 5; add members until the batch holds 6.
    for n in range(4):
        spec["issues"].append(
            {
                "handle": f"x{n}",
                "title": f"x{n}",
                "acceptance": "ok",
                "model": "opus",
                "component": "runtime",
                "batch": "g",
                "deps": ["a"],
            }
        )
    problems = molecule.validate_spec(spec, CFG)
    assert len(problems) == 1
    assert "exceeds the cap of 5" in problems[0]


def test_batch_cap_is_config_overridable():
    spec = _batched_spec()
    cfg = {**CFG, "work": {"batch_max_size": 1}}  # cap of 1 → a 2-member batch is over
    problems = molecule.validate_spec(spec, cfg)
    assert len(problems) == 1
    assert "exceeds the cap of 1" in problems[0]


def test_non_cohesive_batch_flags_one_problem():
    spec = _batched_spec()
    # Break BOTH cohesion paths: different components AND no dep edge between members.
    spec["issues"][0]["component"] = "runtime"
    spec["issues"][1]["component"] = "docs"
    spec["issues"][1]["deps"] = []  # b no longer depends on a → disconnected
    problems = molecule.validate_spec(spec, CFG)
    assert len(problems) == 1
    assert "not cohesive" in problems[0]


def test_contiguous_batch_with_mixed_components_passes():
    spec = _batched_spec()
    # Different components, but b depends on a → contiguous in the DAG ⇒ cohesive.
    spec["issues"][0]["component"] = "runtime"
    spec["issues"][1]["component"] = "docs"
    assert molecule.validate_spec(spec, CFG) == []


def test_same_component_batch_without_dep_edge_passes():
    spec = _batched_spec()
    spec["issues"][1]["deps"] = []  # disconnected, but same component ⇒ cohesive
    assert molecule.validate_spec(spec, CFG) == []


def test_single_member_batch_is_trivially_cohesive():
    spec = _batched_spec()
    spec["issues"][1]["batch"] = "other"  # each batch now has one member
    assert molecule.validate_spec(spec, CFG) == []


# ---- loader (ruamel, no pyyaml) --------------------------------------------


def test_load_spec_round_trips_yaml(tmp_path):
    p = tmp_path / "spec.yaml"
    p.write_text(
        "epic:\n"
        "  title: Add widgets\n"
        "issues:\n"
        "  - handle: a\n"
        "    title: scaffold\n"
        "    acceptance: module exists\n"
        "    deps: []\n"
    )
    spec = molecule.load_spec(p)
    assert spec["epic"]["title"] == "Add widgets"
    assert molecule.validate_spec(spec, CFG) == []


def test_load_spec_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        molecule.load_spec(tmp_path / "nope.yaml")


def test_load_spec_non_mapping_raises(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("- just\n- a\n- list\n")
    with pytest.raises(molecule.MoleculeError):
        molecule.load_spec(p)
