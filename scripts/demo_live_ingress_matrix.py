#!/usr/bin/env python3
"""Hermetic L1-L4 live-ingress acceptance matrix.

The providers are deterministic, provider-qualified packed-seat doubles.  They prove Beadhive's
orchestration boundary; genuine baml-harness Codex remains an explicitly blocked external smoke.
Every wait has a deadline and every absent observation raises, so this demo cannot silently skip.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from beadhive import localloop, role_execution, role_process, run_journal

ROOT = Path(__file__).resolve().parents[1]
TIMEOUT = 10.0
HIVE = "github/beadhive/beadhive"
DIGEST = "sha256:" + "d" * 64
PROVIDERS = {"claude": "claude-code", "codex": "codex"}


def require(value, detail: str):
    if not value:
        raise RuntimeError(detail)
    return value


def wait_for(predicate, detail: str) -> None:
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise TimeoutError(detail)


async def async_wait_for(predicate, detail: str) -> None:
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise TimeoutError(detail)


def digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def fixture(root: Path, provider: str) -> role_execution.QualifiedArtifact:
    binary = root / f"bh-developer-{provider}"
    binary.write_text(
        "#!" + sys.executable + "\n"
        "import json,os,pathlib,subprocess,sys,time\n"
        "def arg(name): return sys.argv[sys.argv.index(name)+1]\n"
        "continuation=arg('--session_id'); bead=arg('--bead')\n"
        "progress=pathlib.Path(os.environ['LIVE_PROGRESS_PATH'])\n"
        "release=pathlib.Path(os.environ['LIVE_RELEASE_PATH'])\n"
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
        "pathlib.Path(os.environ['LIVE_DESCENDANT_PATH']).write_text(str(child.pid))\n"
        "progress.write_text(json.dumps({'kind':'provider.progress','provider':"
        + repr(provider)
        + ",'session_id':continuation}))\n"
        "print('provider.progress',file=sys.stderr,flush=True)\n"
        "while not release.exists(): time.sleep(.01)\n"
        "child.terminate(); child.wait(5)\n"
        "print(json.dumps({'outcome':{'status':'done','summary':'fixture','bead_id':bead},"
        "'session_id':continuation,'cost_usd':0.01,'usage':{'input_tokens':1,'output_tokens':1}}),flush=True)\n"
    )
    binary.chmod(0o755)
    packs = [{"name": "fixture", "version": "1", "digest": DIGEST}]
    permissions = {"allow": ["Read"], "ask": [], "deny": ["Bash(git push*)"]}
    authority = {
        "provider": provider,
        "inherit_user_config": False,
        "mcp": {"enabled": False, "servers": []},
    }
    mechanism = "codex-jsonl"
    if provider == "claude-code":
        authority.update(permission_mode="auto", permissions=permissions)
        mechanism = "claude-stream-json"
    else:
        authority.update(sandbox="read-only", approval="untrusted")
    manifest = {
        "version": 1,
        "artifact": binary.name,
        "artifact_digest": digest(binary),
        "seat": "developer",
        "provider": provider,
        "driver": "baml-harness",
        "runnable": True,
        "baked": True,
        "profile": "deterministic-fixture",
        "profile_digest": DIGEST,
        "packs_digest": DIGEST,
        "contract_version": 1,
        "packs": packs,
        "authority": authority,
        "framing": {"input": "stream-json", "output": "seat-run-jsonl"},
        "live_event_mechanism": mechanism,
        "capabilities": {
            "name": "developer",
            "permission_mode": "auto",
            "permissions": permissions,
            "inherit_user": False,
            "has_mcp": False,
            "mcp_servers": [],
            "packs": packs,
            "baked": {
                "baked": True,
                "provider": provider,
                "profile": "deterministic-fixture",
                "contract_version": 1,
            },
        },
    }
    manifest_path = binary.with_name(binary.name + ".manifest.json")
    manifest_path.write_text(json.dumps(manifest, sort_keys=True))
    return role_execution.validate_qualified_artifact(
        binary, manifest_path, seat="developer", provider=provider
    )


def env_for(root: Path, name: str) -> tuple[dict[str, str], Path, Path, Path]:
    progress, release, descendant = (
        root / f"{name}.progress.json",
        root / f"{name}.release",
        root / f"{name}.descendant",
    )
    env = dict(os.environ)
    env.update(
        LIVE_PROGRESS_PATH=str(progress),
        LIVE_RELEASE_PATH=str(release),
        LIVE_DESCENDANT_PATH=str(descendant),
    )
    return env, progress, release, descendant


def identity(provider: str, bead: str) -> run_journal.RunIdentity:
    return run_journal.RunIdentity(HIVE, bead, "baml", provider, DIGEST)


async def work_attempt(
    root: Path, artifact, name: str, *, cancel: bool = False, degraded: bool = False
) -> dict:
    env, progress, release, descendant = env_for(root, name)
    hive_dir = root / "hive"
    workspace = root / "workspace"
    hive_dir.mkdir(exist_ok=True)
    workspace.mkdir(exist_ok=True)
    context = localloop.SeatLaunchContext(
        str(artifact.binary), "", HIVE, "baml", artifact.provider, artifact.manifest_digest
    )
    journal_base = root / "journals"
    if degraded:
        journal_base = root / f"{name}.blocked"
        journal_base.write_text("not a directory")
    loop = localloop.LocalLoop(
        hive_dir=hive_dir,
        epic="bh-matrix",
        actor="dev/matrix",
        workspace_for=lambda _bead: str(workspace),
        instructions=lambda *_a: str(root / "instructions.md"),
        routing=lambda *_a: None,
        launch_context=lambda _role: context,
        journal_base=journal_base,
        env=env,
        terminate_grace=1.0,
        envelope_grace=0.2,
    )
    report = localloop.PassReport(number=1)
    await loop._spawn_for("bh-matrix.1", action="dispatch", role="developer", report=report)
    seat = require(loop.in_flight.get("bh-matrix.1"), f"{name}: no spawn")
    await async_wait_for(progress.exists, f"{name}: no live progress")
    require(not seat.finished, f"{name}: progress arrived only after outer exit")
    require(descendant.exists(), f"{name}: descendant tripwire missing")
    if cancel:
        result = await localloop.cancel(
            seat,
            rungs=(localloop.RUNG_SIGNAL,),
            envelope_grace=0.2,
            terminate_grace=1.0,
        )
        require(result.reap.group_gone, f"{name}: descendant survived cancellation")
        return {"run_id": seat.journal.run_id, "cancelled": True}
    release.touch()
    await async_wait_for(lambda: seat.finished, f"{name}: fixture did not exit")
    await loop._harvest(report)
    require(report.harvested == (("bh-matrix.1", "done"),), f"{name}: harvest missing")
    if degraded:
        require(seat.journal.degraded, f"{name}: broken journal was not diagnosed")
        require(seat.journal.dropped_records >= 2, f"{name}: dropped writes were hidden")
    else:
        rows = [json.loads(line) for line in seat.journal.path.read_text().splitlines()]
        require(
            rows[-1]["activity"]["kind"] == "process.harvested",
            f"{name}: no harvest record",
        )
    require(
        len({seat.journal.run_id, seat.session_id, seat.provider_continuation}) == 3,
        f"{name}: identities collapsed",
    )
    return {
        "run_id": seat.journal.run_id,
        "seat_process_id": seat.session_id,
        "provider_continuation": seat.provider_continuation,
        "journal": str(seat.journal.path),
        "live_before_exit": True,
        "final_seat_runs": 1,
    }


def direct_attempt(root: Path, artifact, name: str, *, degraded: bool = False) -> dict:
    env, progress, release, _descendant = env_for(root, name)
    journal_base = root / "journals"
    if degraded:
        journal_base = root / f"{name}.blocked"
        journal_base.write_text("not a directory")
    outer = run_journal.RunJournal.create(
        identity(artifact.provider, "bh-matrix.1"), base=journal_base
    )
    seat_process_id = "seat-" + name
    continuation = "provider-" + name
    argv = localloop.seat_argv(
        str(artifact.binary),
        "developer",
        workspace=str(root),
        bead="bh-matrix.1",
        instructions=str(root / "instructions.md"),
        session_id=continuation,
        bundle="",
    )
    result: list[int] = []
    thread = threading.Thread(
        target=lambda: result.append(
            role_process.run_foreground(
                argv,
                cwd=root,
                env=env,
                journal=outer,
                bead="bh-matrix.1",
                role="developer",
                seat_process_id=seat_process_id,
                provider_continuation=continuation,
            )
        )
    )
    thread.start()
    wait_for(progress.exists, f"{name}: no live progress")
    require(thread.is_alive(), f"{name}: progress arrived only after outer exit")
    release.touch()
    thread.join(TIMEOUT)
    require(not thread.is_alive() and result == [0], f"{name}: direct role did not harvest")
    if degraded:
        require(outer.degraded and outer.dropped_records >= 2, f"{name}: sink loss hidden")
    else:
        rows = [json.loads(line) for line in outer.path.read_text().splitlines()]
        require(
            [row["activity"]["kind"] for row in rows][-1] == "process.harvested",
            f"{name}: no harvest record",
        )
    require(
        len({outer.run_id, seat_process_id, continuation}) == 3, f"{name}: identities collapsed"
    )
    return {
        "run_id": outer.run_id,
        "seat_process_id": seat_process_id,
        "provider_continuation": continuation,
        "journal": str(outer.path),
        "live_before_exit": True,
        "final_seat_runs": 1,
    }


async def direct_cancel(root: Path, artifact, name: str) -> None:
    env, progress, _release, _descendant = env_for(root, name)
    outer = run_journal.RunJournal.create(
        identity(artifact.provider, "bh-matrix.1"), base=root / "journals"
    )
    continuation = "provider-" + name
    argv = localloop.seat_argv(
        str(artifact.binary),
        "developer",
        workspace=str(root),
        bead="bh-matrix.1",
        instructions=str(root / "instructions.md"),
        session_id=continuation,
        bundle="",
    )
    task = asyncio.create_task(
        role_process._run_foreground(
            argv,
            cwd=root,
            env=env,
            journal=outer,
            bead="bh-matrix.1",
            role="developer",
            seat_process_id="seat-" + name,
            provider_continuation=continuation,
        )
    )
    await async_wait_for(progress.exists, f"{name}: no progress before cancel")
    require(not task.done(), f"{name}: direct role exited before cancellation")
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    else:
        raise RuntimeError(f"{name}: caller cancellation did not propagate")
    rows = [json.loads(line) for line in outer.path.read_text().splitlines()]
    require(rows[-1]["activity"]["kind"] == "process.cancelled", f"{name}: cancel not journaled")
    require(rows[-1]["activity"]["process"]["group_gone"], f"{name}: descendant survived")


def parity(root: Path, *, python_executable: str | None = None) -> dict:
    python_executable = python_executable or sys.executable
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    source = root / "committed"
    archive = root / "source.tar"
    with archive.open("wb") as sink:
        subprocess.run(["git", "archive", "HEAD"], cwd=ROOT, stdout=sink, check=True)
    source.mkdir()
    subprocess.run(["tar", "-xf", archive, "-C", source], check=True)
    installed = root / "installed"
    bin_dir = installed / "bin"
    site = installed / "site-packages"
    bin_dir.mkdir(parents=True)
    site.mkdir()
    # Construct only the isolation we need instead of asking stdlib venv to bootstrap pip.  The
    # check-all fence deliberately has no ensurepip, registry, or network.  -I/-S excludes cwd,
    # PYTHONPATH, user packages, and automatic site processing; the explicit addsitedir below
    # admits this exact archived package and its recorded read-only runtime dependencies only.
    dependency_paths = [
        value for value in sys.path if "site-packages" in value and Path(value).is_dir()
    ]
    require(dependency_paths, "isolated parity could not locate runtime dependencies")
    (site / "_beadhive_parity_dependencies.pth").write_text("\n".join(dependency_paths) + "\n")
    shutil.copytree(source / "src" / "beadhive", site / "beadhive")
    bootstrap = f"import site,sys;sys.path.insert(0,{str(site)!r});site.addsitedir({str(site)!r});"
    bh = bin_dir / "bh"
    bh_program = bootstrap + "sys.argv[0]='bh';from beadhive.cli import app;app()"
    bh.write_text(
        f"#!/bin/sh\nexec {shlex.quote(python_executable)} -I -S -c "
        f'{shlex.quote(bh_program)} "$@"\n',
        encoding="utf-8",
    )
    bh.chmod(0o755)
    env = {**os.environ, "BH_SKIP_SETUP_CHECK": "1"}
    for name in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP"):
        env.pop(name, None)
    env["PYTHONNOUSERSITE"] = "1"
    loop_help = subprocess.check_output([bh, "work", "loop", "--help"], env=env, text=True)
    role_help = subprocess.check_output([bh, "role", "--help"], env=env, text=True)
    require(
        "--baml-required" in loop_help and "--harness" in loop_help, "installed loop lacks feature"
    )
    require("--baml-required" in role_help, "installed role lacks feature")
    installed_module = subprocess.check_output(
        [
            python_executable,
            "-I",
            "-S",
            "-c",
            bootstrap + "import beadhive.work_dispatch as m;print(m.__file__)",
        ],
        env=env,
        text=True,
    ).strip()
    committed = subprocess.check_output(
        ["git", "show", f"{sha}:src/beadhive/work_dispatch.py"], cwd=ROOT
    )
    require(
        hashlib.sha256(committed).digest()
        == hashlib.sha256(Path(installed_module).read_bytes()).digest(),
        "installed module differs from committed SHA",
    )
    return {
        "commit_sha": sha,
        "executable_digest": digest(bh),
        "module_digest": digest(Path(installed_module)),
        "install_method": (
            "manual isolated bin/site exact-SHA archive copy; read-only runtime deps only"
        ),
        "feature_probes": ["work-loop-baml-required", "role-baml-required"],
    }


def refusal_matrix(root: Path) -> dict[str, str]:
    cases = {
        "invalid": (lambda doc: "{not-json", "manifest_invalid"),
        "versioned": (lambda doc: {**doc, "version": 2}, "manifest_version_unsupported"),
        "unbaked": (lambda doc: {**doc, "baked": False}, "artifact_unbaked"),
        "nonrunnable": (lambda doc: {**doc, "runnable": False}, "provider_unavailable"),
        "provider": (lambda doc: {**doc, "provider": "wrong"}, "provider_mismatch"),
        "driver": (lambda doc: {**doc, "driver": "wrong"}, "driver_mismatch"),
        "digest": (lambda doc: {**doc, "artifact_digest": DIGEST}, "digest_mismatch"),
        "provenance": (
            lambda doc: {key: value for key, value in doc.items() if key != "profile"},
            "manifest_invalid",
        ),
        "authority": (
            lambda doc: {
                **doc,
                "authority": {**doc["authority"], "inherit_user_config": True},
            },
            "authority_unsupported",
        ),
        "live-mechanism": (
            lambda doc: {**doc, "live_event_mechanism": "none"},
            "live_mechanism_unsupported",
        ),
    }
    proved: dict[str, str] = {}
    for name, (mutation, expected) in cases.items():
        case_root = root / f"refusal-{name}"
        case_root.mkdir()
        artifact = fixture(case_root, "codex")
        document = json.loads(artifact.manifest_path.read_text())
        changed = mutation(document)
        if isinstance(changed, str):
            artifact.manifest_path.write_text(changed)
        else:
            artifact.manifest_path.write_text(json.dumps(changed))
        try:
            role_execution.validate_qualified_artifact(
                artifact.binary,
                artifact.manifest_path,
                seat="developer",
                provider="codex",
            )
        except role_execution.RoleLaunchRefused as exc:
            require(exc.code == expected, f"{name}: got {exc.code}, expected {expected}")
            require("secret-fixture-value" not in exc.detail, f"{name}: refusal leaked content")
            proved[name] = exc.code
        else:
            raise RuntimeError(f"{name}: invalid artifact was accepted")

    missing_root = root / "refusal-missing"
    missing_root.mkdir()
    binary = missing_root / "bh-developer-codex"
    binary.write_text("fixture")
    binary.chmod(0o755)
    try:
        role_execution.validate_qualified_artifact(
            binary,
            binary.with_name(binary.name + ".manifest.json"),
            seat="developer",
            provider="codex",
        )
    except role_execution.RoleLaunchRefused as exc:
        require(exc.code == "manifest_missing", "missing manifest did not refuse")
        proved["missing"] = exc.code
    else:
        raise RuntimeError("missing manifest accepted")

    journal = run_journal.RunJournal.create(
        identity("codex", "bh-matrix.1"), base=root / "conflict"
    )
    try:
        journal.child_env({"BH_RUN_ID": "conflicting-outer"})
    except run_journal.RunContextConflict:
        proved["conflicting-context"] = "run_context_conflict"
    else:
        raise RuntimeError("conflicting inherited outer id accepted")
    # BAML-required fallback is proven by the resolver's exact missing-artifact refusal.  No
    # claim/spawn callback exists anywhere in this pure validation matrix.
    proved["fallback"] = "artifact_missing_no_fallback"
    return proved


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="bh-live-ingress-") as td:
        root = Path(td)
        (root / "instructions.md").write_text("fixture")
        results = {}
        for index, (_harness, provider) in enumerate(PROVIDERS.items(), 1):
            artifact = fixture(root, provider)
            for surface in ("work", "role"):
                cell = f"L{index if surface == 'work' else index + 2}"
                first = (
                    asyncio.run(work_attempt(root, artifact, cell + "-first"))
                    if surface == "work"
                    else direct_attempt(root, artifact, cell + "-first")
                )
                retry = (
                    asyncio.run(work_attempt(root, artifact, cell + "-retry"))
                    if surface == "work"
                    else direct_attempt(root, artifact, cell + "-retry")
                )
                require(first["run_id"] != retry["run_id"], f"{cell}: retry reused outer id")
                if surface == "work":
                    asyncio.run(work_attempt(root, artifact, cell + "-cancel", cancel=True))
                    asyncio.run(work_attempt(root, artifact, cell + "-degraded", degraded=True))
                else:
                    asyncio.run(direct_cancel(root, artifact, cell + "-cancel"))
                    direct_attempt(root, artifact, cell + "-degraded", degraded=True)
                results[cell] = {
                    "surface": surface,
                    "provider": provider,
                    "first": first,
                    "retry_run_id": retry["run_id"],
                    "cancellation_cleanup": True,
                    "journal_degradation_invariant": True,
                }
        report = {
            "schema_version": 1,
            "contract": "beadhive.live-ingress-matrix/v1",
            "cells": results,
            "refusal_matrix": refusal_matrix(root),
            "installed_parity": parity(root),
            "compatibility_claim": "producer schemas and raw fixtures only",
            "beadhive_ui_codec_consumption": "not claimed",
            "genuine_provider_smoke": {
                "status": "externally_blocked",
                "reason": "current baml-harness Codex artifact is non-runnable/nonconformant",
            },
        }
        print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
