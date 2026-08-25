"""Test helpers for the manifest-authoritative validation store."""

from __future__ import annotations

import datetime as dt
import json
import time
from pathlib import Path

from beadhive import private_paths, validation_ledger, validation_records


def pointer(entry, rev: str, cmd: str) -> Path:
    path = validation_ledger._verdict_path(
        entry, validation_ledger.tree_of(entry, rev), validation_ledger.cmd_hash(cmd)
    )
    assert path is not None
    return path


def latest(repo: Path, entry, rev: str, cmd: str) -> dict:
    run = validation_records.latest_run(
        repo,
        tree=validation_ledger.tree_of(entry, rev),
        command_hash=validation_ledger.cmd_hash(cmd),
    )
    assert run is not None
    return run


def manifest_path(repo: Path, run_id: str) -> Path:
    root = private_paths.git_private_root(repo)
    assert root is not None
    return root / "validation" / "runs" / run_id / "manifest.json"


def rewrite(repo: Path, run: dict) -> None:
    validation_records._atomic_json(manifest_path(repo, run["run_id"]), run)


def age(repo: Path, entry, rev: str, cmd: str, seconds: float) -> dict:
    tree = validation_ledger.tree_of(entry, rev)
    key = validation_ledger.cmd_hash(cmd)
    matching = validation_records.matching_runs(repo, tree=tree, command_hash=key)
    assert matching
    ordered = sorted(matching, key=lambda item: str(item.get("finished_at") or ""))
    for offset, run in enumerate(ordered):
        run["finished_at"] = dt.datetime.fromtimestamp(
            time.time() - seconds + offset * 0.001, dt.UTC
        ).isoformat()
        rewrite(repo, run)
    validation_ledger.rebuild_verdict_index(entry)
    return ordered[-1]


def runs(repo: Path) -> list[dict]:
    root = private_paths.git_private_root(repo)
    directory = root / "validation" / "runs" if root else None
    if directory is None or not directory.is_dir():
        return []
    values = []
    for child in directory.iterdir():
        path = child / "manifest.json"
        try:
            value = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(value, dict):
            values.append(value)
    return sorted(values, key=lambda item: str(item.get("finished_at") or ""))
