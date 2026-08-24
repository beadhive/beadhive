"""Executable contracts for the config path/store extraction boundaries."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from beadhive import config, config_edit, config_paths, config_store


def test_facade_uses_named_path_and_store_modules(monkeypatch, tmp_path):
    monkeypatch.setattr(config_paths, "home", lambda api: tmp_path / api.BINARY_ALIAS)
    assert config.home() == tmp_path / "bh"

    calls = []
    monkeypatch.setattr(config_store, "load", lambda api: calls.append(api) or {"ok": True})
    assert config.load() == {"ok": True}
    assert calls == [config]


def test_atomic_save_failure_preserves_original_bytes(monkeypatch):
    path = config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    original = b"# operator comment\notel: {enabled: true}\n"
    path.write_bytes(original)

    def fail_dump(_data, _stream):
        raise RuntimeError("synthetic dump failure")

    monkeypatch.setattr(config._yaml, "dump", fail_dump)
    with pytest.raises(RuntimeError, match="synthetic"):
        config.save({"otel": {"enabled": False}})

    assert path.read_bytes() == original
    assert list(path.parent.glob(f".{path.name}.*")) == []


def test_atomic_save_is_parseable_under_thread_races():
    payloads = [{"writer": n, "nested": {"value": str(n) * 100}} for n in range(12)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(config.save, payloads))

    assert config.load_host() in payloads


def test_dotted_edits_are_serialized_without_lost_updates():
    config.save({"custom": {}})

    def write(index):
        return config.set_value(f"custom.key_{index}", str(index))

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(write, range(12)))

    assert all(result["ok"] for result in results)
    assert config.load_host()["custom"] == {f"key_{index}": index for index in range(12)}


def test_edit_facade_calls_named_boundary(monkeypatch):
    monkeypatch.setattr(
        config_edit,
        "get_value",
        lambda api, dotted, cfg, scope: {
            "ok": True,
            "problems": [],
            "value": (api.BINARY_ALIAS, dotted, cfg, scope),
        },
    )
    result = config.get_value("otel.enabled", cfg={"sentinel": True}, scope="host")
    assert result["value"] == ("bh", "otel.enabled", {"sentinel": True}, "host")
