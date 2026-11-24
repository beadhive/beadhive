"""Triplet-less `managed_repos` entries at the raw-index consumers (bh-n2gj1).

`registry.entry_triplet` (bh-p2ptn) names the missing keys. Every consumer below used to index
`entry['provider'|'org'|'repo']` raw and die with a bare `KeyError`. Batch / long-running loops
skip the entry with a warning naming it; single-target paths surface the named error.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from beadhive import (
    backup,
    cache_store,
    daemon_factory,
    doctor,
    hive_services,
    hub,
    operator_sources,
    registry,
    state_stream_polling,
)

GOOD = {"provider": "github", "org": "acme", "repo": "widget", "prefix": "aw", "kind": "hive"}
BAD = {"org": "acme", "repo": "gizmo", "prefix": "ag", "kind": "hive"}  # no provider


def test_cache_store_cache_path_names_the_missing_key(tmp_path):
    with pytest.raises(registry.FleetEntryError, match="missing provider"):
        cache_store.cache_path(tmp_path, BAD)
    assert cache_store.cache_path(tmp_path, GOOD) == tmp_path / "github" / "acme" / "widget"


def test_hub_hive_url_raises_named_error_and_repo_paths_skip_with_warning(capsys):
    with pytest.raises(registry.FleetEntryError, match="missing provider"):
        hub._hive_url({}, BAD)
    paths = hub._managed_repo_paths({}, [BAD, GOOD])
    assert str(registry.hive_dir(GOOD)) in paths
    assert "skipping malformed fleet entry" in capsys.readouterr().err


def test_hub_fetch_cache_reports_triplet_less_entry_instead_of_keyerror(capsys):
    assert hub._fetch_cache({}, BAD) is None
    err = capsys.readouterr().err
    assert "ag: cannot hydrate" in err and "missing provider" in err


def test_hive_services_status_surfaces_named_error(monkeypatch):
    monkeypatch.setattr(hive_services.hive, "status_payload", lambda _cfg: {"hives": []})
    monkeypatch.setattr(hive_services.registry, "resolve_hive", lambda _cfg, _id: BAD)
    registry_service = hive_services.LegacyHiveRegistry(lambda: {})
    request = SimpleNamespace(hive_id="acme/gizmo")
    with pytest.raises(registry.FleetEntryError, match="missing provider"):
        registry_service.status(request)


def test_state_stream_polling_skips_triplet_less_entry_with_warning(caplog):
    cfg = {"managed_repos": [BAD, GOOD]}
    with caplog.at_level(logging.WARNING, logger=state_stream_polling.logger.name):
        adapter = state_stream_polling.PollingStateStreamProvider(
            cfg, backend=object(), poll_interval=0
        )
    assert len(adapter._entries_by_labels) == 1
    assert "skipping malformed fleet entry" in caplog.text


def test_backup_batch_paths_skip_triplet_less_entry_with_warning(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(backup.config, "cache_dir", lambda: tmp_path / "cache")
    cfg = {"managed_repos": [BAD, GOOD]}
    assert backup.classify_cache_entries(cfg) == []
    assert backup._backed_up_entries(cfg) == [GOOD]
    assert "backup skipping malformed fleet entry" in capsys.readouterr().err
    with pytest.raises(registry.FleetEntryError):
        backup.entry_slug(BAD)


def test_doctor_report_drops_triplet_less_entry_with_a_warning():
    cfg, skipped = doctor._skip_tripletless({"managed_repos": [BAD, GOOD]})
    assert cfg["managed_repos"] == [GOOD]
    assert len(skipped) == 1 and "missing provider" in skipped[0]
    assert [row["repo"] for row in doctor._data_hives(cfg)] == ["widget"]


def test_daemon_factory_relationships_name_a_triplet_less_entry():
    hive = SimpleNamespace(identity="x/y/z", entry={**BAD, "upstream": "acme/widget"})
    with pytest.raises(registry.FleetEntryError, match="missing provider"):
        daemon_factory.FactoryDirectory._relationships([hive])


def test_operator_sources_registry_rejects_triplet_less_entry_cleanly():
    sources = SimpleNamespace(cfg={"managed_repos": [BAD]})
    with pytest.raises(operator_sources.OperatorSourceError) as info:
        operator_sources.OperatorSources.registered_hives(sources)
    assert info.value.code == "invalid_registry"
