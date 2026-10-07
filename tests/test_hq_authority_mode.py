"""hq.authority_mode / BH_HQ_AUTHORITY_MODE resolver (bh-dzb8m)."""

from __future__ import annotations

import pytest

from beadhive import hq_authority_enforce as ae
from beadhive.modules.config import contracts
from beadhive.modules.config.application import partition


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in (ae.MODE_ENV, ae.ENFORCE_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(ae, "_deprecation_emitted", False)
    monkeypatch.setattr(ae, "_invalid_emitted", set())


def test_default_and_inherit_resolve_signed():
    assert ae.resolve(None) == ("signed", "default")
    assert ae.resolve("inherit").mode == "signed"
    assert ae.resolve("signed").mode == "signed"


def test_key_trusted_then_env_overrides(monkeypatch):
    assert ae.resolve("trusted").mode == "trusted"
    monkeypatch.setenv(ae.MODE_ENV, "signed")
    assert ae.resolve("trusted") == ("signed", "env")
    monkeypatch.setenv(ae.MODE_ENV, "trusted")
    assert ae.resolve("signed") == ("trusted", "env")


def test_invalid_fails_closed_with_error(monkeypatch, capsys):
    monkeypatch.setenv(ae.MODE_ENV, "bogus")
    assert ae.resolve("trusted").mode == "signed"
    assert "failing closed to signed" in capsys.readouterr().err
    monkeypatch.delenv(ae.MODE_ENV)
    assert ae.resolve("nope").mode == "signed"


def test_enforce_false_is_trusted_and_warns_once(monkeypatch, capsys):
    monkeypatch.setenv(ae.ENFORCE_ENV, "false")
    assert ae.resolve(None) == ("trusted", "enforce-env")
    assert ae.resolve(None).mode == "trusted"
    assert capsys.readouterr().err.count("deprecated") == 1


def test_mode_env_beats_enforce_env(monkeypatch):
    monkeypatch.setenv(ae.ENFORCE_ENV, "false")
    monkeypatch.setenv(ae.MODE_ENV, "signed")
    assert ae.resolve(None).mode == "signed"


def test_shim_reads_host_key(monkeypatch):
    monkeypatch.setattr(ae, "_configured_mode", lambda: "trusted")
    assert ae.enforced() is False
    assert ae.status() == "disabled"
    assert any("TRUSTED" in w for w in ae.doctor_warnings())
    monkeypatch.setattr(ae, "_configured_mode", lambda: None)
    assert ae.enforced() is True


def test_operator_settings_key(monkeypatch, tmp_path):
    f = tmp_path / "op.yaml"
    f.write_text("hq:\n  authority_mode: trusted\n  sql: {}\n")
    monkeypatch.setenv("BH_HQ_OPERATOR_SETTINGS", str(f))
    assert ae.mode() == "trusted"


def test_key_is_host_only_and_validated():
    assert partition.partition_of("hq.authority_mode") == partition.HOST
    assert "hq.authority_mode" in partition.HOST_KEYS
    assert contracts.HqConfig().authority_mode == "inherit"
    with pytest.raises(ValueError):
        contracts.HqConfig(authority_mode="bogus")
