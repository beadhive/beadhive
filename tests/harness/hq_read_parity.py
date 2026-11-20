"""Shadow parity of the route=api READS with hq-service (bh-h9dn1), beside ``hq_parity.py``.

Opt-in pytest plugin, the same switch as :mod:`harness.hq_parity`: ``HQ_PARITY_BIN=<hq-parity>
HQ_PARITY_LOG=<file.jsonl> pytest -p harness.hq_read_parity tests/test_*_int.py``. Without
``HQ_PARITY_BIN`` it does nothing.

Two runners, each shadowing a SQL read the integration tests already make against a live Dolt
server, at the same instant:

* **eligibility** (``SqlControlPlane.read_eligibility``): Python reads the session-carrier
  composite; hq-service is asked (kind ``eligibility``) for the same frame's
  ``EligibilityRead`` and the signed authority envelope it is bound to. The Go body is folded by
  the api plane's own projection (:func:`beadhive.hq_api_eligibility.eligibility`) and the two
  verdicts are compared: head, grant state and cordon, and the predicates both sides compute
  (``session_epoch_matches``, ``authenticated_fresh_heartbeat``, ``conformance_pass``,
  ``release_matches``).
* **config** (``SqlFleetConfigRevisionStore.load_snapshot``): hq-service (kind ``config``)
  serves ``ConfigRead``; the committed head (the signed record's ``config_head``) and the
  ordered documents digest are compared.

Upstream gap: ``hq-parity`` at hq-service v0.1.0 serves only the ``hive_lease``, ``heartbeat``,
``registration`` and ``authority`` kinds and rejects unknown input fields, so until it gains
``eligibility`` and ``config`` every record classifies ``unsupported`` (never ``diff``). The
runners, the projection and the comparison are exercised without a binary by
``tests/test_hq_read_parity.py``.

Known, documented divergence the comparison tolerates (see :mod:`beadhive.hq_api_eligibility`):
``conformance_pass`` on the api side cannot check the evidence profile, and ``release_matches``
judges the manifest's release where SQL judges the evidence report's. Both are compared and a
mismatch is reported as ``diff``; they are expected to agree on a conforming frame.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from beadhive import hq_api_eligibility as api_reads
from beadhive.modules.config.domain.ports import ordered_documents_digest

#: The predicates both carriers compute from the same rows.
PREDICATES = (
    "session_epoch_matches",
    "authenticated_fresh_heartbeat",
    "conformance_pass",
    "release_matches",
)


def _binding(binding: dict, password: str, database: str) -> dict:
    return {
        "host": binding["host"],
        "port": binding["port"],
        "database": database,
        "user": binding["user"],
        "password": password,
        "ca_file": binding.get("ca_file", ""),
        "server_name": binding.get("server_name", ""),
    }


def _payload(settings: dict, broker: Any, kind: str, **extra: Any) -> dict:
    """The ``hq-parity`` request: the runtime and config bindings, the trust pins, the kind."""
    runtime = settings["runtime"]
    password = broker.get(runtime["credential"], deadline=time.monotonic() + 5)
    log = Path(os.environ["HQ_PARITY_LOG"])
    floor = log.with_name(f"{log.stem}.go-floor.{settings['runtime_initial_revision']}.json")
    return {
        "runtime": _binding(runtime, password, runtime["database"]),
        "config": _binding(
            runtime, password, (settings.get("reader") or {}).get("database", "beadhive_hq_config")
        ),
        "operator_public_key": settings["runtime_operator_public_key"],
        "backend_identity": settings["runtime_backend_identity"],
        "generation": settings["runtime_generation"],
        "initial_revision": settings["runtime_initial_revision"],
        "floor_path": str(floor),
        "kind": kind,
        **extra,
    }


def _go(binary: str, payload: dict) -> dict:
    run = subprocess.run(
        [binary], input=json.dumps(payload), capture_output=True, text=True, timeout=60
    )
    try:
        return json.loads(run.stdout)
    except ValueError:
        return {"error": f"hq-parity exit {run.returncode}: {run.stderr.strip()[:500]}"}


# ---- verdicts -----------------------------------------------------------------------------


def python_eligibility(result: tuple) -> dict:
    """The SQL plane's ``(head, desired, observation)`` as comparable verdicts."""
    head, desired, observation = result
    verdict: dict[str, Any] = {
        "head": head,
        "state": desired.get("state"),
        "cordoned": desired.get("cordoned"),
        "carrier": getattr(observation, "carrier", "beat"),
    }
    for name in PREDICATES:
        verdict[name] = bool(getattr(observation, name, False))
    return verdict


def go_eligibility(go: dict, manifest: Any, *, enforce: bool) -> dict:
    """hq-service's ``{eligibility, authority}`` folded by the api plane's projection."""
    body = {**go["eligibility"], "authority": go["authority"]}
    measured = api_reads.manifest_release(manifest)
    head, _active, desired, _policies, observation = api_reads.eligibility(
        body, manifest, enforce=enforce, measured=measured
    )
    verdict: dict[str, Any] = {
        "head": head,
        "state": desired.get("state"),
        "cordoned": desired.get("cordoned"),
        "carrier": getattr(observation, "carrier", "beat"),
    }
    for name in PREDICATES:
        verdict[name] = bool(getattr(observation, name, False))
    return verdict


def python_config(snapshot: Any) -> dict:
    return {
        "config_head": snapshot.commit_revision,
        "documents_sha256": ordered_documents_digest(snapshot.documents),
    }


def go_config(go: dict) -> dict:
    body = go["config"]
    return {
        "config_head": body["authority"]["record"]["config_head"],
        "documents_sha256": body["documents_sha256"],
    }


def classify(record: dict) -> str:
    """``match`` | ``diff`` | ``unsupported`` (hq-parity lacks the kind) | ``error`` |
    ``not-comparable`` (the Python read was on the signed-beat carrier, not sessions)."""
    go, python = record.get("go") or {}, record.get("python") or {}
    if "error" in go:
        message = str(go["error"])
        if "unknown field" in message or "kind" in message:
            return "unsupported"
        return "error"
    if python.get("outcome") == "refused" or go.get("outcome") == "refused":
        return "match" if python.get("outcome") == go.get("outcome") else "diff"
    if record.get("kind") == "eligibility" and python.get("verdict", {}).get("carrier") != (
        api_reads.SESSION_CARRIER
    ):
        return "not-comparable"
    return "match" if python.get("verdict") == go.get("verdict") else "diff"


def _append(log: Path, record: dict) -> None:
    with log.open("a") as handle:
        handle.write(json.dumps(record, default=str) + "\n")


# ---- shadows ------------------------------------------------------------------------------


def shadow_eligibility(original, binary: str, log: Path, label: str):
    """Wrap ``SqlControlPlane.read_eligibility``: Python decides, hq-service is asked, both
    logged. The harness never changes the test's outcome."""

    def shadowed(self, manifest, *args, **kwargs):
        from beadhive.hq_authority_enforce import enforced

        record: dict[str, Any] = {"kind": "eligibility", "test": label}
        try:
            result = original(self, manifest, *args, **kwargs)
        except ValueError as exc:
            record["python"] = {"outcome": "refused", "message": str(exc)}
            result = exc
        else:
            record["python"] = {"outcome": "served", "verdict": python_eligibility(result)}
        try:
            runtime = self._runtime_authority()
            go = _go(
                binary,
                _payload(
                    runtime.settings,
                    runtime.broker,
                    "eligibility",
                    frame=manifest.frame_id,
                    measured=api_reads.manifest_release(manifest),
                ),
            )
            if go.get("outcome") == "served":
                go = {
                    "outcome": "served",
                    "verdict": go_eligibility(go, manifest, enforce=enforced()),
                }
        except Exception as exc:  # noqa: BLE001 - the harness must never change the outcome
            go = {"error": f"harness: {type(exc).__name__}: {exc}"}
        record["go"] = go
        _append(log, record)
        if isinstance(result, BaseException):
            raise result
        return result

    return shadowed


def shadow_config(original, binary: str, log: Path, label: str):
    """Wrap ``SqlFleetConfigRevisionStore.load_snapshot`` the same way (kind ``config``)."""

    def shadowed(self, *args, **kwargs):
        record: dict[str, Any] = {"kind": "config", "test": label}
        try:
            snapshot = original(self, *args, **kwargs)
        except ValueError as exc:
            record["python"] = {"outcome": "refused", "message": str(exc)}
            snapshot = exc
        else:
            record["python"] = {"outcome": "served", "verdict": python_config(snapshot)}
        try:
            settings = self.settings
            if settings.get("runtime") is None:
                go = {"error": "harness: no runtime binding to read config through"}
            else:
                go = _go(binary, _payload(settings, self.broker, "config"))
                if go.get("outcome") == "served":
                    go = {"outcome": "served", "verdict": go_config(go)}
        except Exception as exc:  # noqa: BLE001 - the harness must never change the outcome
            go = {"error": f"harness: {type(exc).__name__}: {exc}"}
        record["go"] = go
        _append(log, record)
        if isinstance(snapshot, BaseException):
            raise snapshot
        return snapshot

    return shadowed


@pytest.fixture(autouse=True)
def hq_read_parity(monkeypatch, request):
    binary = os.environ.get("HQ_PARITY_BIN")
    if binary:
        from beadhive.hq_control_plane import SqlControlPlane
        from beadhive.hq_sql_config import SqlFleetConfigRevisionStore

        log = Path(os.environ["HQ_PARITY_LOG"]).with_suffix(".reads.jsonl")
        label = request.node.nodeid
        monkeypatch.setattr(
            SqlControlPlane,
            "read_eligibility",
            shadow_eligibility(SqlControlPlane.read_eligibility, binary, log, label),
        )
        monkeypatch.setattr(
            SqlFleetConfigRevisionStore,
            "load_snapshot",
            shadow_config(SqlFleetConfigRevisionStore.load_snapshot, binary, log, label),
        )
    yield


def pytest_terminal_summary(terminalreporter):
    if not os.environ.get("HQ_PARITY_BIN"):
        return
    log = Path(os.environ["HQ_PARITY_LOG"]).with_suffix(".reads.jsonl")
    records = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    verdicts = [classify(record) for record in records]
    terminalreporter.section("hq-service read parity")
    for record, verdict in zip(records, verdicts, strict=True):
        if verdict in ("diff", "error"):
            terminalreporter.line(
                f"{verdict}: {record['test']} python={record['python']} go={record['go']}"
            )
    for kind in ("eligibility", "config"):
        mine = [v for r, v in zip(records, verdicts, strict=True) if r.get("kind") == kind]
        counts = ", ".join(
            f"{mine.count(name)} {name}"
            for name in ("match", "diff", "unsupported", "not-comparable", "error")
        )
        terminalreporter.line(f"{kind}: {counts} of {len(mine)} reads")
