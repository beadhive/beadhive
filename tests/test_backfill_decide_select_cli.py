"""``bh backfill decide --select`` (bh-espkv, ADR ``backfill-merge-heavy-repos`` D6).

Same harness as :mod:`tests.test_backfill_decide_cli`: bd behind the real loader (FakeBd), a mocked
``decided_at`` clock, and a mocked collation age (the file's mtime is set and ``selection_now`` is
fixed), so nothing here depends on the wall clock.
"""
# ruff: noqa: F811 -- the imported fixtures are used by name

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from tests.test_backfill_bd import FakeBd
from tests.test_backfill_history_cli import (  # noqa: F401 — fixtures resolved by name
    ENTRY,
    TRIPLET,
    _config,
    _invoke,
    _row,
    _run,
    cfg_path,
    hive,
)

from beadhive import backfill_cli, backfill_create, backfill_select
from beadlog_sources import ProjectOptions
from beadlog_sources.collate import Collation, Unit

NOW = 1_800_000_000.0
LABELS = ("origin:backfill", *TRIPLET)


class Clock:
    def __init__(self) -> None:
        self.n = 0

    def __call__(self) -> str:
        self.n += 1
        return f"2026-11-13T12:{self.n // 60:02d}:{self.n % 60:02d}Z"


@pytest.fixture(autouse=True)
def _fixed(monkeypatch, hive):  # noqa: F811
    monkeypatch.setattr(backfill_cli, "decision_clock", Clock())
    monkeypatch.setattr(backfill_cli, "selection_now", lambda: NOW)
    monkeypatch.setattr(backfill_create, "creation_labels", lambda h, c: LABELS)
    monkeypatch.setattr(
        backfill_create,
        "create_mode_options",
        lambda h, c, m: ProjectOptions(creation_labels=LABELS),
    )
    return hive


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _merge(repo: Path, branch: str, message: str) -> None:
    _git(repo, "checkout", "-q", "-b", branch, "main")
    (repo / f"{branch}.txt").write_text(branch)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"feat(x): {branch} work")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "-m", message, branch)


def history(tmp_path: Path, repo: Path, *, merges: int = 2) -> dict[str, Any]:
    for n in range(merges):
        _merge(repo, f"topic{n}", f"Merge branch 'main' into topic{n}")
    _run(tmp_path, "--sources", "git", "--until", "collate")
    run = tmp_path / "run" / "collation.json"
    os.utime(run, (NOW - 7200, NOW - 7200))  # a fixed collation age: two hours
    return json.loads(run.read_text())


def decide(tmp_path: Path, *args: str, code: int = 0) -> Any:
    return _invoke("backfill", "decide", "--run-dir", str(tmp_path / "run"), *args, code=code)


def decisions(store: FakeBd) -> list[dict[str, Any]]:
    return [r for r in store.rows.values() if r.get("issue_type") == "decision"]


def noise_keys(doc: dict[str, Any]) -> list[str]:
    return sorted(u["key"] for u in doc["units"] if u.get("noise") == "branch-sync")


# -- the selector ------------------------------------------------------------------------------


def test_select_records_one_decision_per_match_with_the_selector_as_reason(hive, tmp_path) -> None:
    store, repo = hive
    keys = noise_keys(history(tmp_path, repo))
    assert len(keys) == 2
    out = decide(tmp_path, "--select", "noise=branch-sync", "--reason", "sync", "skip")
    assert "collation: " in out.stdout and "(age 2h)" in out.stdout
    rows = decisions(store)
    assert sorted(r["metadata"]["backfill.unit"] for r in rows) == keys
    for row in rows:
        assert row["status"] == "closed"
        assert row["metadata"]["backfill.reason"] == "select: noise=branch-sync; sync"
        assert row["metadata"]["backfill.decision"] == "skip"
    view = _invoke("backfill", "view", "decisions").stdout
    assert "select: noise=branch-sync" in view


def test_clauses_are_anded_and_regex_titles_work(hive, tmp_path) -> None:
    store, repo = hive
    doc = history(tmp_path, repo)
    merges = sorted(u["key"] for u in doc["units"] if u["rule"] == "merge")
    assert merges
    decide(tmp_path, "--select", "rule=merge", "--select", "title~'^Merge'", "skip")
    assert sorted(r["metadata"]["backfill.unit"] for r in decisions(store)) == merges
    decide(tmp_path, "--select", "rule=commit", "--select", "title~^zzz-no-such", "skip")
    assert len(decisions(store)) == len(merges)


def test_equal_inputs_select_the_same_sorted_set_and_zero_matches_write_nothing(
    hive, tmp_path
) -> None:
    store, repo = hive
    keys = noise_keys(history(tmp_path, repo))
    first = json.loads(
        decide(tmp_path, "--select", "noise=branch-sync", "--dry-run", "--json", "skip").stdout
    )
    second = json.loads(
        decide(tmp_path, "--select", "noise=branch-sync", "--dry-run", "--json", "skip").stdout
    )
    assert (
        [d["unit"] for d in first["decisions"]] == keys == [d["unit"] for d in second["decisions"]]
    )
    out = decide(tmp_path, "--select", "title~^zzz-no-such", "skip")
    assert "0 unit(s) matched" in out.stdout
    assert store.writes == [] and decisions(store) == []


def test_dry_run_lists_ids_and_fields_and_writes_nothing(hive, tmp_path) -> None:
    store, repo = hive
    keys = noise_keys(history(tmp_path, repo))
    before = dict(store.rows)
    out = decide(tmp_path, "--select", "noise=branch-sync", "--dry-run", "skip")
    assert "would record skip" in out.stdout and "dry run: nothing written" in out.stdout
    assert all(k in out.stdout for k in keys)
    payload = json.loads(
        decide(tmp_path, "--select", "noise=branch-sync", "--dry-run", "--json", "skip").stdout
    )
    assert payload["dry_run"] is True and payload["collation"]["age_seconds"] == 7200
    assert payload["decisions"][0]["bead"].startswith("ag-infra-")
    assert payload["decisions"][0]["fields"]["metadata.backfill.reason"].startswith("select: ")
    assert store.writes == [] and store.rows == before


def test_usage_errors(hive, tmp_path) -> None:
    store, repo = hive
    history(tmp_path, repo)
    bad = {
        "colour=red": "unknown --select field",
        "rule": "needs <field><op><value>",
        "rule=nope": "is not one of",
        "title~(": "not a regular expression",
        f"title~{'a' * 201}": "at most 200 characters",
        "type=feat": 'collate.merge_types=["*"]',
    }
    for clause, text in bad.items():
        result = decide(tmp_path, "--select", clause, "skip", code=1)
        assert text in result.output, clause
    assert (
        "replaces the unit key"
        in decide(tmp_path, "git:commit/x", "skip", "--select", "rule=merge", code=1).output
    )
    assert store.writes == [] and decisions(store) == []


def test_missing_collation_names_collate(hive, tmp_path) -> None:
    result = decide(tmp_path, "--select", "rule=merge", "skip", code=1)
    assert "bh backfill collate" in result.output


def test_type_selects_when_merge_types_is_set(hive, tmp_path, cfg_path) -> None:
    store, repo = hive
    _config(cfg_path, collate={"merge_types": ["*"]})
    doc = history(tmp_path, repo)
    typed = sorted(u["key"] for u in doc["units"] if u.get("type") == "feat")
    assert typed
    decide(tmp_path, "--select", "type=feat", "skip")
    assert sorted(r["metadata"]["backfill.unit"] for r in decisions(store)) == typed


def test_a_decision_on_a_noise_unit_is_recorded(hive, tmp_path) -> None:
    store, repo = hive
    keys = noise_keys(history(tmp_path, repo))
    decide(
        tmp_path,
        "--select",
        "noise=branch-sync",
        "--select",
        f"key={keys[0]}",
        "link",
        "ag-infra-1",
    )
    (row,) = decisions(store)
    assert row["metadata"]["backfill.target"] == "ag-infra-1"


# -- the cap -----------------------------------------------------------------------------------


def test_more_matches_than_the_cap_refuse_without_yes(hive, tmp_path) -> None:
    store, repo = hive
    history(tmp_path, repo, merges=3)
    refused = decide(tmp_path, "--select", "rule=merge", "--max", "2", "skip", code=1)
    assert "more than the 2 allowed" in refused.output
    assert store.writes == [] and decisions(store) == []
    preview = decide(tmp_path, "--select", "rule=merge", "--max", "2", "--dry-run", "skip")
    assert "more than 2 matches" in preview.stdout and store.writes == []
    decide(tmp_path, "--select", "rule=merge", "--max", "2", "--yes", "skip")
    assert len(decisions(store)) == 3


def test_select_max_follows_config_and_the_hive_overlay(
    hive, tmp_path, cfg_path, monkeypatch
) -> None:
    store, repo = hive
    history(tmp_path, repo, merges=3)
    _config(cfg_path, decisions={"select_max": 2})
    assert (
        "more than the 2 allowed"
        in decide(tmp_path, "--select", "rule=merge", "skip", code=1).output
    )
    _config(
        cfg_path,
        decisions={"select_max": 2},
        hives={"ag-infra": {"decisions": {"select_max": 50}}},
    )
    real = backfill_cli.effective_cfg  # the harness hive is not in the registry: pass its entry
    monkeypatch.setattr(backfill_cli, "effective_cfg", lambda cfg, hive: real(cfg, ENTRY))
    decide(tmp_path, "--select", "rule=merge", "skip")
    assert len(decisions(store)) == 3


# -- one export, one load ----------------------------------------------------------------------


def test_n_units_cost_one_export_and_one_load(hive, tmp_path, monkeypatch) -> None:
    store, repo = hive
    history(tmp_path, repo, merges=3)
    real = backfill_cli.store_loader
    calls: list[str] = []

    class Counting:
        def __init__(self, inner: Any) -> None:
            self.inner = inner

        def export(self) -> Any:
            calls.append("export")
            return self.inner.export()

        def load(self, *a: Any, **k: Any) -> Any:
            calls.append("load")
            return self.inner.load(*a, **k)

        def __getattr__(self, name: str) -> Any:
            return getattr(self.inner, name)

    monkeypatch.setattr(backfill_cli, "store_loader", lambda *a, **k: Counting(real(*a, **k)))
    decide(tmp_path, "--select", "rule=merge", "skip")
    assert len(decisions(store)) == 3
    assert calls == ["export", "load"]


# -- guard rails -------------------------------------------------------------------------------


def _collation() -> Collation:
    units = (
        Unit(key="bead:ag-infra-1", kind="attach", rule="local-bead", title="t", bead="ag-infra-1"),
        Unit(key="git:commit/aa", kind="issue", rule="commit", title="has a bead"),
        Unit(key="git:commit/bb", kind="issue", rule="commit", title="free"),
    )
    return Collation(local="l", corpus="c", baseline="b", units=units)


def test_attach_units_and_units_with_a_live_bead_are_excluded() -> None:
    found = backfill_select.select_units(
        _collation(),
        backfill_select.parse_clauses(["title~."]),
        live_refs={"git:commit/aa"},
    )
    assert found.matched == ("git:commit/bb",)
    assert dict(found.excluded) == {
        "bead:ag-infra-1": "attach unit (its identity is an existing bead)",
        "git:commit/aa": "already has a live bead",
    }


def test_a_live_bead_by_external_ref_is_excluded_in_the_cli(hive, tmp_path) -> None:
    store, repo = hive
    keys = noise_keys(history(tmp_path, repo))
    store.rows["ag-infra-9"] = _row("ag-infra-9", external_ref=keys[0])
    out = json.loads(decide(tmp_path, "--select", "noise=branch-sync", "--json", "skip").stdout)
    assert [d["unit"] for d in out["decisions"]] == keys[1:]
    assert out["excluded"] == [{"unit": keys[0], "reason": "already has a live bead"}]
    assert [r["metadata"]["backfill.unit"] for r in decisions(store)] == keys[1:]
