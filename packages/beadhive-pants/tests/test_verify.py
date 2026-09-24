from __future__ import annotations

import json
from pathlib import Path

from beadhive.kernel.plugins.contracts import DiagnosticCode, DiagnosticSeverity
from beadhive_pants import verify


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_docs.py").write_text('DOC = "docs/guide.md"\n')
    canonical = tmp_path / "packages/beadhive-pants/src/beadhive_pants/data"
    canonical.mkdir(parents=True)
    payload = {
        "schema_version": 1,
        "tests": {
            "tests/test_docs.py": {
                "status": "proven",
                "partition": "pants",
                "dependencies": ["docs:docs"],
            }
        },
    }
    (canonical / "proven_tests.json").write_text(json.dumps(payload))
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/pants_proven_tests.json").write_text(json.dumps(payload))
    return tmp_path


def test_unowned_file_produces_named_diagnostic(tmp_path, monkeypatch) -> None:
    root = _repo(tmp_path)
    monkeypatch.setattr(verify, "tracked_files", lambda _root: {"owned.py", "unowned.py"})

    diagnostic = verify.verify_ownership(root, query=lambda *_: (0, "owned.py\n", ""))

    assert diagnostic.code is DiagnosticCode.BUILD_OWNERSHIP
    assert diagnostic.severity is DiagnosticSeverity.ERROR
    assert "unowned.py" in diagnostic.detail


def test_drifted_proven_manifest_produces_named_diagnostic(tmp_path) -> None:
    root = _repo(tmp_path)
    (root / "scripts/pants_proven_tests.json").write_text("{}")

    diagnostic = verify.verify_proven_manifest(root)

    assert diagnostic.code is DiagnosticCode.BUILD_PROVEN_MANIFEST
    assert diagnostic.severity is DiagnosticSeverity.ERROR
    assert "compatibility mirror differs" in diagnostic.detail


def test_configured_selector_without_a_target_is_named(tmp_path) -> None:
    root = _repo(tmp_path)

    def query(*_):
        return 0, json.dumps([{"tags": ["attest:unit"]}]), ""

    diagnostic = verify.verify_attest_tags(
        root, {"unit": "attest:unit", "docs": "attest:docs"}, query=query
    )

    assert diagnostic.code is DiagnosticCode.BUILD_ATTEST_TAGS
    assert diagnostic.severity is DiagnosticSeverity.ERROR
    assert "docs=attest:docs" in diagnostic.detail


def test_verifier_uses_only_filedeps_and_peek_queries(tmp_path, monkeypatch) -> None:
    root = _repo(tmp_path)
    monkeypatch.setattr(verify, "tracked_files", lambda _root: {"owned.py"})
    calls: list[tuple[str, ...]] = []

    def query(_root, args):
        calls.append(tuple(args))
        if tuple(args) == ("filedeps", "::"):
            return 0, "owned.py\n", ""
        return 0, json.dumps([{"tags": ["attest:unit"]}]), ""

    diagnostics = verify.PantsBuildVerifier({"unit": "attest:unit"}, query=query).verify(str(root))

    assert [item.severity for item in diagnostics] == [
        DiagnosticSeverity.INFO,
        DiagnosticSeverity.INFO,
        DiagnosticSeverity.INFO,
    ]
    assert calls == [("filedeps", "::"), ("peek", "::")]
