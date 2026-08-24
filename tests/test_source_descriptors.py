"""Exact named-hive source/launch descriptor coverage (bh-e8s3i.6)."""

from __future__ import annotations

import importlib.metadata
import json
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from beadhive import config, run_journal, source_descriptors

DIGEST = "sha256:" + "a" * 64


def _entry(index: int = 0, *, org: str = "acme", repo: str | None = None) -> dict[str, str]:
    repo = repo or f"hive-{index}"
    return {
        "provider": "github",
        "org": org,
        "repo": repo,
        "prefix": f"h{index}",
    }


def _resolver(tmp_path: Path, entries: list[dict], executable: Path, **kwargs):
    executable.chmod(0o755)
    roots = {entry["repo"]: tmp_path / entry["repo"] for entry in entries}
    for root in roots.values():
        root.mkdir(exist_ok=True)
    return lambda name, **call_kwargs: source_descriptors.resolve_named_hive_sources(
        name,
        cfg={"managed_repos": entries},
        executable_locator=lambda _name: str(executable),
        version_loader=lambda: "0.14.0",
        host_id_loader=lambda: "host-a",
        hive_dir_loader=lambda entry: roots[entry["repo"]],
        **kwargs,
        **call_kwargs,
    )


def test_missing_and_ambiguous_names_fail_closed_with_machine_reasons(tmp_path: Path) -> None:
    entries = [_entry(1, org="one", repo="shared"), _entry(2, org="two", repo="shared")]
    executable = tmp_path / "bh"
    executable.touch()
    resolve = _resolver(tmp_path, entries, executable)

    missing = resolve("absent")
    ambiguous = resolve("shared")

    assert missing.decision is source_descriptors.ResolutionDecision.REFUSED
    assert missing.reasons == (source_descriptors.ResolutionReason.HIVE_MISSING,)
    assert ambiguous.reasons == (source_descriptors.ResolutionReason.HIVE_AMBIGUOUS,)
    assert ambiguous.candidates == ("github/one/shared", "github/two/shared")
    assert ambiguous.descriptor is None


def test_cli_installation_and_version_are_required_exact_facts(tmp_path: Path) -> None:
    entry = _entry()
    root = tmp_path / entry["repo"]
    root.mkdir()
    executable = tmp_path / "bh"
    executable.touch()
    executable.chmod(0o755)
    common = dict(
        cfg={"managed_repos": [entry]},
        host_id_loader=lambda: "host-a",
        hive_dir_loader=lambda _entry: root,
    )

    absent = source_descriptors.resolve_named_hive_sources(
        entry["prefix"], executable_locator=lambda _name: None, **common
    )
    unknown = source_descriptors.resolve_named_hive_sources(
        entry["prefix"],
        executable_locator=lambda _name: str(executable),
        version_loader=lambda: (_ for _ in ()).throw(ValueError("unknown")),
        **common,
    )

    assert absent.reasons == (source_descriptors.ResolutionReason.CLI_UNINSTALLED,)
    assert unknown.reasons == (source_descriptors.ResolutionReason.CLI_VERSION_UNKNOWN,)


def test_current_host_resolves_and_wrong_host_refuses(tmp_path: Path) -> None:
    entry = _entry()
    executable = tmp_path / "bh"
    executable.touch()
    resolve = _resolver(tmp_path, [entry], executable)

    assert resolve(entry["prefix"], requested_host_id="host-a").descriptor is not None
    wrong = resolve(entry["prefix"], requested_host_id="host-b")
    assert wrong.reasons == (source_descriptors.ResolutionReason.WRONG_HOST,)

    absent_here = source_descriptors.resolve_named_hive_sources(
        entry["prefix"],
        cfg={"managed_repos": [entry]},
        executable_locator=lambda _name: str(executable),
        version_loader=lambda: "0.14.0",
        host_id_loader=lambda: "host-a",
        hive_dir_loader=lambda _entry: tmp_path / "not-installed-here",
    )
    assert absent_here.reasons == (source_descriptors.ResolutionReason.HIVE_NOT_INSTALLED_ON_HOST,)


def _journal_record(revision: str, *, run_id: str = "run-1") -> dict:
    return {
        "version": run_journal.VERSION,
        "source_revision": revision,
        "timestamp_ms": 1,
        "run_id": run_id,
        "hive": "github/acme/hive-0",
        "bead": "h0-1",
        "driver": "baml",
        "provider": "codex",
        "manifest_digest": DIGEST,
        "provider_continuation": None,
        "writer": run_journal.WRITER_LOCAL_LOOP,
        "activity": {"kind": "run.created", "phase": "planned"},
    }


def test_absent_runtime_sources_stay_unknown_not_authoritative_empty(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(config, "home", lambda: tmp_path / "home")
    entry = _entry()
    executable = tmp_path / "bh"
    executable.touch()
    row = _resolver(tmp_path, [entry], executable)(entry["prefix"], run_id="run-1").descriptor

    assert row is not None
    assert row.runtime.availability is source_descriptors.Availability.UNKNOWN
    assert (
        row.runtime.availability_reason
        is source_descriptors.ResolutionReason.WRITER_COLOCATION_UNVERIFIED
    )
    assert row.runtime.summary.observation.coverage == "unknown"
    assert row.runtime.summary.availability is source_descriptors.Availability.UNKNOWN
    assert row.runtime.summary.observation.coverage_reason == "source_missing"
    assert row.runtime.journal.observation.coverage == "unknown"
    assert row.runtime.journal.availability_reason.value == "writer_colocation_unverified"
    assert row.runtime.journal.observation.coverage_reason == "source_missing"


def test_recent_and_copied_runtime_sources_never_prove_writer_colocation(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(config, "home", lambda: home)
    entry = _entry()
    executable = tmp_path / "bh"
    executable.touch()
    summary = home / "dispatch" / "github-acme-hive-0.jsonl"
    summary.parent.mkdir(parents=True)
    summary.write_text("{}\n")
    os.utime(summary, None)

    local = _resolver(tmp_path, [entry], executable)(entry["prefix"]).descriptor
    row = _resolver(tmp_path, [entry], executable, copied_runtime=True)(entry["prefix"]).descriptor

    assert local is not None and row is not None
    assert local.runtime.summary.observation.freshness == "unknown"
    assert "writer colocation unverified" in (
        local.runtime.summary.observation.freshness_detail or ""
    )
    assert row.runtime.availability is source_descriptors.Availability.UNKNOWN
    assert row.runtime.availability_reason.value == "writer_colocation_unverified"
    assert row.runtime.summary.observation.freshness == "unknown"
    assert "copied source" in (row.runtime.summary.observation.freshness_detail or "")


def test_malformed_and_unreadable_runtime_sources_degrade_observation_only(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(config, "home", lambda: home)
    entry = _entry()
    executable = tmp_path / "bh"
    executable.touch()
    summary = home / "dispatch" / "github-acme-hive-0.jsonl"
    journal = home / "run-journals" / "github-acme-hive-0" / "run-1.jsonl"
    summary.parent.mkdir(parents=True)
    journal.parent.mkdir(parents=True)
    summary.write_text("not-json\n")
    journal.write_text("not-json\n")
    resolve = _resolver(tmp_path, [entry], executable)

    malformed = resolve(entry["prefix"], run_id="run-1").descriptor
    assert malformed is not None
    assert malformed.runtime.summary.observation.coverage == "partial"
    assert malformed.runtime.journal.observation.coverage == "degraded"
    assert malformed.runtime.availability is source_descriptors.Availability.UNKNOWN

    original = Path.read_bytes

    def unreadable(path: Path):
        if path in (summary, journal):
            raise PermissionError("fixture unreadable")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", unreadable)
    failed = resolve(entry["prefix"], run_id="run-1").descriptor
    assert failed is not None
    assert failed.runtime.summary.observation.coverage_reason == "source_unreadable"
    assert failed.runtime.journal.observation.coverage_reason == "source_unreadable"
    assert failed.runtime.availability_reason.value == "writer_colocation_unverified"


def test_stream_and_journal_contract_versions_argv_and_opaque_cursors(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(config, "home", lambda: home)
    entry = _entry()
    executable = tmp_path / "bh"
    executable.touch()
    journal = home / "run-journals" / "github-acme-hive-0" / "run-1.jsonl"
    journal.parent.mkdir(parents=True)
    journal.write_text(json.dumps(_journal_record("opaque:first")) + "\n")
    row = _resolver(tmp_path, [entry], executable)(
        entry["prefix"],
        run_id="run-1",
        stream_since="opaque:stream-cursor",
        journal_since="opaque:first",
    ).descriptor

    assert row is not None
    assert row.schema_version == 1
    assert row.stream.contract_version == "beadhive.stream/v1"
    assert row.stream.schema_version == 1
    assert row.stream.argv == (
        str(executable.absolute()),
        "stream",
        "--scope",
        "hive",
        "--hive",
        "github/acme/hive-0",
        "--format",
        "ndjson",
        "--since",
        "opaque:stream-cursor",
    )
    assert row.stream.cursor.opaque and row.stream.cursor.resume_argument == "--since"
    assert row.runtime.journal.contract_version == run_journal.VERSION
    assert row.runtime.journal.cursor.field == "source_revision"
    assert row.runtime.journal.observation.revision == "opaque:first"


def test_role_explain_sources_are_redacted_and_spawn_nothing(tmp_path: Path, monkeypatch) -> None:
    executable = tmp_path / "bh"
    executable.touch()
    executable.chmod(0o755)
    secret_home = tmp_path / "secret-home-token"
    monkeypatch.setattr(config, "home", lambda: secret_home)
    monkeypatch.setattr(source_descriptors.shutil, "which", lambda _name: str(executable))
    monkeypatch.setattr(source_descriptors, "_installed_version", lambda: "0.14.0")
    monkeypatch.setattr(source_descriptors.host, "host_id", lambda: "host-a")
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_a, **_kw: pytest.fail("role explain source projection spawned a process"),
    )

    payload = source_descriptors.role_explain_sources(_entry(), "github/acme/hive-0")
    encoded = json.dumps(payload)

    assert payload["decision"] == "available"
    assert payload["descriptor"]["runtime"]["summary"]["locator"] == (
        "<host-local:dispatch-summary>"
    )
    assert payload["descriptor"]["runtime"]["journal"]["locator_environment"] == (
        "BH_RUN_JOURNAL_PATH"
    )
    assert str(secret_home) not in encoded
    assert "writer_colocation_unverified" in encoded


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[-1].split()[0]
    except OSError:
        return False
    return state != "Z"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="descendant check uses /proc")
@pytest.mark.parametrize("count", [1, 3, 5])
def test_current_installed_cli_resolves_registry_and_reaps_every_descriptor_stream(
    tmp_path: Path, count: int
) -> None:
    """Run exact descriptor argv through real bh; fake only its bd backend boundary."""

    workspace = tmp_path / "workspace"
    home = tmp_path / "bh-home"
    home.mkdir()
    entries = [_entry(index) for index in range(count)]
    roots: dict[str, Path] = {}
    for entry in entries:
        root = workspace / entry["provider"] / entry["org"] / entry["repo"]
        root.mkdir(parents=True)
        roots[entry["repo"]] = root

    config_path = home / "config.yaml"
    managed = "".join(
        "  - provider: {provider}\n    org: {org}\n    repo: {repo}\n    prefix: {prefix}\n".format(
            **entry
        )
        for entry in entries
    )
    config_path.write_text(
        f"schema_version: 1\nproviders: [github]\nmanaged_repos:\n{managed}beads:\n  engine: bd\n"
    )

    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    backend = binary_dir / "bd"
    descendants = tmp_path / "descendants.log"
    backend.write_text(
        "#!/usr/bin/env python3\n"
        "import json,os,pathlib,sys,time\n"
        "args=sys.argv[1:]\n"
        "assert len(args) >= 4 and args[0] == '-C'\n"
        "hive=pathlib.Path(args[1]).name\n"
        "child=os.fork()\n"
        "if child == 0:\n"
        "    os.close(0); os.close(1); os.close(2); time.sleep(300); os._exit(0)\n"
        "with open(os.environ['BH_TEST_DESCENDANTS'],'a') as sink:\n"
        "    sink.write(f'{child}\\n')\n"
        "if args[2:] == ['gate','list','--limit','0','--all','--json']:\n"
        "    print('[]'); raise SystemExit(0)\n"
        "assert args[2] == 'export' and args[3] == '-o' and len(args) == 5\n"
        "record={'_type':'issue','id':f'{hive}-1','title':f'Issue {hive}',"
        "'issue_type':'task','status':'open','priority':1,"
        "'updated_at':'2026-08-24T00:00:00Z','labels':[]}\n"
        "pathlib.Path(args[4]).write_text(json.dumps(record)+'\\n')\n"
    )
    backend.chmod(0o755)

    executable = Path(sys.executable).parent / "bh"
    assert executable.is_file() and os.access(executable, os.X_OK)
    environment = {
        **os.environ,
        "PATH": f"{binary_dir}{os.pathsep}{os.environ['PATH']}",
        "BH_HOME": str(home),
        "BH_CONFIG": str(config_path),
        "GIT_WORKSPACE": str(workspace),
        "BH_TEST_DESCENDANTS": str(descendants),
        "OTEL_SDK_DISABLED": "true",
    }
    seen_pids: list[int] = []
    try:
        for entry in entries:
            resolved = source_descriptors.resolve_named_hive_sources(
                entry["prefix"],
                cfg={"managed_repos": entries},
                executable_locator=lambda _name: str(executable),
                host_id_loader=lambda: "host-a",
                hive_dir_loader=lambda item: roots[str(item["repo"])],
            )
            row = resolved.descriptor
            assert row is not None
            identity = f"github/acme/{entry['repo']}"
            assert row.cli.version == importlib.metadata.version("beadhive")
            assert row.correlation.registered_identity == identity
            assert row.correlation.stream_repo_slug == entry["repo"]
            assert row.stream.argv == (
                str(executable.absolute()),
                "stream",
                "--scope",
                "hive",
                "--hive",
                identity,
                "--format",
                "ndjson",
            )

            process = subprocess.Popen(
                row.stream.argv,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                ready, _, _ = select.select([process.stdout], [], [], 15)
                assert ready, "installed bh did not flush its descriptor snapshot"
                frame = json.loads(process.stdout.readline())
                assert frame["frame"] == "snapshot"
                assert frame["scope"] == "hive"
                assert frame["issues"][0]["hive"] == entry["repo"]
                published = [int(value) for value in descendants.read_text().splitlines()]
                current_pids = published[len(seen_pids) :]
                assert len(current_pids) == 2  # exact export + gate-list backend commands
                seen_pids.extend(current_pids)
                assert all(_alive(pid) for pid in current_pids)

                process.send_signal(signal.SIGTERM)
                process.wait(timeout=10)
                assert process.returncode == -signal.SIGTERM
                deadline = time.monotonic() + 10
                while any(_alive(pid) for pid in current_pids) and time.monotonic() < deadline:
                    time.sleep(0.05)
                assert not any(_alive(pid) for pid in current_pids), (
                    "installed bh descriptor stream left a backend descendant"
                )
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
    finally:
        for pid in seen_pids:
            if _alive(pid):
                os.kill(pid, signal.SIGKILL)
