"""Unit tests for scripts/dolt_globals_watchdog.py (bh-ijxif): injected runner, no Dolt."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "dolt_globals_watchdog", ROOT / "scripts" / "dolt_globals_watchdog.py"
)
assert _SPEC is not None and _SPEC.loader is not None
W = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = W
_SPEC.loader.exec_module(W)


def fake(values: dict[str, str], *, persist_ok: bool = False):
    """A runner serving ``SELECT @@GLOBAL.<n>`` from *values*; records every statement."""
    calls: list[str] = []

    def run(argv, timeout):
        sql = argv[-1]
        calls.append(sql)
        if sql.startswith("SELECT @@GLOBAL."):
            name = sql.split("@@GLOBAL.")[1].split()[0]
            if name not in values:
                return 1, "", f"Unknown system variable '@@global.{name}'"
            return 0, json.dumps({"rows": [{"v": values[name]}]}), ""
        if sql.startswith("SET PERSIST"):
            return (0, "", "") if persist_ok else (1, "", "permission denied")
        return 1, "", "unexpected"

    run.calls = calls  # type: ignore[attr-defined]
    return run


GOOD = {
    "dolt_force_transaction_commit": "0",
    "dolt_allow_commit_conflicts": "0",
    "dolt_transaction_commit": "0",
    "read_only": "0",
    "max_connections": "151",
}


def test_default_watched_list_is_the_beads_list():
    assert set(W.DEFAULT_WATCHED) == {
        "dolt_force_transaction_commit",
        "dolt_allow_commit_conflicts",
        "dolt_transaction_commit",
        "read_only",
    }


def test_all_match_exits_zero():
    run = fake(GOOD)
    results = W.check_globals(["dolt"], W.resolve_watched(), run)
    assert W.exit_code(results) == 0
    assert all(c.startswith("SELECT") for c in run.calls)


def test_drift_is_loud_and_never_fixed():
    run = fake({**GOOD, "dolt_force_transaction_commit": "1"})
    results = W.check_globals(["dolt"], W.resolve_watched(), run)
    assert W.exit_code(results) == 2
    text = W.render(results, "h:1")
    assert "ALERT" in text and "NOT auto-fixed" in text
    assert not any(c.upper().startswith("SET") for c in run.calls)


def test_boolean_spellings_normalize():
    run = fake({**GOOD, "read_only": "OFF", "dolt_transaction_commit": "false"})
    assert W.exit_code(W.check_globals(["dolt"], W.resolve_watched(), run)) == 0


def test_unreadable_variable_is_unverifiable_not_skipped():
    values = {k: v for k, v in GOOD.items() if k != "dolt_allow_commit_conflicts"}
    results = W.check_globals(["dolt"], W.resolve_watched(), fake(values))
    assert W.exit_code(results) == 3
    assert "ALERT" in W.render(results, "h:1")


def test_drift_outranks_unverifiable():
    values = {**GOOD, "read_only": "1"}
    del values["dolt_allow_commit_conflicts"]
    assert W.exit_code(W.check_globals(["dolt"], W.resolve_watched(), fake(values))) == 2


def test_watched_list_is_config_driven():
    cfg = {"expect": {"max_connections": "151", "read_only": "1"}}
    assert W.resolve_watched(config=cfg) == {"max_connections": "151", "read_only": "1"}
    # M13-style tweak is pure policy: drop one, add one, no code change.
    tweaked = W.resolve_watched(
        drop=["dolt_allow_commit_conflicts"], expect=["max_connections=151"]
    )
    assert "dolt_allow_commit_conflicts" not in tweaked
    assert tweaked["max_connections"] == "151"
    assert W.resolve_watched(use_defaults=False) == {}
    with pytest.raises(ValueError):
        W.resolve_watched(expect=["nonsense"])
    with pytest.raises(ValueError):
        W.resolve_watched(config={"nope": {}})


def test_shipped_watched_globals_json_matches_defaults():
    shipped = json.loads((ROOT / "deploy" / "dolt" / "watched-globals.json").read_text())
    assert W.resolve_watched(config=shipped) == W.DEFAULT_WATCHED


def test_odd_variable_name_is_refused_before_any_query():
    run = fake(GOOD)
    results = W.check_globals(["dolt"], {"x; DROP": "0"}, run)
    assert results[0].status == W.UNVERIFIABLE and not run.calls


def test_persist_refused_passes_and_accepted_alerts():
    assert W.persist_probe(["dolt"], fake(GOOD))[0] == 0
    code, msg = W.persist_probe(["dolt"], fake(GOOD, persist_ok=True))
    assert code == 2 and "SUCCEEDED" in msg


def test_root_check(tmp_path):
    dot = tmp_path / ".dolt"
    dot.mkdir()
    (dot / "config_global.json").write_text("{}")
    if os.geteuid() == 0:
        pytest.skip("root bypasses file modes")
    assert W.root_check(tmp_path)[0] == 2  # writable
    (dot / "config_global.json").chmod(0o444)
    dot.chmod(0o555)
    try:
        assert W.root_check(tmp_path)[0] == 0
    finally:
        dot.chmod(0o755)
    assert W.root_check(tmp_path / "missing")[0] == 3


def test_config_templates_have_tls_and_no_cohost_note():
    for name in ("hq-server.yaml.example", "hive-server.yaml.example"):
        text = (ROOT / "deploy" / "dolt" / name).read_text()
        assert "require_secure_transport: true" in text and "DOLT_ROOT_PATH" in text
    assert "NEVER co-host" in (ROOT / "deploy/dolt/hq-server.yaml.example").read_text()


def test_upstream_drafts_are_marked_internal():
    for name in ("bd-writer-fencing-issues.md", "dolt-writer-fencing-issues.md"):
        text = (ROOT / "docs" / "upstream" / name).read_text()
        assert "tracked internally, not filed upstream" in text.lower()
