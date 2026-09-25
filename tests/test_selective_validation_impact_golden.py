"""Golden receipts: moving the Pants backend behind ``build.impact`` is behavior-neutral.

Each scenario drives :func:`beadhive.selective_validation.run` over a recorded change set in a
real git repository and captures the receipt the configured resolver produced plus the fallback
warning written to stderr. The golden file was recorded from the pre-plugin wiring, in which
``selective_validation`` constructed ``PantsImpactBackend`` itself (bh-3fcl0.2). The same
harness must reproduce it byte for byte now that bootstrap collects the backend from the
built-in ``pants`` manifest's ``build.impact`` capability.

The Pants engine is replaced by a recorded ``peek`` answer; git, the proven-test manifest,
``pants.toml``, and every fail-closed rule are real. Regenerate only for an intentional change:
``BH_REGEN_IMPACT_GOLDEN=1 uv run pytest tests/test_selective_validation_impact_golden.py``.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from beadhive import selective_validation
from beadhive.modules.config.contracts import AttestConfig
from beadhive_pants import impact as impact_pants

GOLDEN = Path(__file__).parent / "fixtures" / "impact-bootstrap" / "selective-validation.json"
PANTS_BACKEND = impact_pants.PantsImpactBackend

KEYS = [
    {"name": "guide", "cmd": "just attest-guide", "selectors": {"pants": "attest:guide"}},
    {"name": "unit", "cmd": "just attest-unit", "selectors": {"pants": "attest:unit"}},
    {
        "name": "stateful",
        "cmd": "just attest-stateful",
        "selectors": {"pants": "attest:stateful"},
    },
    {"name": "demos", "cmd": "just attest-demos", "selectors": {"pants": "attest:demos"}},
]

PROVEN = {
    "schema_version": 1,
    "inventory": "golden",
    "planning_baseline": 1,
    "tests": {"tests/test_unit.py": {"status": "proven", "dependencies": ["manual:docs"]}},
}


def _target(address, *, sources=(), tags=(), target_type="files"):
    return {
        "address": address,
        "sources": list(sources),
        "tags": list(tags),
        "target_type": target_type,
    }


GRAPH = [
    _target("manual:docs", sources=("manual/guide.md",), tags=("category:docs", "attest:guide")),
    _target(
        "tests:test_unit.py",
        sources=("tests/test_unit.py",),
        tags=("category:test-only", "attest:unit"),
        target_type="python_test",
    ),
    _target(
        "tests:stateful-fixtures",
        sources=("tests/stateful_fixtures.py",),
        tags=("category:test-only", "attest:stateful"),
    ),
    _target("src:lib", sources=("src/lib.py",), tags=("category:code", "attest:demos")),
]

BASE_FILES = {
    "manual/guide.md": "# guide\n",
    "tests/test_unit.py": "def test_unit():\n    pass\n",
    "tests/stateful_fixtures.py": "FIXTURE = 1\n",
    "src/lib.py": "VALUE = 1\n",
    "scripts/pants_proven_tests.json": json.dumps(PROVEN, indent=2, sort_keys=True) + "\n",
}
PANTS_TOML = '[GLOBAL]\npants_version = "2.32.1"\n'

#: name -> (configured backend, pants.toml present, head edits, affected addresses, peek fails)
SCENARIOS = {
    "pants-docs-only": ("pants", True, {"manual/guide.md": "# guide v2\n"}, ["manual:docs"], False),
    "pants-source-transitive": (
        "pants",
        True,
        {"src/lib.py": "VALUE = 2\n"},
        ["src:lib", "tests:test_unit.py"],
        False,
    ),
    "pants-unowned-path": ("pants", True, {"notes.txt": "loose\n"}, [], False),
    "pants-global-input": ("pants", True, {"BUILD": "files()\n"}, [], False),
    "pants-peek-failure": ("pants", True, {"manual/guide.md": "# guide v2\n"}, [], True),
    "pants-without-pants-toml": ("pants", False, {"src/lib.py": "VALUE = 2\n"}, [], False),
    "unknown-backend": ("bazel", True, {"src/lib.py": "VALUE = 2\n"}, [], False),
    "native-full": ("native-full", True, {"src/lib.py": "VALUE = 2\n"}, [], False),
}


def _git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
        "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
    }
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=golden",
            "-c",
            "user.email=golden@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _write(repo: Path, files: dict[str, str]) -> None:
    for relative, text in files.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _change_set(repo: Path, *, with_pants_toml: bool, edits: dict[str, str]) -> tuple[str, str]:
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _write(repo, {**BASE_FILES, **({"pants.toml": PANTS_TOML} if with_pants_toml else {})})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    _write(repo, edits)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "head")
    return base, _git(repo, "rev-parse", "HEAD")


def _run_scenario(name: str, tmp_path: Path, monkeypatch, capsys) -> dict[str, object]:
    backend, with_pants_toml, edits, affected, fails = SCENARIOS[name]
    repo = tmp_path / name
    base, head = _change_set(repo, with_pants_toml=with_pants_toml, edits=edits)

    def query(_repo, args, _timeout):
        if fails:
            raise RuntimeError("engine unavailable")
        if tuple(args) == ("peek", "::"):
            return [dict(target) for target in GRAPH]
        return [dict(target) for target in GRAPH if target["address"] in affected]

    # Replace only the Pants engine. The pre-plugin wiring bound the class into
    # selective_validation at import time; the plugin wiring resolves it from the adapter.
    def recorded(repo_path):
        return PANTS_BACKEND(
            repo_path,
            manifest=Path(repo_path) / "scripts/pants_proven_tests.json",
            query=query,
            sleeper=lambda _seconds: None,
        )

    monkeypatch.setattr(impact_pants, "PantsImpactBackend", recorded)
    if hasattr(selective_validation, "PantsImpactBackend"):
        monkeypatch.setattr(selective_validation, "PantsImpactBackend", recorded)

    attest = AttestConfig.model_validate({"keys": KEYS, "impact": {"backend": backend}})
    monkeypatch.setattr(selective_validation.config, "attest_config", lambda *_args: attest)
    ledger = selective_validation.validation_ledger
    monkeypatch.setattr(ledger, "carry_key_verdict", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        ledger,
        "key_verdict",
        lambda _entry, rev, key, cfg=None: ledger.KeyVerdict(  # noqa: ARG005
            key.name, rev, "cmd-hash", ledger.KeyVerdictState.ABSENT
        ),
    )

    receipts = []
    bind = selective_validation.impact_resolver

    class Recording:
        def __init__(self, resolver):
            self._resolver = resolver

        def resolve(self, *args):
            receipt = self._resolver.resolve(*args)
            receipts.append(receipt)
            return receipt

    monkeypatch.setattr(
        selective_validation,
        "impact_resolver",
        lambda *args, **kwargs: Recording(bind(*args, **kwargs)),
    )
    capsys.readouterr()
    ran: list[str] = []
    rc = selective_validation.run(
        {},
        {},
        base_rev=base,
        head_rev=head,
        repo_path=str(repo),
        runner=lambda cmd: ran.append(cmd) or 0,
    )
    stderr = capsys.readouterr().err
    (receipt,) = receipts
    content = receipt.to_dict()
    content.pop("elapsed_ms")
    return {"exit": rc, "ran": ran, "receipt": content, "stderr": stderr}


def _canonical(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def test_selective_validation_receipts_match_the_pre_plugin_golden(tmp_path, monkeypatch, capsys):
    observed = {
        name: _run_scenario(name, tmp_path, monkeypatch, capsys) for name in sorted(SCENARIOS)
    }
    if os.environ.get("BH_REGEN_IMPACT_GOLDEN") == "1":
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(_canonical(observed), encoding="utf-8")
    assert _canonical(observed) == GOLDEN.read_text(encoding="utf-8")


def test_golden_pins_resolver_choice_and_fallback_reasons():
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    reasons = {name: row["receipt"]["fallback_reason"] for name, row in golden.items()}
    backends = {name: row["receipt"]["backend"] for name, row in golden.items()}

    assert reasons["unknown-backend"] == "bazel: backend not available"
    assert reasons["pants-without-pants-toml"] == "pants: backend not available"
    assert reasons["pants-peek-failure"] == "pants: error: RuntimeError: engine unavailable"
    assert backends["pants-docs-only"] == backends["pants-source-transitive"] == "pants"
    assert golden["pants-docs-only"]["receipt"]["invalidated_keys"] == ["guide"]
    assert backends["native-full"] == "native-full" and reasons["native-full"] == ""


@pytest.mark.parametrize("name", ["unknown-backend", "pants-without-pants-toml"])
def test_unavailable_backend_warning_text_is_unchanged(name):
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert "!!! WARNING: IMPACT RESOLUTION FALLBACK !!!" in golden[name]["stderr"]
    assert f"    {golden[name]['receipt']['fallback_reason']}\n" in golden[name]["stderr"]
