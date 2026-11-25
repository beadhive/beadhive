"""Triplet-less `managed_repos` entries at the remaining raw-index consumers (bh-n2gj1).

Read-only reports skip the entry with a warning. Anything that deletes, prunes, retires or
unregisters REFUSES instead: a skip there would shrink the candidate set unseen.
"""

from __future__ import annotations

import pytest
import typer

from beadhive import (
    doctor,
    hive,
    hive_local_only,
    host_retire,
    hq,
    operator_contract,
    registry,
    retire,
    stream_cli,
    survey,
    worktree_inventory,
)

GOOD = {"provider": "github", "org": "acme", "repo": "widget", "prefix": "aw", "kind": "hive"}


BAD = {"org": "acme", "repo": "gizmo", "prefix": "ag", "kind": "hive"}  # no provider


def test_survey_skips_triplet_less_entry_with_warning(monkeypatch, capsys):
    monkeypatch.setattr(survey.gitworkspace, "tracked_repos", lambda _cfg: [])
    keys = survey._all_repo_keys({"managed_repos": [BAD, GOOD]})
    assert keys == {"github/acme/widget": True}
    assert "survey skipping malformed fleet entry" in capsys.readouterr().err


def test_hq_init_reports_malformed_hq_entry_cleanly(monkeypatch, capsys):
    monkeypatch.setattr(hq.registry, "hive_of_kind", lambda _cfg, _k: BAD)
    with pytest.raises(typer.Exit):
        hq.init()
    assert "missing provider" in capsys.readouterr().err


def test_host_retire_assess_refuses_a_triplet_less_entry(monkeypatch, tmp_path):
    monkeypatch.setattr(registry, "hives", lambda _cfg: [BAD, GOOD])
    with pytest.raises(registry.FleetEntryError, match="missing provider"):
        host_retire.assess(cfg={}, hq_dir=tmp_path, host_id="h1")


def test_host_retire_reclaim_set_never_shrinks_past_a_malformed_entry():
    with pytest.raises(registry.FleetEntryError, match="missing provider"):
        host_retire._step_reclaim_hives(
            {"managed_repos": [BAD, GOOD]}, dry_run=True, backup=False, confirm=False, purge=False
        )


def test_host_retire_refuses_before_any_mutation(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(host_retire.host_cli, "_require_hq_dir", lambda: tmp_path)
    monkeypatch.setattr(host_retire.host_cli, "_require_host_id", lambda: "h1")
    monkeypatch.setattr(host_retire, "_refuse_owned_local_only", lambda *_a: None)
    monkeypatch.setattr(registry, "hives", lambda _cfg: [BAD])
    results = host_retire.retire(dry_run=False, confirm=True)
    assert [r.status for r in results] == ["failed"]
    assert "refused" in capsys.readouterr().err


@pytest.mark.parametrize("fn", [retire.retire_hive, retire.reclaim_hive])
def test_retire_refuses_a_triplet_less_entry(monkeypatch, capsys, fn):
    monkeypatch.setattr(retire.registry, "resolve_hive", lambda _cfg, _h: BAD)
    with pytest.raises(typer.Exit):
        fn("acme/gizmo", dry_run=True)
    assert "missing provider" in capsys.readouterr().err


def test_hive_local_only_key_and_clear_name_the_missing_key():
    with pytest.raises(registry.FleetEntryError, match="missing provider"):
        hive_local_only._key(BAD)
    assert hive_local_only._key(GOOD) == "github/acme/widget"


def test_worktree_inventory_status_scope_refuses_a_triplet_less_entry(monkeypatch):
    monkeypatch.setattr(worktree_inventory, "workspace_identity", lambda: ("github", "acme", "w"))
    cfg = {"managed_repos": [BAD, GOOD]}
    with pytest.raises(registry.FleetEntryError, match="missing provider"):
        worktree_inventory.impl__status_scope(cfg, "", [])


def test_worktree_inventory_status_cmd_exits_clean(monkeypatch, capsys):
    def _scope(cfg, h, rows):
        return worktree_inventory.impl__status_scope({"managed_repos": [BAD]}, h, rows)

    monkeypatch.setattr(worktree_inventory, "managed", lambda _cfg: [])
    monkeypatch.setattr(worktree_inventory, "workspace_identity", lambda: ("github", "acme", "w"))
    monkeypatch.setattr(worktree_inventory, "_status_scope", _scope)
    with pytest.raises(typer.Exit):
        worktree_inventory.impl_status_cmd("")
    assert "missing provider" in capsys.readouterr().err


def test_stream_cli_hive_slug_exits_clean(monkeypatch, capsys):
    monkeypatch.setattr(stream_cli.registry, "resolve_hive", lambda _cfg, _h: BAD)
    with pytest.raises(typer.Exit):
        stream_cli._hive_slug({}, "acme/gizmo")
    assert "missing provider" in capsys.readouterr().err
    monkeypatch.setattr(stream_cli.registry, "resolve_hive", lambda _cfg, _h: GOOD)
    assert stream_cli._hive_slug({}, "x") == "widget"


def test_backfill_hive_group_falls_back_for_a_triplet_less_entry(tmp_path):
    from beadhive import backfill_cli

    group = backfill_cli.hive_group(BAD, tmp_path)
    assert group.description == f"bead hive path:{tmp_path.resolve()}"


def test_operator_contract_names_the_missing_key():
    with pytest.raises(registry.FleetEntryError, match="missing provider"):
        operator_contract.hive_info(BAD, canonical_prefix=False)
    with pytest.raises(registry.FleetEntryError, match="missing provider"):
        operator_contract.factory_hive_summary(BAD, None)
    assert operator_contract.hive_info(GOOD, canonical_prefix=True)["prefix"] == (
        "github/acme/widget"
    )


def test_hive_rm_refuses_a_triplet_less_entry(monkeypatch):
    monkeypatch.setattr(hive.registry, "resolve_hive", lambda _cfg, _h: BAD)
    with pytest.raises(registry.FleetEntryError, match="missing provider"):
        hive.rm("acme/gizmo", dry_run=True)
    with pytest.raises(registry.FleetEntryError):
        hive._restore_cmd(BAD)


def test_hive_read_only_reports_skip_with_warning(monkeypatch, capsys):
    monkeypatch.setattr(hive.registry, "local_only_of", lambda _e: None)
    cfg = {"managed_repos": [BAD, GOOD]}
    assert hive.status_payload.__name__  # keep import used
    assert [h["repo"] for h in _status_hives(cfg, monkeypatch)] == ["widget"]
    assert "skipping malformed fleet entry" in capsys.readouterr().err


def _status_hives(cfg, monkeypatch):
    monkeypatch.setattr(hive, "available", lambda _cfg: {"candidates": []})
    monkeypatch.setattr(hive.registry, "prefix_collisions", lambda _cfg: [])
    monkeypatch.setattr(hive.registry, "required_violations", lambda _cfg: [])
    return hive.status_payload(cfg)["hives"]


@pytest.mark.parametrize(
    "collector",
    [
        doctor._data_hives,
        doctor._data_prefix_mismatches,
        doctor._data_beads_role,
        doctor._data_unpublished,
        doctor._data_writer_fence,
        doctor._data_forward,
    ],
)
def test_doctor_section_collectors_survive_a_triplet_less_entry(collector, monkeypatch, tmp_path):
    monkeypatch.setattr(doctor, "workspace_root", lambda: str(tmp_path))
    collector({"managed_repos": [BAD]})  # must not KeyError


def test_doctor_hives_lists_only_complete_entries_when_called_directly():
    assert [r["repo"] for r in doctor._data_hives({"managed_repos": [BAD, GOOD]})] == ["widget"]
    assert doctor._data_disk_usage([BAD], __import__("pathlib").Path("/nonexistent"), {})
