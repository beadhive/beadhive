from __future__ import annotations

from beadhive import doctor, hive_ready
from beadhive.kernel.plugins import (
    BUILD_VERIFY,
    DiagnosticCode,
    DiagnosticSeverity,
    PluginDiagnostic,
)


def _diagnostics():
    return (
        PluginDiagnostic(
            DiagnosticCode.BUILD_OWNERSHIP,
            DiagnosticSeverity.ERROR,
            "FAILED: unowned.py",
            plugin_id="pants",
            capability=BUILD_VERIFY,
        ),
        PluginDiagnostic(
            DiagnosticCode.BUILD_PROVEN_MANIFEST,
            DiagnosticSeverity.INFO,
            "OK: manifest current",
            plugin_id="pants",
            capability=BUILD_VERIFY,
        ),
    )


def test_hive_ready_lists_named_build_verify_results(tmp_path, monkeypatch) -> None:
    (tmp_path / "pants.toml").write_text("")
    monkeypatch.setattr(
        hive_ready, "collect_build_verify_diagnostics", lambda *_a, **_k: _diagnostics()
    )

    checks = hive_ready._build_verify_checks({}, None, tmp_path)

    assert [(check.label, check.state) for check in checks] == [
        ("pants build-ownership", "missing"),
        ("pants build-proven-manifest", "ok"),
    ]


def test_doctor_lists_named_build_verify_results(tmp_path, monkeypatch, capsys) -> None:
    (tmp_path / "pants.toml").write_text("")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(doctor.registry, "current_hive", lambda _cfg: None)
    monkeypatch.setattr(
        doctor, "collect_build_verify_diagnostics", lambda *_a, **_k: _diagnostics()
    )

    data = doctor._data_build_verify({})
    doctor._render_build_verify(data)

    output = capsys.readouterr().out
    assert "pants build-ownership: FAILED: unowned.py" in output
    assert "pants build-proven-manifest: OK: manifest current" in output
