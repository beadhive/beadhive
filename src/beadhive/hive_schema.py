"""``hives/<provider>/<org>/<repo>.yaml`` — each registered hive's observed bd schema version,
recorded in Factory HQ (`bh-wnly`).

Mirrors :mod:`beadhive.hosts` (``hosts/<host_id>.yaml``, `bh-ytbb.3`) deliberately: HQ already
carries one small per-HOST manifest file per host; this is the same shape for per-HIVE facts —
schema + read/write/validate only, ``extra="forbid"``, a raise-loud ``load`` plus a soft
``try_load`` for callers that just want "no record yet" instead of a caught exception.

WHY A SEPARATE FILE, NOT A NEW FIELD ON ``managed_repos`` ENTRIES: a hive's `managed_repos`
entry (`registry.py`) is IDENTITY data, rewritten WHOLESALE by `registry.register()` from only
its own known fields (`prefix`/`kind`/`upstream`/`furnish`/`contribution`) — any other key
living on that same mapping would be silently dropped the next time that hive re-registers.
This module's record is PROBE-REFRESHED data on a different, more frequent cadence (see
`refresh` below) — exactly why `hosts.py` didn't stuff host facts into `managed_repos` either.

WHAT "REFRESH" MEANS HERE, AND WHY `bh doctor`: schema version is a property of the STORE and
moves forward whenever a newer bd writes to it (this bead's own hard part) — anything recorded
here is a snapshot, not a live read. `bh doctor` is the refresh trigger this bead wires up:
it already walks every registered hive with a local checkout (`doctor._data_warnings`), already
does per-hive subprocess work in that same loop (`git ls-files`, grant checks), and is the
surface `bh-areg.3`'s own dolt_health reporting already uses for exactly this class of fact — so
adding one more local, no-network probe per hive costs nothing new in kind, only degree.
`bh hive sync` and "the end of any `bh work` verb that wrote" are both real, named alternatives
(this bead's own text) deliberately NOT wired up here: `bh hive sync` runs per-hive, not
fleet-wide, so it would leave every OTHER hive's record equally stale between syncs; and wiring
every write-shaped `bh work` verb would touch many call sites for a fact that changes rarely
(schema migrations ship far less often than beads are written) — scope creep this bead's own
"detection and reporting only" boundary argues against. Revisit if `bh doctor` alone proves too
infrequent in practice.

STALENESS (AC4 — "a stale value that is TRUSTED is worse than no value at all"): this module
never manufactures a placeholder. A record only ever exists because it was ACTUALLY measured
(`refresh` refuses to write when the probe failed) — so any record's `schema_version` was true
AT `observed_at`. It can still be stale (the real store may have advanced further since, e.g. a
different host wrote to it), which is exactly why every reader gets `observed_at` back, not just
the bare integer — `is_stale` names a default staleness bound a caller can apply to decide
whether to still show a green "no known skew" or fall back to "last confirmed N days ago,
unverified since". Silence is never the honest answer to "the record might be stale"; a caller
that reads a record must surface its age, not just its number.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from ruamel.yaml import YAML

from . import dolt_health

_yaml = YAML()
_yaml.indent(mapping=2, sequence=4, offset=2)

# `_yaml` is one shared, stateful `ruamel.yaml.YAML()` instance — `load()`/`dump()` mutate its
# internal parser/composer state, and bh-ti7ws now calls `refresh`/`save`/`load` from a
# ThreadPoolExecutor (one hive per worker, doctor.py's `_bd_schema_skew_warnings`). Concurrent
# calls corrupt each other's parse (bh-3qo60 measured this exact class of bug on config.py's
# equivalent singleton: 147/200 failures at 16 threads with no lock, 0/200 with one). One lock
# around every use, mirroring config.py's fix — each hive still writes its OWN manifest file
# (`manifest_path`), so the lock only serializes the in-memory YAML call, not the I/O.
_yaml_lock = threading.Lock()

# How long a recorded observation is treated as still-informative for an "all clear" read
# (`is_stale`). Deliberately generous: schema migrations ship far less often than a hive is
# touched, so a multi-day bound catches genuine long-idle drift (the fleet-skew scenario this
# bead is about) without nagging on ordinary day-to-day gaps between `bh doctor` runs. Not a
# new config key (mirrors `dolt_health`'s own module docstring precedent of adding zero new
# `DoltConfig` keys) — a hardcoded, documented constant a future bead can promote to config if
# real usage shows the bound is wrong.
DEFAULT_STALE_AFTER_SECONDS = 7 * 24 * 3600.0  # 7 days

_TIMESTAMP_FMT = "%Y-%m-%dT%H:%M:%SZ"


class HiveSchemaRecord(BaseModel):
    """One hive's last-OBSERVED bd schema version — ``hives/<provider>/<org>/<repo>.yaml`` in
    HQ. Every field here was measured, never guessed (see module docstring)."""

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(..., description="Repo-group path segment (registry.py's `provider`).")
    org: str = Field(..., description="Org segment of the hive's identity triplet.")
    repo: str = Field(..., description="Repo segment of the hive's identity triplet.")
    schema_version: int = Field(
        ..., description="The real bd/Dolt migration-count integer (e.g. 59), not a decoy field."
    )
    dolt_mode: str = Field(
        "", description="bd's reported engine mode at observation time (embedded/server/...)."
    )
    observed_at: str = Field(
        ..., description="UTC timestamp (see _TIMESTAMP_FMT) the probe actually ran."
    )
    observed_by_host: str = Field(
        "", description="host_id (beadhive.host.host_id()) of the host that ran the probe."
    )
    observed_by_bd_version: str = Field(
        "", description="`bd --version` output of the bd binary that produced this observation."
    )


def hives_dir(hq_dir: Path) -> Path:
    """The ``hives/`` directory under a given HQ store root. Purely a path computation — mirrors
    `hosts.hosts_dir`; does not create it (`save` does, on write)."""
    return hq_dir / "hives"


def manifest_path(hq_dir: Path, provider: str, org: str, repo: str) -> Path:
    """Where one hive's schema record lives: ``hives/<provider>/<org>/<repo>.yaml`` — the
    identity triplet as a real subpath (not a flattened/sanitized filename), matching how a
    hive's own checkout is already laid out elsewhere (`registry.hive_dir`)."""
    return hives_dir(hq_dir) / provider / org / f"{repo}.yaml"


def save(hq_dir: Path, record: HiveSchemaRecord) -> Path:
    """Write ``record`` to its manifest path, creating parent directories as needed. `record` is
    already-validated (a `HiveSchemaRecord` instance cannot exist in an invalid shape)."""
    p = manifest_path(hq_dir, record.provider, record.org, record.repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w") as f, _yaml_lock:
        _yaml.dump(record.model_dump(mode="json"), f)
    return p


class ManifestError(ValueError):
    """A hive schema manifest failed validation on read — names the offending key(s), mirroring
    `hosts.ManifestError`'s "fails loudly" contract."""


def load(hq_dir: Path, provider: str, org: str, repo: str) -> HiveSchemaRecord:
    """Read + VALIDATE a hive's schema manifest. Raises ``FileNotFoundError`` when none has
    ever been recorded (see `try_load` for a soft variant); raises `ManifestError` on a
    malformed file — never a silent partial read."""
    p = manifest_path(hq_dir, provider, org, repo)
    if not p.exists():
        raise FileNotFoundError(f"no hive schema record for {provider}/{org}/{repo} at {p}")
    text = p.read_text()
    with _yaml_lock:
        raw = _yaml.load(text) or {}
    try:
        return HiveSchemaRecord.model_validate(raw)
    except ValidationError as exc:
        lines = [f"malformed hive schema record at {p}:"]
        for err in exc.errors():
            dotted = ".".join(str(part) for part in err["loc"]) or "<root>"
            lines.append(f"  `{dotted}`: {err['msg']}")
        raise ManifestError("\n".join(lines)) from exc


def try_load(hq_dir: Path, provider: str, org: str, repo: str) -> HiveSchemaRecord | None:
    """`load`, but ``None`` instead of raising when no record exists yet — the common case for
    a hive `bh doctor` (or whatever refresh trigger) hasn't reached yet. A malformed file still
    raises `ManifestError`: absence and corruption are different problems and must read
    differently to a caller."""
    try:
        return load(hq_dir, provider, org, repo)
    except FileNotFoundError:
        return None


def age_seconds(record: HiveSchemaRecord, *, now: datetime | None = None) -> float:
    """Seconds since `record.observed_at`. ``inf`` on an unparseable timestamp — never a
    silent 0, which would masquerade as "just observed"."""
    try:
        observed = datetime.strptime(record.observed_at, _TIMESTAMP_FMT).replace(tzinfo=UTC)
    except ValueError:
        return float("inf")
    return ((now or datetime.now(UTC)) - observed).total_seconds()


def is_stale(
    record: HiveSchemaRecord | None, *, max_age: float = DEFAULT_STALE_AFTER_SECONDS
) -> bool:
    """True when there is no record at all, or its `observed_at` is older than `max_age` — the
    AC4 gate: a caller must not present a stale/absent record as a confirmed "no skew" (see
    module docstring)."""
    if record is None:
        return True
    return age_seconds(record) > max_age


def refresh(
    hive_dir: Path, provider: str, org: str, repo: str, *, hq_dir: Path, dolt_mode: str | None
) -> HiveSchemaRecord | None:
    """Probe `hive_dir`'s real schema version and persist it — the one place a
    `HiveSchemaRecord` gets written. Returns the new record, or ``None`` (and writes NOTHING)
    when the probe itself failed: this module never manufactures a placeholder value (module
    docstring's staleness contract) — a failed refresh must leave the PRIOR record (if any) as
    the last known-true observation, not overwrite it with a guess."""
    from . import host

    probed = dolt_health.probe_raw_schema_version(hive_dir, dolt_mode=dolt_mode)
    if probed.version is None:
        return None
    try:
        observed_by_host = host.host_id()
    except Exception:
        observed_by_host = ""
    probe_timeout = dolt_health.DEFAULT_SCHEMA_PROBE_TIMEOUT
    bd_version = dolt_health._local_bd_version_string(timeout=probe_timeout)
    record = HiveSchemaRecord(
        provider=provider,
        org=org,
        repo=repo,
        schema_version=probed.version,
        dolt_mode=dolt_mode or "",
        observed_at=datetime.now(UTC).strftime(_TIMESTAMP_FMT),
        observed_by_host=observed_by_host,
        observed_by_bd_version=bd_version or "",
    )
    save(hq_dir, record)
    return record
