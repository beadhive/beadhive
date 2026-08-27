"""Executable acceptance for LocalLoop's run-journal lifecycle (bh-e8s3i.2)."""

from __future__ import annotations

import asyncio
import json
import stat
import sys
from pathlib import Path

import pytest

from beadhive import localloop, run_journal, seatrun
from harness.processes import process_context

SCHEMA = Path(__file__).resolve().parents[1] / "docs/schemas/run-journal-v1.schema.json"
DIGEST = "sha256:" + "a" * 64


def _identity(bead: str = "bh-example.2") -> run_journal.RunIdentity:
    return run_journal.RunIdentity(
        hive="github/beadhive/beadhive",
        bead=bead,
        driver="baml",
        provider="claude-code",
        manifest_digest=DIGEST,
    )


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _append_many(identity, run_id: str, path: str, worker: int, count: int) -> None:
    journal = run_journal.RunJournal(identity, run_id, Path(path))
    for index in range(count):
        assert journal.append(
            {
                "kind": "worker.observed",
                "phase": "observed",
                "outcome_code": f"worker-{worker}-{index}",
            }
        )


def test_identity_is_strict_and_child_environment_rejects_conflicts(tmp_path: Path) -> None:
    journal = run_journal.RunJournal.create(_identity(), base=tmp_path)
    env = journal.child_env({"PATH": "/bin"})

    assert env["BH_RUN_ID"] == journal.run_id
    assert env["BH_RUN_HIVE"] == "github/beadhive/beadhive"
    assert env["BH_RUN_BEAD"] == "bh-example.2"
    assert env["BH_RUN_DRIVER"] == "baml"
    assert env["BH_RUN_PROVIDER"] == "claude-code"
    assert env["BH_RUN_MANIFEST_DIGEST"] == DIGEST
    assert Path(env["BH_RUN_JOURNAL_PATH"]).is_absolute()

    with pytest.raises(run_journal.RunContextConflict, match="BH_RUN_ID"):
        journal.child_env({"BH_RUN_ID": "some-other-attempt"})
    with pytest.raises(ValueError, match="manifest_digest"):
        run_journal.RunIdentity("h", "b", "baml", "claude-code", "sha256:not-a-digest")


def test_writer_rejects_content_fields_instead_of_serializing_then_redacting(
    tmp_path: Path,
) -> None:
    journal = run_journal.RunJournal.create(_identity(), base=tmp_path)
    before = journal.path.read_bytes()

    assert journal.append({"kind": "provider.observed", "prompt": "secret task"}) is False
    assert journal.path.read_bytes() == before
    assert journal.status.coverage == "degraded"


def test_writer_provenance_can_be_bound_by_each_direct_observer(tmp_path: Path) -> None:
    outer = run_journal.RunJournal.create(_identity(), base=tmp_path)
    role = run_journal.RunJournal.from_env(outer.child_env({}), writer=run_journal.WRITER_ROLE)
    assert role.append({"kind": "process.spawned", "process": {"pid": 123}})
    records = _records(role.path)
    assert [record["writer"] for record in records] == [
        "beadhive.local-loop",
        "beadhive.role",
    ]
    assert len({record["run_id"] for record in records}) == 1

    before = role.path.read_bytes()
    assert role.append({"kind": "process.observed"}, writer="consumer.guess") is False
    assert role.path.read_bytes() == before


def test_create_mints_private_distinct_attempts_before_spawn(tmp_path: Path) -> None:
    first = run_journal.RunJournal.create(_identity(), base=tmp_path)
    retry = run_journal.RunJournal.create(_identity(), base=tmp_path)

    assert first.run_id != retry.run_id
    assert _records(first.path)[0]["activity"] == {"kind": "run.created", "phase": "planned"}
    assert stat.S_IMODE(first.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(first.path.parent.stat().st_mode) == 0o700


def test_provider_continuation_binds_once_and_never_aliases_outer(tmp_path: Path) -> None:
    journal = run_journal.RunJournal.create(_identity(), base=tmp_path, run_id="outer-attempt")

    journal.bind_provider_continuation("provider-continuation")
    journal.bind_provider_continuation("provider-continuation")

    with pytest.raises(run_journal.ProviderContinuationConflict, match="cannot be rebound"):
        journal.bind_provider_continuation("provider-other")
    with pytest.raises(run_journal.ProviderContinuationConflict, match="cannot alias"):
        run_journal.RunJournal.create(
            _identity(), base=tmp_path, run_id="outer-other"
        ).bind_provider_continuation("outer-other")


def test_concurrent_appenders_leave_complete_schema_valid_lines(tmp_path: Path) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    identity = _identity()
    journal = run_journal.RunJournal.create(identity, base=tmp_path)
    ctx = process_context()
    workers = [
        ctx.Process(target=_append_many, args=(identity, journal.run_id, str(journal.path), n, 40))
        for n in range(6)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(10)
        assert worker.exitcode == 0

    records = _records(journal.path)
    assert len(records) == 1 + 6 * 40
    revisions = [record["source_revision"] for record in records]
    assert len(revisions) == len(set(revisions))
    validator = jsonschema.Draft202012Validator(json.loads(SCHEMA.read_text()))
    for record in records:
        validator.validate(record)


def test_spawn_cancel_and_harvest_keep_one_exact_outer_identity(tmp_path: Path) -> None:
    jsonschema = pytest.importorskip("jsonschema")

    async def scenario() -> tuple[list[dict], list[dict]]:
        cancel_journal = run_journal.RunJournal.create(_identity("bh-cancel"), base=tmp_path)
        cancel_seat = await localloop.spawn_seat(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            bead_id="bh-cancel",
            role="developer",
            action="dispatch",
            session_id="seat-process-cancel",
            provider_continuation="provider-session-cancel",
            journal=cancel_journal,
        )
        cancelled = await localloop.cancel(
            cancel_seat,
            rungs=(localloop.RUNG_SIGNAL,),
            envelope_grace=0.2,
            terminate_grace=1.0,
        )
        assert cancelled.reap.group_gone

        harvest_journal = run_journal.RunJournal.create(_identity("bh-harvest"), base=tmp_path)
        payload = json.dumps(
            {
                "outcome": {"status": "done", "summary": "ok", "bead_id": "bh-harvest"},
                "session_id": "provider-session-harvest",
                "cost_usd": 0.25,
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }
        )
        harvest_seat = await localloop.spawn_seat(
            [sys.executable, "-c", f"print({payload!r})"],
            bead_id="bh-harvest",
            role="developer",
            action="dispatch",
            session_id="seat-process-harvest",
            provider_continuation="provider-session-harvest",
            journal=harvest_journal,
        )
        await harvest_seat.collect()
        assert await harvest_seat.wait_exit(5)
        loop = localloop.LocalLoop(hive_dir=tmp_path, epic="bh-epic", actor="dev/test")
        loop.in_flight["bh-harvest"] = harvest_seat
        report = localloop.PassReport(number=1)
        await loop._harvest(report)
        assert report.harvested == (("bh-harvest", "done"),)
        return _records(cancel_journal.path), _records(harvest_journal.path)

    cancelled, harvested = asyncio.run(scenario())
    for records, bead in ((cancelled, "bh-cancel"), (harvested, "bh-harvest")):
        assert len({record["run_id"] for record in records}) == 1
        assert {record["bead"] for record in records} == {bead}
        assert {record["provider_continuation"] for record in records[1:]} == {
            f"provider-session-{'cancel' if bead == 'bh-cancel' else 'harvest'}"
        }
        assert records[0]["provider_continuation"] is None
        validator = jsonschema.Draft202012Validator(json.loads(SCHEMA.read_text()))
        for record in records:
            validator.validate(record)
    assert [record["activity"]["kind"] for record in cancelled] == [
        "run.created",
        "process.spawned",
        "process.cancelled",
    ]
    assert [record["activity"]["kind"] for record in harvested] == [
        "run.created",
        "process.spawned",
        "process.harvested",
    ]
    assert harvested[-1]["activity"]["usage"] == {"input_tokens": 10, "output_tokens": 5}


def test_missing_sink_degrades_without_changing_process_outcome(
    tmp_path: Path, monkeypatch
) -> None:
    diagnostics: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        run_journal._LOG,
        "error",
        lambda event, **fields: diagnostics.append((event, fields)),
    )
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("occupied")
    failed = run_journal.RunJournal.create(_identity("bh-failure"), base=blocked)
    assert failed.status.coverage == "degraded"
    assert failed.status.dropped_records == 1

    payload = json.dumps(
        {
            "outcome": {"status": "done", "summary": "same", "bead_id": "bh-failure"},
            "session_id": "provider-session",
            "cost_usd": 0,
            "usage": {},
        }
    )

    async def run_one(journal=None):
        seat = await localloop.spawn_seat(
            [sys.executable, "-c", f"print({payload!r})"],
            bead_id="bh-failure",
            role="developer",
            action="dispatch",
            session_id="provider-session",
            journal=journal,
        )
        stdout = await seat.collect()
        assert await seat.wait_exit(5)
        reap = await localloop.reap_group(seat, grace=1.0)
        return seat.proc.returncode, reap, seatrun.classify_run(seat.proc.returncode or 0, stdout)

    failed_result = asyncio.run(run_one(failed))
    control_result = asyncio.run(run_one())
    assert failed_result[0] == control_result[0] == 0
    assert failed_result[1].group_gone == control_result[1].group_gone is True
    assert failed_result[2] == control_result[2]
    assert failed.status.dropped_records == 2
    assert diagnostics == [
        (
            "run_journal_write_failed",
            {
                "run_id": failed.run_id,
                "hive": "github/beadhive/beadhive",
                "bead": "bh-failure",
                "operation": "create",
                "exception_class": "NotADirectoryError",
            },
        )
    ]


def test_local_loop_retry_mints_a_fresh_attempt(tmp_path: Path) -> None:
    identities: list[str] = []

    def identity(bead, action, role, routing):
        assert (bead, action, role) == ("bh-retry", "retry", "merger")
        return _identity(bead)

    async def scenario() -> None:
        command = f'{sys.executable} -c "print(123)"'
        loop = localloop.LocalLoop(
            hive_dir=tmp_path,
            epic="bh-epic",
            actor="dev/test",
            seat_command=command,
            workspace_for=lambda _bead: str(Path.cwd()),
            routing=lambda _bead, _role: None,
            run_identity=identity,
            journal_base=tmp_path / "journals",
        )
        for _ in range(2):
            report = localloop.PassReport(number=1)
            await loop._spawn_for("bh-retry", action="retry", role="merger", report=report)
            seat = loop.in_flight.pop("bh-retry")
            identities.append(seat.journal.run_id)
            await seat.collect()
            assert await seat.wait_exit(5)
            await localloop.reap_group(seat, grace=1.0)

    asyncio.run(scenario())
    assert len(identities) == len(set(identities)) == 2
