"""Triplet-less `managed_repos` entries at the registry core and the mutating/repair sites
(bh-s5721). Mutating and registry-core paths REFUSE with a named `FleetEntryError` (or a clean
exit 1); telemetry/env derivations degrade; read-only listings skip with a warning.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import typer

from beadhive import (
    hive_ready,
    hive_repair,
    hive_sync,
    hq_control_plane,
    intake_services,
    observaloop_env,
    otel,
    registry,
    source_descriptors,
    storage_migrate,
    sync_remote,
    worktree,
)
from beadhive.modules.hives.domain.identity import identity_record

GOOD = {"provider": "github", "org": "acme", "repo": "widget", "prefix": "aw", "kind": "hive"}
BAD = {"org": "acme", "repo": "gizmo", "prefix": "ag", "kind": "hive"}  # no provider
MISSING = "missing provider"


def _cfg(*entries, **extra):
    return {"managed_repos": list(entries), **extra}


# ---- registry core ---------------------------------------------------------------------------


def test_hive_dir_refuses_a_triplet_less_entry():
    with pytest.raises(registry.FleetEntryError, match=MISSING):
        registry.hive_dir(BAD)


def test_resolve_hive_skips_a_non_matching_malformed_entry_with_warning(capsys):
    cfg = _cfg(BAD, GOOD)
    assert registry.resolve_hive(cfg, "aw") is GOOD
    assert registry.resolve_hive(cfg, "github/acme/widget") is GOOD
    assert registry.resolve_hive(cfg, "acme/widget") is GOOD
    assert "skipping malformed fleet entry" in capsys.readouterr().err


@pytest.mark.parametrize("hive_id", ["ag", "acme/gizmo", "gizmo", "github/acme/gizmo"])
def test_resolve_hive_refuses_when_the_target_itself_is_malformed(hive_id):
    # prefix, org/repo, repo, and a triplet whose carried fields agree all name BAD as the target
    with pytest.raises(registry.FleetEntryError, match=MISSING):
        registry.resolve_hive(_cfg(BAD, GOOD), hive_id)


def test_resolve_hive_unknown_id_is_unchanged_next_to_a_malformed_entry(capsys):
    with pytest.raises(typer.Exit):
        registry.resolve_hive(_cfg(BAD, GOOD), "nope")
    assert "no hive matching 'nope'" in capsys.readouterr().err


def test_hive_matches_refuses_a_malformed_target():
    with pytest.raises(registry.FleetEntryError, match=MISSING):
        registry.hive_matches(_cfg(BAD, GOOD), "ag")
    assert registry.hive_matches(_cfg(BAD, GOOD), "aw") == (GOOD,)


def test_entry_matches_triplet_refuses_only_a_possible_target():
    assert registry.entry_matches_triplet(GOOD, "github", "acme", "widget") is True
    assert registry.entry_matches_triplet(GOOD, "github", "acme", "other") is False
    assert registry.entry_matches_triplet(BAD, "github", "zzz", "other") is False
    with pytest.raises(registry.FleetEntryError, match=MISSING):
        registry.entry_matches_triplet(BAD, "github", "acme", "gizmo")


def test_prefix_collisions_and_required_violations_report_instead_of_aborting():
    dup = {**BAD, "prefix": "aw"}
    cols = registry.prefix_collisions(_cfg(dup, GOOD))
    assert cols == [{"prefix": "aw", "hives": ["acme/gizmo", "acme/widget"]}]
    cfg = _cfg(BAD, {"prefix": "x", "kind": "hive"}, orgs={"acme": {"policy": "required"}})
    out = registry.required_violations(cfg)
    assert any("malformed fleet entry" in v for v in out)


def test_docs_listing_skips_a_triplet_less_entry_with_warning(capsys):
    lines = registry._docs_managed_hives(_cfg(BAD, GOOD))
    assert "## Managed hives (1)" in lines[0]
    assert any("github/acme/widget" in ln for ln in lines)
    assert "skipping malformed fleet entry" in capsys.readouterr().err


# ---- mutating / repair -----------------------------------------------------------------------


def test_hive_repair_refuses_a_malformed_target_with_repair_error():
    with pytest.raises(hive_repair.RepairError, match=MISSING):
        hive_repair._resolve_entry(_cfg(BAD, GOOD), "ag")


def test_hive_repair_resolves_a_healthy_hive_beside_a_malformed_entry():
    assert hive_repair._resolve_entry(_cfg(BAD, GOOD), "aw") is GOOD


def test_storage_migrate_fleet_order_refuses_rather_than_dropping_the_entry():
    with pytest.raises(registry.FleetEntryError, match=MISSING):
        storage_migrate.fleet_order(_cfg(BAD, GOOD))


def test_hive_sync_hive_id_and_targets_refuse():
    with pytest.raises(registry.FleetEntryError, match=MISSING):
        hive_sync._hive_id(BAD)
    with pytest.raises(registry.FleetEntryError, match=MISSING):
        hive_sync._targets(_cfg(BAD, GOOD), None)


def test_hive_sync_exits_clean_before_touching_any_hive(monkeypatch, capsys):
    monkeypatch.setattr(hive_sync.config, "load", lambda: _cfg(BAD, GOOD))
    with pytest.raises(typer.Exit) as exc:
        hive_sync.hive_sync(dry_run=True)
    assert exc.value.exit_code == 1
    assert "refusing to sync" in capsys.readouterr().err


def test_sync_remote_refuses_all_hive_targets_and_exits_clean(monkeypatch, capsys):
    cfg = _cfg(BAD, GOOD)
    with pytest.raises(registry.FleetEntryError, match=MISSING):
        sync_remote._resolve_targets(cfg, None)
    monkeypatch.setattr(sync_remote.config, "load", lambda: cfg)
    with pytest.raises(typer.Exit) as exc:
        sync_remote.sync_remote(dry_run=True)
    assert exc.value.exit_code == 1
    assert "refusing to sync" in capsys.readouterr().err


def test_worktree_wt_dir_refuses_a_triplet_less_entry():
    with pytest.raises(registry.FleetEntryError, match=MISSING):
        worktree.wt_dir(BAD, "leaf")


# ---- telemetry / env: never crash a command --------------------------------------------------


def test_otel_derived_hive_omits_a_triplet_less_entry():
    cfg = _cfg(BAD, GOOD)
    assert otel._derived_hive(cfg, ("github", "acme", "widget")) == "aw"
    assert otel._derived_hive(cfg, ("github", "acme", "gizmo")) == "gizmo"


def test_observaloop_entry_for_falls_back_past_a_triplet_less_entry():
    cfg = _cfg(BAD, GOOD)
    assert observaloop_env._entry_for(cfg, Path("/r/github/acme/widget/leaf")) is GOOD
    synth = observaloop_env._entry_for(cfg, Path("/r/github/acme/gizmo/leaf"))
    assert synth["prefix"] == "gizmo"


# ---- the others ------------------------------------------------------------------------------


def test_hq_control_plane_managed_hive_dir_names_the_missing_key():
    with pytest.raises(hq_control_plane.ControlPlaneError, match=MISSING):
        hq_control_plane._managed_hive_dir(BAD)


def test_hive_ready_schema_check_warns_on_a_triplet_less_entry():
    check = hive_ready._schema_version_check(BAD, Path("."))
    assert check.state == "warn" and MISSING in check.detail


def test_source_descriptors_identity_names_the_missing_key():
    with pytest.raises(registry.FleetEntryError, match=MISSING):
        source_descriptors._identity(BAD)


def test_intake_services_store_scope_names_the_missing_key():
    with pytest.raises(registry.FleetEntryError, match=MISSING):
        intake_services.store_scope({}, BAD)


def test_hive_identity_record_names_the_missing_key():
    with pytest.raises(ValueError, match="missing provider"):
        identity_record(BAD)
    assert identity_record(GOOD)["provider"] == "github"
