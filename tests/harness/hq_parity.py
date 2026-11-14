"""Shadow parity with hq-service on real integration traffic (ADR 0005 migration step 3).

Opt-in pytest plugin: ``HQ_PARITY_BIN=<hq-parity> HQ_PARITY_LOG=<file.jsonl>
pytest -p harness.hq_parity tests/test_*_int.py``. Every time a test calls
``SqlTrustedReceiver.accept_hive_lease``, the Go service first decides the same
request against the same live Dolt server at the same clock instant, as a dry
run that writes nothing. Then Python decides for real. Both outcomes are
appended to the log. ``SqlRuntimeAuthority.load_state`` is shadowed the same
way by hq-service's signed authority read (kind ``authority``): HEAD and the
sha256 of the canonical signed record are compared. Without ``HQ_PARITY_BIN``
the plugin does nothing.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from beadhive.hq_sql_receiver import ReceiverError, SqlTrustedReceiver
from beadhive.hq_sql_runtime import SqlRuntimeAuthority, SqlRuntimeError
from beadhive.hq_sql_signatures import canonical


def _binding(settings: dict, binding: dict, password: str, database: str) -> dict:
    return {
        "host": binding["host"],
        "port": binding["port"],
        "database": database,
        "user": binding["user"],
        "password": password,
        "ca_file": binding.get("ca_file", ""),
        "server_name": binding.get("server_name", ""),
    }


KINDS = {
    "accept_hive_lease": "hive_lease",
    "accept_heartbeat": "heartbeat",
    "accept_registration": "registration",
}
CORPORA = {
    "hive_lease": ("HQ_LEASE_CORPUS", "hive-lease"),
    "heartbeat": ("HQ_HEARTBEAT_CORPUS", "heartbeat"),
    "registration": ("HQ_REGISTRATION_CORPUS", "registration"),
    "authority": ("HQ_AUTHORITY_CORPUS", "authority"),
}


def _go(
    binary: str,
    receiver,
    kind: str,
    principal: str,
    request_id: str,
    now: float,
) -> dict:
    settings = receiver.settings
    # The receiver reads through its observer connection, load_state through the runtime one.
    observer = settings["runtime" if kind == "authority" else "observer"]
    password = receiver.broker.get(observer["credential"], deadline=time.monotonic() + 5)
    # One rollback floor per HQ lineage, as Python keeps one per runtime_floor_path:
    # each integration test builds a fresh HQ that restarts at sequence 1.
    log = Path(os.environ["HQ_PARITY_LOG"])
    floor = log.with_name(f"{log.stem}.go-floor.{settings['runtime_initial_revision']}.json")
    payload = {
        # The Python receiver reads both databases through its observer connection.
        "runtime": _binding(settings, observer, password, observer["database"]),
        "config": _binding(
            settings,
            observer,
            password,
            (settings.get("reader") or {}).get("database", "beadhive_hq_config"),
        ),
        "operator_public_key": settings["runtime_operator_public_key"],
        "backend_identity": settings["runtime_backend_identity"],
        "generation": settings["runtime_generation"],
        "initial_revision": settings["runtime_initial_revision"],
        "floor_path": str(floor),
        "kind": kind,
        "principal": principal,
        "request_id": request_id,
        "now": now,  # the receiver's clock(), fractional, for every kind
    }
    run = subprocess.run(
        [binary], input=json.dumps(payload), capture_output=True, text=True, timeout=60
    )
    try:
        return json.loads(run.stdout)
    except ValueError:
        return {"error": f"hq-parity exit {run.returncode}: {run.stderr.strip()[:500]}"}


def shadow(original, kind: str, binary: str, log: Path, label: str):
    """Wrap accept_<kind>: Go dry-runs first, Python decides, both are logged."""

    def shadowed(self, principal, request_id):
        now = self.clock()
        try:
            go = _go(binary, self, kind, principal, request_id, now)
        except Exception as exc:  # the harness must never change the test's outcome
            go = {"error": f"harness: {type(exc).__name__}: {exc}"}
        record = {
            "kind": kind,
            "test": label,
            "principal": principal,
            "request_id": request_id,
            "now": now,
            "go": go,
        }
        try:
            revision = original(self, principal, request_id)
        except ReceiverError as exc:
            record["python"] = {
                "outcome": "refused",
                "message": str(exc),
                "type": type(exc).__name__,
            }
            with log.open("a") as f:
                f.write(json.dumps(record) + "\n")
            raise
        record["python"] = {"outcome": "accepted", "revision": revision}
        with log.open("a") as f:
            f.write(json.dumps(record) + "\n")
        return revision

    return shadowed


def shadow_authority(original, binary: str, log: Path, label: str):
    """Wrap load_state: Go reads the signed authority first, Python loads it, both are logged."""

    def shadowed(self, *args, **kwargs):
        try:
            go = _go(binary, self, "authority", "", "", self.clock())
        except Exception as exc:  # the harness must never change the test's outcome
            go = {"error": f"harness: {type(exc).__name__}: {exc}"}
        record = {
            "kind": "authority",
            "test": label,
            "request_id": "",
            "now": self.clock(),
            "go": go,
        }
        try:
            head, state, crossref, policies = result = original(self, *args, **kwargs)
        except SqlRuntimeError as exc:
            record["python"] = {
                "outcome": "refused",
                "message": str(exc),
                "type": type(exc).__name__,
            }
            with log.open("a") as f:
                f.write(json.dumps(record) + "\n")
            raise
        signed = {
            "backend_identity": self.settings["runtime_backend_identity"],
            "generation": self.settings["runtime_generation"],
            "revision": state["revision"],
            "config_backend": crossref[0],
            "config_generation": crossref[1],
            "config_head": crossref[2],
            "state": state,
            "hive_policies": policies,
        }
        record["python"] = {
            "outcome": "served",
            "head": head,
            "record_sha256": hashlib.sha256(canonical(signed, limit=16 * 1024 * 1024)).hexdigest(),
        }
        with log.open("a") as f:
            f.write(json.dumps(record) + "\n")
        return result

    return shadowed


def install_in_worker() -> None:
    """For receiver subprocesses (tests/harness/sql_receiver_worker.py)."""
    binary = os.environ.get("HQ_PARITY_BIN")
    if binary:
        label = (
            os.environ.get("PYTEST_CURRENT_TEST", "worker").removesuffix(" (call)") + " [worker]"
        )
        for method, kind in KINDS.items():
            original = getattr(SqlTrustedReceiver, method)
            log = Path(os.environ["HQ_PARITY_LOG"])
            setattr(SqlTrustedReceiver, method, shadow(original, kind, binary, log, label))
        original = SqlRuntimeAuthority.load_state
        log = Path(os.environ["HQ_PARITY_LOG"])
        SqlRuntimeAuthority.load_state = shadow_authority(original, binary, log, label)


@pytest.fixture(autouse=True)
def hq_parity(monkeypatch, request):
    binary = os.environ.get("HQ_PARITY_BIN")
    if binary:
        log = Path(os.environ["HQ_PARITY_LOG"])
        for method, kind in KINDS.items():
            original = getattr(SqlTrustedReceiver, method)
            monkeypatch.setattr(
                SqlTrustedReceiver, method, shadow(original, kind, binary, log, request.node.nodeid)
            )
        monkeypatch.setattr(
            SqlRuntimeAuthority,
            "load_state",
            shadow_authority(SqlRuntimeAuthority.load_state, binary, log, request.node.nodeid),
        )
    yield


def _codes(kind: str) -> dict:
    """Python refusal message -> stable code, from the hq-service corpus when present."""
    env, name = CORPORA[kind]
    root = os.environ.get(env) or (
        os.path.join(
            os.environ["GIT_WORKSPACE"], f"github/beadhive/hq-service/conformance/{name}/v1"
        )
        if os.environ.get("GIT_WORKSPACE")
        else ""
    )
    path = Path(root) / "refusal-codes.json"
    if not path.is_file():
        return {}
    return {
        msg: code for code, msg in json.loads(path.read_text()).items() if not code.startswith("_")
    }


def classify(record: dict, codes: dict) -> str:
    """match | commit-race, floor-lock (expected) | diff."""
    go, py = record["go"], record["python"]
    if py["outcome"] == "served":
        same = (go.get("head"), go.get("record_sha256")) == (py["head"], py["record_sha256"])
        return "match" if same else "diff"
    if py.get("message") == "HQ runtime floor custody wait exceeded deadline":
        # The test holds Python's floor lock; Go keeps its own floor file, so it is not blocked.
        return "floor-lock"
    if record.get("kind") == "authority":  # every other SqlRuntimeError is authority_failed
        return (
            "match" if go.get("refusal") == codes.get(py["message"], "authority_failed") else "diff"
        )
    if py["outcome"] == "accepted":
        return "match" if go.get("revision") == py["revision"] else "diff"
    if py.get("type") == "ReceiverUnknown":
        # Python's commit failed after crossing (a racing writer won). A dry run
        # cannot see commit-time races; hq-service retries such a 1213 to cas_conflict.
        return "commit-race" if go.get("outcome") in ("accepted", "replayed") else "diff"
    return "match" if go.get("refusal") == codes.get(py["message"]) else "diff"


def pytest_terminal_summary(terminalreporter):
    if not os.environ.get("HQ_PARITY_BIN"):
        return
    log = Path(os.environ["HQ_PARITY_LOG"])
    records = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    codes = {kind: _codes(kind) for kind in CORPORA}
    verdicts = [classify(r, codes[r.get("kind", "hive_lease")]) for r in records]
    terminalreporter.section("hq-service shadow parity")
    for record, verdict in zip(records, verdicts, strict=True):
        if verdict != "match":
            terminalreporter.line(
                f"{verdict}: {record['test']} {record['request_id']} "
                f"python={record['python']} go={record['go']}"
            )
    for kind in CORPORA:
        mine = [
            v for r, v in zip(records, verdicts, strict=True) if r.get("kind", "hive_lease") == kind
        ]
        terminalreporter.line(
            f"{kind}: {mine.count('match')} match, {mine.count('commit-race')} commit-race, "
            f"{mine.count('floor-lock')} floor-lock, "
            f"{mine.count('diff')} diff of {len(mine)} real decisions"
        )
