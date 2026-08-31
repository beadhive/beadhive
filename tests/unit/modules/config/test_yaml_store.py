"""Port-contract tests for round-trip YAML persistence."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from ruamel.yaml.comments import CommentedMap

from beadhive.modules.config.adapters.yaml_store import RoundTripYamlStore
from beadhive.modules.config.domain.ports import (
    ConfigDocumentEditPort,
    ConfigDocumentLoadPort,
    ConfigDocumentSavePort,
    ConfigScope,
)


def _store(tmp_path, **kwargs):
    paths = {
        ConfigScope.HOST: tmp_path / "config.yaml",
        ConfigScope.FLEET: tmp_path / "fleet.yaml",
    }
    return RoundTripYamlStore(path_for=paths.__getitem__, **kwargs), paths


def test_yaml_adapter_satisfies_each_declared_port(tmp_path):
    store, _paths = _store(tmp_path)

    assert isinstance(store, ConfigDocumentLoadPort)
    assert isinstance(store, ConfigDocumentSavePort)
    assert isinstance(store, ConfigDocumentEditPort)


def test_round_trip_preserves_comments_order_flow_style_and_permissions(tmp_path):
    store, paths = _store(tmp_path)
    paths[ConfigScope.HOST].write_text("# operator\notel: {enabled: true, protocol: grpc}\n")
    os.chmod(paths[ConfigScope.HOST], 0o640)

    document = store.load_document(ConfigScope.HOST)
    document["otel"]["enabled"] = False
    store.save_document(ConfigScope.HOST, document)

    written = paths[ConfigScope.HOST].read_text()
    assert written.startswith("# operator\n")
    assert "otel: {enabled: false, protocol: grpc}" in written
    assert paths[ConfigScope.HOST].stat().st_mode & 0o777 == 0o640


def test_dump_interruption_preserves_original_bytes_and_cleans_temporary_file(tmp_path):
    class BrokenYaml:
        def dump(self, _document, stream):
            stream.write("partial-secret")
            raise RuntimeError("synthetic interruption")

    store, paths = _store(tmp_path, yaml_factory=BrokenYaml)
    original = b"# original\notel: {enabled: true}\n"
    paths[ConfigScope.HOST].write_bytes(original)

    with pytest.raises(RuntimeError, match="synthetic"):
        store.save_document(ConfigScope.HOST, {"otel": {"enabled": False}})

    assert paths[ConfigScope.HOST].read_bytes() == original
    assert list(tmp_path.glob(".config.yaml.*")) == []


@pytest.mark.usefixtures("runtime_test_scope")
def test_edit_port_serializes_complete_thread_transactions(tmp_path):
    store, _paths = _store(tmp_path)
    store.save_document(ConfigScope.HOST, CommentedMap({"values": CommentedMap()}))

    def write(index):
        def edit(document):
            document["values"][str(index)] = index

        store.edit_document(ConfigScope.HOST, edit)

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(write, range(12)))

    assert store.load_document(ConfigScope.HOST)["values"] == {
        str(index): index for index in range(12)
    }


def test_missing_document_error_preserves_the_scaffold_contract(tmp_path):
    store, _paths = _store(tmp_path)

    with pytest.raises(FileNotFoundError, match="scaffold it with:  bh config init"):
        store.load_document(ConfigScope.HOST)

    assert store.load_document(ConfigScope.FLEET, missing_ok=True) == {}
