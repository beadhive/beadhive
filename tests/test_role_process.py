"""Qualified direct-role process ownership and live progress."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from beadhive import activity_publisher, role_process, run_journal

DIGEST = "sha256:" + "d" * 64


def _journal(
    tmp_path: Path, bead: str = "bh-direct", activity_publisher=None
) -> run_journal.RunJournal:
    return run_journal.RunJournal.create(
        run_journal.RunIdentity(
            hive="github/acme/core",
            bead=bead,
            driver="baml",
            provider="codex",
            manifest_digest=DIGEST,
        ),
        base=tmp_path,
        writer=run_journal.WRITER_BAML,
        activity_publisher=activity_publisher,
    )


def _records(journal: run_journal.RunJournal) -> list[dict]:
    return [json.loads(line) for line in journal.path.read_text().splitlines()]


def test_foreground_exposes_progress_and_preserves_one_final_seat_run(
    tmp_path: Path, capsys
) -> None:
    provider = "provider-direct"
    seat_process = "seat-direct"
    payload = json.dumps(
        {
            "outcome": {"status": "done", "summary": "ok", "bead_id": "bh-direct"},
            "session_id": provider,
            "cost_usd": 0.5,
            "usage": {"input_tokens": 4, "output_tokens": 2},
        }
    )
    code = (
        "import sys,time; "
        "print('provider-progress', file=sys.stderr, flush=True); "
        "time.sleep(0.05); "
        f"print({payload!r}, flush=True)"
    )
    journal = _journal(tmp_path)

    result = role_process.run_foreground(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=dict(os.environ),
        journal=journal,
        bead="bh-direct",
        role="developer",
        seat_process_id=seat_process,
        provider_continuation=provider,
    )

    captured = capsys.readouterr()
    assert result == 0
    assert captured.err.count("provider-progress") == 1
    assert len(captured.out.strip().splitlines()) == 1
    assert json.loads(captured.out)["session_id"] == provider
    records = _records(journal)
    assert [row["activity"]["kind"] for row in records] == [
        "run.created",
        "process.spawned",
        "process.harvested",
    ]
    assert {row["provider_continuation"] for row in records[1:]} == {provider}
    assert len({journal.run_id, seat_process, provider}) == 3


def test_real_baml_foreground_result_isolated_from_source_publisher_failure(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("BH_ACTIVITY_PUBLISH_BAML_TOKEN", "baml-child-token")
    monkeypatch.setenv("BH_ACTIVITY_PUBLISH_HITCH_TOKEN", "hitch-must-not-leak")
    monkeypatch.setenv("BH_ACTIVITY_PUBLISH_BEADHIVE_TOKEN", "beadhive-must-not-leak")
    publisher = activity_publisher.ActivityPublisher(
        activity_publisher.ActivityPublisherConfig(
            origin="http://127.0.0.1:8737",
            queue_path=tmp_path / "baml-outbox.sqlite3",
            tokens={"baml": "token"},
        ),
        transport=lambda *_args: 503,
        start_worker=False,
    )
    journal = _journal(tmp_path, activity_publisher=publisher)
    child_env = tmp_path / "baml-child-env.json"
    payload = json.dumps(
        {
            "outcome": {"status": "done", "summary": "ok", "bead_id": "bh-direct"},
            "session_id": "provider-isolated",
        }
    )

    result = role_process.run_foreground(
        [
            sys.executable,
            "-c",
            (
                "import json,os,pathlib; "
                f"pathlib.Path({str(child_env)!r}).write_text(json.dumps("
                "sorted(k for k in os.environ if k.startswith('BH_ACTIVITY_PUBLISH_') "
                "and k.endswith('_TOKEN')))); "
                f"print({payload!r}, flush=True)"
            ),
        ],
        cwd=tmp_path,
        env=dict(os.environ),
        journal=journal,
        bead="bh-direct",
        role="developer",
        seat_process_id="seat-isolated",
        provider_continuation="provider-isolated",
    )

    assert result == 0
    assert json.loads(child_env.read_text()) == ["BH_ACTIVITY_PUBLISH_BAML_TOKEN"]
    assert publisher.status().retained == 3
    publisher.flush_once(force=True)
    assert publisher.status().retried == 1


def test_caller_cancellation_reaps_direct_role_descendants(tmp_path: Path) -> None:
    pid_file = tmp_path / "descendant.pid"
    code = (
        "import pathlib,subprocess,sys,time; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid)); "
        "print('alive', file=sys.stderr, flush=True); time.sleep(60)"
    )
    journal = _journal(tmp_path, "bh-cancel")

    async def scenario() -> None:
        task = asyncio.create_task(
            role_process._run_foreground(
                [sys.executable, "-c", code],
                cwd=tmp_path,
                env=dict(os.environ),
                journal=journal,
                bead="bh-cancel",
                role="developer",
                seat_process_id="seat-cancel",
                provider_continuation="provider-cancel",
            )
        )
        for _ in range(200):
            if pid_file.exists():
                break
            await asyncio.sleep(0.01)
        assert pid_file.exists(), "fixture never spawned its descendant"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    descendant = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(descendant, 0)
    records = _records(journal)
    assert records[-1]["activity"]["kind"] == "process.cancelled"
    assert records[-1]["activity"]["process"]["group_gone"] is True
