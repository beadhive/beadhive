"""The standalone ``beadlog`` CLI: merge, reconcile, inspect, verify and entry-point extract.

The end-to-end test runs the CLI in a fresh interpreter in which every ``beadhive*`` import is
blocked, proving the commands work with no BeadHive installed.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import zipfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

import beadlog
from beadlog_sources import ENTRY_POINT_GROUP, CommandResult, ExtractRequest, plan_entries
from beadlog_sources.cli import discover_strategies, main

GROUP = beadlog.Group("urn:uuid:00000000-0000-4000-8000-0000000000cc", "bh")
GENERATOR = beadlog.Generator("fixture", "0")
EXTRACTED = "2026-10-31T00:00:00Z"

NO_BEADHIVE = """\
import importlib.abc, sys

class _NoBeadhive(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".", 1)[0].startswith("beadhive"):
            raise ModuleNotFoundError(f"No module named {name!r} (blocked by the test)")
        return None

sys.meta_path.insert(0, _NoBeadhive())
from beadlog_sources.cli import main
code = main(sys.argv[1:])
leaked = sorted(m for m in sys.modules if m.split(".", 1)[0].startswith("beadhive"))
if leaked:
    print("imported", leaked, file=sys.stderr)
    code = 99
sys.exit(code)
"""


def _create(bead: str, title: str, system: str = "bd") -> beadlog.BeadEvent:
    return beadlog.mint_event(
        "create",
        beadlog.BeadRef(GROUP.id, bead),
        {"type": "task", "fields": {"title": title}},
        occurred_at="2026-07-01T00:00:00Z",
        recorded_at="2026-07-01T00:00:00Z" if system == "bd" else None,
        extracted_at=EXTRACTED,
        source=(system, f"{system}:{bead}"),
        provenance="observed" if system == "bd" else "inferred",
    )


def _archive(path: Path, events: Iterable[beadlog.BeadEvent]) -> Path:
    beadlog.write_archive(path, list(events), groups=[GROUP], generator=GENERATOR)
    return path


@pytest.fixture
def fixtures(tmp_path: Path) -> dict[str, Path]:
    baseline = [_create("bh-1", "Add the archive writer"), _create("bh-2", "Reader")]
    proposed = [
        _create("bh-1", "Add the archive writer", "docs"),  # PRESENT
        _create("doc-1", "Quarterly zebra census", "docs"),  # NEW, no candidate
        _create("doc-2", "Archive writer tests", "docs"),  # residual: judged
    ]
    return {
        "baseline": _archive(tmp_path / "baseline.bdlg", baseline),
        "proposed": _archive(tmp_path / "proposed.bdlg", proposed),
        "extra": _archive(tmp_path / "extra.bdlg", [_create("bh-3", "Index")]),
        "dir": tmp_path,
    }


def _cli(*argv: str, **options: Any) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    options.setdefault("environ", {})
    options.setdefault("entry_points", lambda: [])
    code = main(list(argv), stdout=out, stderr=err, **options)
    return code, out.getvalue(), err.getvalue()


def test_merge_unions_archives(fixtures: dict[str, Path]) -> None:
    out = fixtures["dir"] / "merged.bdlg"
    code, text, _ = _cli("merge", str(out), str(fixtures["baseline"]), str(fixtures["extra"]))
    assert code == 0 and "3 events" in text
    manifest, events = beadlog.read_archive(out)
    assert {event.bead.id for event in events} == {"bh-1", "bh-2", "bh-3"}
    assert manifest.generator.name == "beadlog-sources"
    # merging is idempotent: merging the result with an input changes nothing
    again = fixtures["dir"] / "again.bdlg"
    assert _cli("merge", str(again), str(out), str(fixtures["extra"]))[0] == 0
    assert again.read_bytes() == out.read_bytes()


def test_reconcile_writes_a_plan_and_inspect_and_verify_read_it(
    fixtures: dict[str, Path],
) -> None:
    plan = fixtures["dir"] / "plan.bdlg"
    code, text, _ = _cli(
        "reconcile", str(fixtures["proposed"]), str(fixtures["baseline"]), "-o", str(plan), "--json"
    )
    assert code == 0
    summary = json.loads(text)
    assert summary["counts"] == {"NEW": 2, "PRESENT": 1, "NOISE": 0, "DRIFT": 0}
    assert summary["judge_calls"] == 0 and summary["judge"]["engine"] == "deterministic-lexical"
    assert summary["review_required"] == 1  # the deterministic engine abstained on doc-2

    code, text, _ = _cli("inspect", str(plan), "--json")
    inspected = json.loads(text)
    assert code == 0 and inspected["plan"]["pass"] == "reconcile"
    assert inspected["reconcile"]["counts"] == summary["counts"]
    code, text, _ = _cli("inspect", str(plan))
    assert code == 0 and "plan: pass=reconcile" in text and "reconcile:" in text

    code, text, _ = _cli("verify", str(plan))
    assert code == 0 and text.startswith("ok:")


def test_reconcile_judge_flags_and_env_drive_the_command_judge(fixtures: dict[str, Path]) -> None:
    requests: list[dict[str, Any]] = []

    def runner(argv: Any, stdin: bytes, timeout: float) -> CommandResult:
        request = json.loads(stdin)
        requests.append({"argv": list(argv), "timeout": timeout, **request})
        answer = {
            "schema": "bh-judge/1",
            "type": "response",
            "outcome": "new",
            "probabilities": {"new": 1.0},
            "rationale": "different work",
            "engine": "fake",
            "engine_version": "1",
            "model": request["model"],
        }
        return CommandResult(0, json.dumps(answer).encode())

    plan = fixtures["dir"] / "judged.bdlg"
    code, text, _ = _cli(
        "reconcile",
        str(fixtures["proposed"]),
        str(fixtures["baseline"]),
        "-o",
        str(plan),
        "--json",
        "--judge-command",
        "my-judge --x",
        "--judge-auto-accept",
        "reconcile.match=0.9",
        environ={"BEADLOG_JUDGE_ENABLED": "1", "BEADLOG_JUDGE_TIMEOUT_SECONDS": "7"},
        runner=runner,
    )
    assert code == 0
    summary = json.loads(text)
    assert summary["judge_calls"] == 1 and summary["review_required"] == 0
    (request,) = requests
    assert request["argv"] == ["my-judge", "--x"] and request["timeout"] == 7.0
    assert request["model"] == "claude-haiku-5-5" and request["family"] == "reconcile.match"

    # --prior reuses the recorded verdict: no further judge call.
    again = fixtures["dir"] / "again.bdlg"
    code, text, _ = _cli(
        "reconcile",
        str(fixtures["proposed"]),
        str(fixtures["baseline"]),
        "-o",
        str(again),
        "--json",
        "--prior",
        str(plan),
        "--judge-enabled",
        "true",
        runner=runner,
    )
    assert code == 0 and json.loads(text)["judge_calls"] == 0 and len(requests) == 1
    manifest, _ = beadlog.read_archive(again)
    verdicts = [e.verdict for e in plan_entries(manifest) if e.verdict is not None]
    assert [v.judgment.engine for v in verdicts] == ["fake"]


def test_disabled_judge_spawns_nothing_through_the_cli(
    fixtures: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("no subprocess with the judge disabled")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    plan = fixtures["dir"] / "plan.bdlg"
    code, _, _ = _cli(
        "reconcile",
        str(fixtures["proposed"]),
        str(fixtures["baseline"]),
        "-o",
        str(plan),
        "--judge-command",
        "would-spawn",
    )
    assert code == 0


def test_verify_reports_a_tampered_archive(fixtures: dict[str, Path]) -> None:
    tampered = fixtures["dir"] / "tampered.bdlg"
    with zipfile.ZipFile(fixtures["baseline"]) as source, zipfile.ZipFile(tampered, "w") as target:
        for info in source.infolist():
            data = source.read(info)
            if info.filename.startswith("events/"):
                data = data.replace(b"Reader", b"Writer")
            target.writestr(info, data)
    code, _, err = _cli("verify", str(tampered))
    assert code == 1 and err.startswith("beadlog: ")


def test_usage_errors_exit_two(fixtures: dict[str, Path]) -> None:
    with pytest.raises(SystemExit) as raised:
        _cli("reconcile", str(fixtures["proposed"]))
    assert raised.value.code == 2
    with pytest.raises(SystemExit) as raised:
        _cli("extract", "nothing-registered")
    assert raised.value.code == 2


class _OneCommit:
    name = "one-commit"
    version = "1"

    def extract(self, request: ExtractRequest) -> Iterable[beadlog.BeadEvent]:
        yield beadlog.mint_event(
            "create",
            beadlog.BeadRef(request.group, "bh-9"),
            {"type": "task", "fields": {"title": f"from {request.options.get('ref', '?')}"}},
            occurred_at="2026-01-01T00:00:00Z",
            recorded_at=None,
            extracted_at=request.extracted_at,
            source=("git", "git:commit/abc"),
            provenance="derived",
        )


class _EntryPoint:
    def __init__(self, name: str, target: Any) -> None:
        self.name = name
        self.group = ENTRY_POINT_GROUP
        self._target = target

    def load(self) -> Any:
        return self._target


def test_extract_subcommands_come_from_the_entry_point_group(tmp_path: Path) -> None:
    points = [_EntryPoint("one-commit", _OneCommit), _EntryPoint("broken", object())]
    assert list(discover_strategies(lambda: points)) == ["broken", "one-commit"]
    out = tmp_path / "extracted.bdlg"
    code, text, _ = _cli(
        "extract",
        "one-commit",
        "--group",
        GROUP.id,
        "--group-dir",
        "bh",
        "--extracted-at",
        EXTRACTED,
        "--option",
        "ref=main",
        "-o",
        str(out),
        entry_points=lambda: points,
    )
    assert code == 0 and "1 events with one-commit" in text
    _, (event,) = beadlog.read_archive(out)
    assert event.data["fields"]["title"] == "from main"

    code, _, err = _cli(
        "extract",
        "broken",
        "--group",
        GROUP.id,
        "--group-dir",
        "bh",
        "--extracted-at",
        EXTRACTED,
        "-o",
        str(tmp_path / "x.bdlg"),
        entry_points=lambda: points,
    )
    assert code == 1 and "not a SourceStrategy" in err


def test_real_discovery_reads_the_installed_group() -> None:
    # No strategy is registered yet; discovery still runs against importlib.metadata.
    assert isinstance(discover_strategies(), dict)


def test_commands_work_with_no_beadhive_importable(
    fixtures: dict[str, Path], tmp_path: Path
) -> None:
    def run(*argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-I", "-c", NO_BEADHIVE, *argv],
            capture_output=True,
            text=True,
            check=False,
            cwd=tmp_path,
        )

    merged, plan = tmp_path / "merged.bdlg", tmp_path / "plan.bdlg"
    steps = [
        ("merge", str(merged), str(fixtures["baseline"]), str(fixtures["extra"])),
        ("reconcile", str(fixtures["proposed"]), str(merged), "-o", str(plan)),
        ("inspect", str(plan)),
        ("verify", str(plan)),
        ("verify", str(merged)),
    ]
    for step in steps:
        result = run(*step)
        assert result.returncode == 0, (step, result.stderr)
    assert run("--version").stdout.strip() == "beadlog 0.1.0"
