from __future__ import annotations

from beadhive import doctor, hive_ready
from beadhive.bootstrap import build_verify
from beadhive.kernel.plugins import (
    BUILD_VERIFY,
    DiagnosticCode,
    DiagnosticSeverity,
    PluginDiagnostic,
)
from beadhive.modules.config.contracts import AttestConfig


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


def test_absent_verifier_package_degrades_with_an_install_hint_not_a_crash(
    monkeypatch, tmp_path
) -> None:
    """``beadhive-pants`` is an optional extra (bh-mxjoy): a checkout that selects the ``pants``
    build.verify manifest but never installed the package must degrade to a warning diagnostic,
    not raise ``ModuleNotFoundError`` out of ``collect_build_verify_diagnostics``."""

    def missing(name: str):
        raise ModuleNotFoundError(f"No module named {name!r}", name="beadhive_pants")

    monkeypatch.setattr(build_verify, "import_module", missing)

    diagnostics = build_verify.collect_build_verify_diagnostics(str(tmp_path), AttestConfig())

    (diagnostic,) = diagnostics
    assert diagnostic.code is DiagnosticCode.BUILD_ATTEST_TAGS
    assert diagnostic.severity is DiagnosticSeverity.WARNING
    assert diagnostic.plugin_id == "pants"
    assert diagnostic.capability == BUILD_VERIFY
    assert "not installed" in diagnostic.detail
    assert "beadhive[pants]" in diagnostic.detail


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
