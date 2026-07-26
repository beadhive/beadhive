"""config_partition.py — the fleet vs host key partition (bh-e0y8.3).

Covers: every current `config_schema` leaf lands on exactly one side (the walk-the-schema
enforcement mechanism the AC requires), the AC's named fleet/host key examples resolve to
the expected side, the two prefix sets never overlap, the override-allowlist mechanism
works (independent of whatever's actually in it today), and the materialized
`FLEET_KEYS`/`HOST_KEYS` sets exactly partition the schema's leaves.
"""

from __future__ import annotations

from beadhive.config_partition import (
    FLEET,
    FLEET_HOST_OVERRIDE_ALLOWLIST,
    FLEET_KEYS,
    FLEET_PREFIXES,
    HOST,
    HOST_KEYS,
    HOST_PREFIXES,
    is_host_overridable,
    partition_of,
    schema_leaf_paths,
)

# ---- the enforcement mechanism: every current schema leaf lands in exactly one side ----


def test_every_current_schema_leaf_is_classified():
    """`partition_of` must not return None for any leaf `config_schema` declares TODAY —
    this is what forces a deliberate fleet/host call the moment a new field is added,
    instead of it silently landing nowhere."""
    unclassified = [p for p in schema_leaf_paths() if partition_of(p) is None]
    assert not unclassified, f"unclassified schema keys (needs a fleet/host call): {unclassified}"


def test_every_current_schema_leaf_is_classified_exactly_one_side():
    for path in schema_leaf_paths():
        assert partition_of(path) in (FLEET, HOST), path


def test_host_and_fleet_prefixes_never_overlap():
    assert HOST_PREFIXES.isdisjoint(FLEET_PREFIXES)


def test_fleet_keys_and_host_keys_exactly_partition_the_schema_leaves():
    leaves = set(schema_leaf_paths())
    assert FLEET_KEYS | HOST_KEYS == leaves
    assert FLEET_KEYS.isdisjoint(HOST_KEYS)


def test_schema_leaf_paths_excludes_container_rows():
    """A namespace row (`otel`, `hq`) is not itself a leaf — only its concrete fields are."""
    leaves = set(schema_leaf_paths())
    assert "otel" not in leaves
    assert "otel.enabled" in leaves
    assert "hq" not in leaves
    assert "hq.remote" in leaves
    assert "work.dispatch" not in leaves
    assert "work.dispatch.mode" in leaves


# ---- AC-named fleet keys: orgs, dimensions, exclude, managed_repos, work defaults, -----
# ---- passthrough, delimiter, schema_version --------------------------------------------


def test_ac_named_fleet_keys():
    assert partition_of("schema_version") == FLEET
    assert partition_of("delimiter") == FLEET
    assert partition_of("orgs") == FLEET
    assert partition_of("dimensions") == FLEET
    assert partition_of("exclude.orgs") == FLEET
    assert partition_of("exclude.repos") == FLEET
    assert partition_of("managed_repos") == FLEET
    assert partition_of("passthrough.bd_enabled") == FLEET
    assert partition_of("passthrough.git_enabled") == FLEET
    # "work defaults" — WorkConfig fields other than identity / dispatch budgets.
    assert partition_of("work.validate_cmd") == FLEET
    assert partition_of("work.review_gate") == FLEET
    assert partition_of("work.max_commits") == FLEET
    assert partition_of("work.dispatch.mode") == FLEET


# ---- AC-named host keys: worktrees.path, otel, identity, dispatch budgets, hq.remote ---


def test_ac_named_host_keys():
    assert partition_of("worktrees.path") == HOST
    assert partition_of("otel.enabled") == HOST
    assert partition_of("otel.endpoint") == HOST
    assert partition_of("otel.genai.model") == HOST
    assert partition_of("work.identity.mode") == HOST
    assert partition_of("work.identity.name") == HOST
    assert partition_of("work.dispatch.max_beads_per_session") == HOST
    assert partition_of("work.dispatch.auto_budget") == HOST
    assert partition_of("hq.remote") == HOST


def test_worktrees_path_is_host_but_sibling_fields_stay_fleet():
    """Only the actual disk location is inherently host-specific; naming/behavior
    conventions for worktrees should stay identical across the fleet."""
    assert partition_of("worktrees.path") == HOST
    assert partition_of("worktrees.ephemeral") == FLEET
    assert partition_of("worktrees.bead_branch") == FLEET


def test_dispatch_budgets_are_host_but_sibling_dispatch_policy_stays_fleet():
    assert partition_of("work.dispatch.max_beads_per_session") == HOST
    assert partition_of("work.dispatch.auto_budget") == HOST
    assert partition_of("work.dispatch.mode") == FLEET
    assert partition_of("work.dispatch.review_mode") == FLEET


# ---- override allowlist: mechanism works regardless of what's populated today ----------


def test_override_allowlist_is_explicit_frozenset_data():
    assert isinstance(FLEET_HOST_OVERRIDE_ALLOWLIST, frozenset)


def test_is_host_overridable_true_only_for_an_allowlisted_prefix():
    """Exercises the allowlist mechanism directly (independent of whatever real entries
    `FLEET_HOST_OVERRIDE_ALLOWLIST` carries today) so the behavior is pinned even while
    that set is empty."""
    from beadhive.config_partition import _prefix_match_len

    allowlist = frozenset({"work.validate_cmd"})
    assert _prefix_match_len("work.validate_cmd", allowlist) >= 0
    assert _prefix_match_len("work.review_gate", allowlist) < 0


def test_is_host_overridable_false_for_everything_when_allowlist_is_empty():
    """Pins today's actual (empty) allowlist: no fleet key may currently be overridden."""
    assert FLEET_HOST_OVERRIDE_ALLOWLIST == frozenset()
    assert not is_host_overridable("work.validate_cmd")
    assert not is_host_overridable("delimiter")
