"""Run-scoped, append-only activity journals.

This module implements the storage half of the accepted
``beadhive.run-journal/v1`` contract.  It deliberately does not resolve a
driver, provider, or launch manifest: the component that validates a launch
owns those facts and passes :class:`RunIdentity` to the process owner.

Journal I/O is an observability side effect.  Every write failure is converted
to degradation state plus a structured diagnostic; it never escapes into the
seat lifecycle.  A record is encoded as one bounded JSON line and issued with
one ``os.write`` against an ``O_APPEND`` descriptor so concurrent processes
cannot share a stale file offset or interleave a well-formed record.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from . import config, log, registry

VERSION = "beadhive.run-journal/v1"
WRITER_LOCAL_LOOP = "beadhive.local-loop"
WRITER_ROLE = "beadhive.role"
WRITER_HITCH = "agent-hitch.direct"
WRITER_BAML = "baml.provider"
WRITERS = frozenset({WRITER_LOCAL_LOOP, WRITER_ROLE, WRITER_HITCH, WRITER_BAML})
ATOMIC_LINE_BYTES = 4096
ENV_FIELDS = {
    "BH_RUN_JOURNAL_VERSION": "version",
    "BH_RUN_JOURNAL_PATH": "path",
    "BH_RUN_ID": "run_id",
    "BH_RUN_HIVE": "hive",
    "BH_RUN_BEAD": "bead",
    "BH_RUN_DRIVER": "driver",
    "BH_RUN_PROVIDER": "provider",
    "BH_RUN_MANIFEST_DIGEST": "manifest_digest",
}

_LOG = log.get_logger(__name__)
_RUN_ID = re.compile(r"^[A-Za-z0-9_.:-]+$")
_ACTIVITY_FIELDS = frozenset(
    {
        "kind",
        "phase",
        "outcome_code",
        "reason_code",
        "process",
        "provider_fact",
        "usage",
        "cost_usd",
        "journal_degraded",
    }
)
_PROCESS_FIELDS = frozenset({"pid", "pgid", "exit_code", "cancel_rung", "signals", "group_gone"})
_PROVIDER_FIELDS = frozenset({"baml", "model", "continuation_observed"})
_USAGE_FIELDS = frozenset(
    {"input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens"}
)


class RunContextConflict(ValueError):
    """A child environment already carried a different run identity."""


@dataclass(frozen=True)
class RunIdentity:
    """Identity facts validated by the launch owner and propagated unchanged."""

    hive: str
    bead: str | None
    driver: str
    provider: str
    manifest_digest: str

    def __post_init__(self) -> None:
        values = {
            "hive": self.hive,
            "driver": self.driver,
            "provider": self.provider,
            "manifest_digest": self.manifest_digest,
        }
        for name, value in values.items():
            if not isinstance(value, str) or not value:
                raise ValueError(f"run identity {name} must be a non-empty string")
        limits = {"hive": 512, "driver": 128, "provider": 128}
        for name, limit in limits.items():
            if len(values[name]) > limit:
                raise ValueError(f"run identity {name} exceeds {limit} characters")
        if self.bead == "":
            raise ValueError("run identity bead must be an exact id or None")
        if self.bead is not None and len(self.bead) > 256:
            raise ValueError("run identity bead exceeds 256 characters")
        digest = self.manifest_digest
        if len(digest) != 71 or not digest.startswith("sha256:"):
            raise ValueError("manifest_digest must be sha256:<64 lowercase hex characters>")
        try:
            int(digest[7:], 16)
        except ValueError as exc:
            raise ValueError(
                "manifest_digest must be sha256:<64 lowercase hex characters>"
            ) from exc
        if digest[7:] != digest[7:].lower():
            raise ValueError("manifest_digest must be sha256:<64 lowercase hex characters>")


@dataclass(frozen=True)
class RunJournalStatus:
    """Host-local observability coverage for one outer attempt."""

    run_id: str
    path: Path
    coverage: str
    last_revision: str | None
    dropped_records: int


def journal_root(identity: RunIdentity, *, base: Path | None = None) -> Path:
    """Return the host-local directory for *identity* without creating it."""

    root = Path(base) if base is not None else config.home() / "run-journals"
    root = root.expanduser().absolute()
    return root / registry.sanitize(identity.hive)


def journal_path(identity: RunIdentity, run_id: str, *, base: Path | None = None) -> Path:
    """Return the run-scoped JSONL path.  The opaque id is never parsed by readers."""

    return journal_root(identity, base=base) / f"{run_id}.jsonl"


@dataclass
class RunJournal:
    """Best-effort writer for one immutable outer launch attempt."""

    identity: RunIdentity
    run_id: str
    path: Path
    writer: str = WRITER_LOCAL_LOOP
    provider_continuation: str | None = None
    last_revision: str | None = None
    dropped_records: int = 0
    degraded: bool = False
    _diagnosed: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        if not self.run_id or len(self.run_id) > 256 or not _RUN_ID.fullmatch(self.run_id):
            raise ValueError("run_id must be a path-safe opaque token of 1..256 characters")
        if self.writer not in WRITERS:
            raise ValueError("writer is outside the v1 provenance allowlist")
        if not self.path.is_absolute():
            raise ValueError("run journal path must be absolute")

    @classmethod
    def create(
        cls,
        identity: RunIdentity,
        *,
        base: Path | None = None,
        run_id: str | None = None,
        writer: str = WRITER_LOCAL_LOOP,
    ) -> RunJournal:
        """Mint an attempt and try to create + seed its journal before process spawn."""

        attempt = run_id or f"run-{uuid.uuid4()}"
        writer = cls(
            identity=identity,
            run_id=attempt,
            path=journal_path(identity, attempt, base=base),
            writer=writer,
        )
        writer.append({"kind": "run.created", "phase": "planned"}, operation="create")
        return writer

    @classmethod
    def from_env(
        cls,
        inherited: Mapping[str, str] | None = None,
        *,
        writer: str,
    ) -> RunJournal:
        """Attach a direct observer to the exact outer context its parent propagated."""

        env = os.environ if inherited is None else inherited
        if env.get("BH_RUN_JOURNAL_VERSION") != VERSION:
            raise ValueError(f"BH_RUN_JOURNAL_VERSION must be {VERSION}")
        required = (
            "BH_RUN_JOURNAL_PATH",
            "BH_RUN_ID",
            "BH_RUN_HIVE",
            "BH_RUN_DRIVER",
            "BH_RUN_PROVIDER",
            "BH_RUN_MANIFEST_DIGEST",
        )
        missing = [name for name in required if not env.get(name)]
        if missing:
            raise ValueError(f"incomplete inherited run context: missing {', '.join(missing)}")
        path = Path(env["BH_RUN_JOURNAL_PATH"])
        if not path.is_absolute():
            raise ValueError("BH_RUN_JOURNAL_PATH must be absolute")
        if writer not in WRITERS:
            raise ValueError("writer is outside the v1 provenance allowlist")
        return cls(
            identity=RunIdentity(
                hive=env["BH_RUN_HIVE"],
                bead=env.get("BH_RUN_BEAD") or None,
                driver=env["BH_RUN_DRIVER"],
                provider=env["BH_RUN_PROVIDER"],
                manifest_digest=env["BH_RUN_MANIFEST_DIGEST"],
            ),
            run_id=env["BH_RUN_ID"],
            path=path,
            writer=writer,
        )

    @property
    def status(self) -> RunJournalStatus:
        return RunJournalStatus(
            run_id=self.run_id,
            path=self.path,
            coverage="degraded" if self.degraded else "complete",
            last_revision=self.last_revision,
            dropped_records=self.dropped_records,
        )

    def child_env(self, inherited: Mapping[str, str] | None = None) -> dict[str, str]:
        """Copy *inherited* and add v1 context, rejecting identity conflicts."""

        env = dict(os.environ if inherited is None else inherited)
        values: dict[str, str | None] = {
            "version": VERSION,
            "path": str(self.path.resolve()),
            "run_id": self.run_id,
            "hive": self.identity.hive,
            "bead": self.identity.bead,
            "driver": self.identity.driver,
            "provider": self.identity.provider,
            "manifest_digest": self.identity.manifest_digest,
        }
        for variable, field_name in ENV_FIELDS.items():
            expected = values[field_name]
            existing = env.get(variable)
            if expected is None:
                if existing not in (None, ""):
                    raise RunContextConflict(f"conflicting inherited {variable}")
                env.pop(variable, None)
                continue
            if existing is not None and existing != expected:
                raise RunContextConflict(f"conflicting inherited {variable}")
            env[variable] = expected
        return env

    def append(
        self,
        activity: Mapping[str, object],
        *,
        operation: str = "append",
        writer: str | None = None,
    ) -> bool:
        """Append one complete record, degrading instead of changing control flow."""

        revision = f"opaque:{uuid.uuid4()}"
        observing_writer = writer or self.writer
        record = {
            "version": VERSION,
            "source_revision": revision,
            "timestamp_ms": time.time_ns() // 1_000_000,
            "run_id": self.run_id,
            "hive": self.identity.hive,
            "bead": self.identity.bead,
            "driver": self.identity.driver,
            "provider": self.identity.provider,
            "manifest_digest": self.identity.manifest_digest,
            "provider_continuation": self.provider_continuation,
            "writer": observing_writer,
            "activity": dict(activity),
        }
        try:
            if observing_writer not in WRITERS:
                raise ValueError("writer is outside the v1 provenance allowlist")
            _validate_activity(activity)
            if self.provider_continuation == self.run_id:
                raise ValueError("provider continuation cannot alias the outer run id")
            encoded = (
                json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
            ).encode("utf-8")
            if len(encoded) > ATOMIC_LINE_BYTES:
                raise ValueError("record exceeds atomic journal line bound")
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(self.path.parent, 0o700)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.fchmod(fd, 0o600)
                written = os.write(fd, encoded)
                if written != len(encoded):
                    raise OSError("partial append")
            finally:
                os.close(fd)
        except Exception as exc:  # noqa: BLE001 - observability must remain outcome-neutral
            self._drop(operation, exc)
            return False
        self.last_revision = revision
        return True

    def _drop(self, operation: str, exc: Exception) -> None:
        self.degraded = True
        self.dropped_records += 1
        # One diagnostic is enough to make the sink loss visible; status retains the exact count.
        if self._diagnosed:
            return
        self._diagnosed = True
        _LOG.error(
            "run_journal_write_failed",
            run_id=self.run_id,
            hive=self.identity.hive,
            bead=self.identity.bead,
            operation=operation,
            exception_class=type(exc).__name__,
        )


def _validate_activity(activity: Mapping[str, object]) -> None:
    """Keep the writer an allowlist rather than a serialize-then-redact sink."""

    if not isinstance(activity, Mapping) or not isinstance(activity.get("kind"), str):
        raise ValueError("activity.kind is required")
    unknown = set(activity) - _ACTIVITY_FIELDS
    if unknown:
        raise ValueError("activity contains fields outside the v1 allowlist")
    nested = (
        ("process", _PROCESS_FIELDS),
        ("provider_fact", _PROVIDER_FIELDS),
        ("usage", _USAGE_FIELDS),
    )
    for field_name, allowed in nested:
        value = activity.get(field_name)
        if value is None:
            continue
        if not isinstance(value, Mapping) or set(value) - allowed:
            raise ValueError(f"activity.{field_name} contains fields outside the v1 allowlist")


def activity_outcome(classification) -> tuple[str, dict[str, int], float]:
    """Extract only allowlisted outcome/usage facts from a SeatRun classification."""

    outcome = str(classification.outcome)
    source = classification.seat_run or classification.envelope
    usage: dict[str, int] = {}
    cost = 0.0
    if source is not None:
        cost = max(float(getattr(source, "cost_usd", 0.0) or 0.0), 0.0)
        raw_usage = getattr(source, "usage", None)
        if raw_usage is not None:
            aliases = {
                "input_tokens": "input_tokens",
                "output_tokens": "output_tokens",
                "cache_read_tokens": "cache_read_tokens",
                "cache_creation_tokens": "cache_creation_tokens",
            }
            for target, attribute in aliases.items():
                value = (
                    raw_usage.get(attribute)
                    if isinstance(raw_usage, Mapping)
                    else getattr(raw_usage, attribute, None)
                )
                if isinstance(value, int) and value >= 0:
                    usage[target] = value
    return outcome, usage, cost
