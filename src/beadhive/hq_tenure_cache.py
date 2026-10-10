"""The elected-tenure cache (trust-profiles ADR section 3, bh-ofszz).

Before this cache every ``bh`` process re-read and re-validated the verified HQ configuration
snapshot — eleven loads and 423 YAML parses, about 8 s of a 10 s ``bh bd show`` (bh-qrsec) —
and a transient reader failure failed an unrelated local write closed (bh-kjxe9). The cache
moves both reads to the host daemon:

* **What is cached.** The verified configuration snapshot, keyed by the SQL binding it was read
  through and the reader's rollback floor (backend, generation, sequence, head), and the
  per-capability eligibility decision for each hive this host holds, with the configuration
  revision it was derived from. One file, :data:`STATE` under ``$BH_HOME/state``, owner-only.
* **Tenure.** :func:`current_policy` reads it from the host's resolved trust profile
  (:class:`beadhive.trust_profiles.Tenure`): ``open`` ``hq.tenure`` alone (default 24 h, never
  unbounded), ``trusted`` the role's ``failover_after``, ``verified`` the shorter of
  ``hq.tenure`` and ``failover_after``, ``attested`` the heartbeat TTL with no offline grace.
* **Refresh.** The daemon refreshes on ``hq.tenure_refresh`` (default ``failover_after / 4``,
  never longer than the tenure) and on every successful heartbeat (:func:`mark_heartbeat`,
  :class:`TenureRefresher`). A command never refreshes the cache synchronously: it reads it
  (:func:`read_snapshot`, :func:`cached_decision`). A command that finds no current cache —
  a fresh host, a stopped daemon, a floor another process advanced — falls back to one
  per-process live read and seeds the cache.
* **HQ unreachable.** Commands keep working from the cache until tenure lapses, printing one
  warning per hour (``HQ unreachable since …; tenure ends at …``). After the lapse ``publish``
  and ``place`` refuse in every profile; ``author`` and ``claim`` continue only under ``open``
  and ``trusted`` (:meth:`TenurePolicy.allows_offline`).
* **Revocation.** A cordon, retirement or quarantine published at HQ is learned at the next
  refresh: revocation latency is the refresh interval. Under ``attested`` the eligibility
  boundary keeps the fresh authoritative reread (:attr:`TenurePolicy.fresh_eligibility`), so the
  next command sees it.
* **Not cached.** The placement CAS, ``bh work submit`` / ``finish`` state pushes, authority
  writes and every fleet-configuration publication read their HQ state live; only the ordinary
  effective-configuration read and the ``author`` / ``claim`` eligibility decision consult this
  module (``tests/test_hq_tenure_cache.py`` holds that as an architecture test).

The module is a leaf: it imports :mod:`beadhive.trust_profiles`, :mod:`beadhive.capabilities`
and the config domain ports, and reaches the profile, failover and heartbeat constants late.
Every time it reads comes from an injected ``now`` (or :data:`clock`), so the daemon loop and
every lapse rule are tested with a fake clock and no sleeps.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import trust_profiles
from .capabilities import AUTHOR, CLAIM
from .modules.config.domain.ports import (
    FleetConfigDocument,
    FleetConfigSnapshot,
    ordered_documents_digest,
)

STATE = "hq-tenure-cache.json"
#: The last successful heartbeat this host sent (written by the heartbeat sender).
HEARTBEAT = "hq-tenure-heartbeat.json"
#: When the hourly "HQ unreachable" warning was last printed.
WARNED = "hq-tenure-warned.json"
SCHEMA = 1
#: One warning per hour while HQ is unreachable.
WARN_INTERVAL_S = 3600.0
#: ``BH_HQ_TENURE_CACHE=off`` (or ``hq.tenure_cache: false``) restores the per-process read.
ENV = "BH_HQ_TENURE_CACHE"
#: A live read that finds the reader unreachable is retried this many times in all.
RETRY_ATTEMPTS = 3
#: Backoff before each retry (bounded: the whole retry budget is these plus the connects).
RETRY_BACKOFF_S: tuple[float, ...] = (0.25, 0.5)
#: The capabilities whose decision a command reads from the cache while HQ is reachable;
#: ``execute`` and ``publish`` keep their live read and use the cache only while HQ is down.
CACHED_CAPABILITIES: tuple[str, ...] = (AUTHOR, CLAIM)
#: Placement's own capability, never a bead-write capability (refused after lapse everywhere).
PLACE = "place"

#: The time source. Tests replace it (or pass ``now``); nothing here sleeps on it.
clock: Callable[[], float] = time.time
#: The backoff sleeper for :func:`live_with_retry` (tests replace it with a recorder).
sleep: Callable[[float], None] = time.sleep


# ---- the policy a profile sets ---------------------------------------------------------------


@dataclass(frozen=True)
class TenurePolicy:
    """What the resolved trust profile makes of the cache on this host."""

    profile: str
    #: How long the cache keeps the host working without a successful refresh.
    tenure_s: float
    #: How often the daemon refreshes (``hq.tenure_refresh``).
    refresh_s: float
    #: Whether the cache keeps the host working while HQ is unreachable (not ``attested``).
    offline_grace: bool = True
    #: Capabilities that continue after the tenure lapses unrefreshed.
    after_lapse: tuple[str, ...] = ()
    #: ``attested``: the eligibility boundary rereads HQ on every command.
    fresh_eligibility: bool = False

    @property
    def stale_after(self) -> float:
        """A record older than this is not the daemon's current one: a command reads live once
        (one refresh interval, plus half of one for a tick that is in flight)."""
        return min(self.tenure_s, 1.5 * self.refresh_s)

    def continues_after_lapse(self, capability: str) -> bool:
        return capability in self.after_lapse

    def allows_offline(self, capability: str, *, lapsed: bool) -> bool:
        """Whether `capability` may proceed from the cache while HQ is unreachable.

        Within tenure every capability does when the profile has offline grace. After the lapse
        (or with no grace at all, ``attested``) only :attr:`after_lapse` does: ``author`` and
        ``claim`` under ``open`` and ``trusted``; ``publish`` and ``place`` never."""
        if capability == PLACE:
            return False
        if not lapsed and self.offline_grace:
            return True
        return self.continues_after_lapse(capability)

    def as_dict(self) -> dict:
        return {
            "profile": self.profile,
            "tenure_s": self.tenure_s,
            "refresh_s": self.refresh_s,
            "offline_grace": self.offline_grace,
            "after_lapse": list(self.after_lapse),
            "fresh_eligibility": self.fresh_eligibility,
        }


def policy_for(
    profile: trust_profiles.ResolvedProfile,
    *,
    failover_after_s: float,
    heartbeat_ttl_s: float | None = None,
    refresh_s: float | None = None,
) -> TenurePolicy:
    """The cache policy for a resolved profile, a hive's `failover_after_s` and the frame's
    heartbeat TTL. ``refresh_s`` is ``hq.tenure_refresh``; unset, a quarter of the shorter of
    ``failover_after`` and the tenure. It never exceeds the tenure."""
    tenure = float(profile.tenure.seconds(failover_after_s, heartbeat_ttl_s))
    if refresh_s is None:
        refresh_s = min(float(failover_after_s), tenure) / 4.0
    return TenurePolicy(
        profile=profile.name,
        tenure_s=tenure,
        refresh_s=min(float(refresh_s), tenure),
        offline_grace=profile.tenure.offline_grace,
        after_lapse=tuple(
            capability
            for capability in (AUTHOR, CLAIM, "execute", "publish", PLACE)
            if profile.continues_after_lapse(capability)
        ),
        fresh_eligibility=profile.tenure.kind == "ttl",
    )


def _section(settings: object, name: str) -> dict:
    value = settings.get(name) if isinstance(settings, Mapping) else None
    return value if isinstance(value, Mapping) else {}


def _positive(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not value > 0:
        return None
    return float(value)


def failover_after(host_cfg: Mapping) -> float:
    """``failover_after`` for this host's role set; a never-placed role set (and any doubt)
    takes the executor default, the longest window."""
    from .failover_policy import ROLE_FAILOVER_AFTER_S, failover_after_for

    host = _section(host_cfg, "host")
    roles = host.get("roles") or host.get("role") or "executor"
    try:
        value = failover_after_for(roles)
    except (TypeError, ValueError):
        value = None
    return float(value if value is not None else ROLE_FAILOVER_AFTER_S["executor"])


def current_policy(host_cfg: Mapping, *, profile=None) -> TenurePolicy:
    """The policy this host runs now: the learned trust profile with its host overrides
    (``hq.tenure`` among them), ``hq.tenure_refresh`` and the role's ``failover_after``."""
    from . import hq_trust_profile
    from .heartbeat_conformance import LEASE_DURATION_SECONDS

    resolved = profile if profile is not None else hq_trust_profile.resolved(dict(host_cfg))
    return policy_for(
        resolved,
        failover_after_s=failover_after(host_cfg),
        heartbeat_ttl_s=float(LEASE_DURATION_SECONDS),
        refresh_s=_positive(_section(host_cfg, "hq").get("tenure_refresh")),
    )


def enabled(host_cfg: Mapping) -> bool:
    """Whether the cache is on for this host (on by default; ``hq.tenure_cache: false`` or
    ``BH_HQ_TENURE_CACHE=off`` turns it off)."""
    raw = os.environ.get(ENV, "").strip().lower()
    if raw in ("0", "off", "false", "no"):
        return False
    return _section(host_cfg, "hq").get("tenure_cache", True) is not False


def binding_key(host_cfg: Mapping) -> str:
    """The SQL binding a record was read through: a record from any other binding (another
    reader, another backend, another beadyard pin) is never served."""
    hq = _section(host_cfg, "hq")
    payload = {"sql": hq.get("sql") or {}, "beadyard_id": hq.get("beadyard_id")}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def endpoint(host_cfg: Mapping) -> str:
    reader = _section(_section(_section(host_cfg, "hq"), "sql"), "reader")
    return f"{reader.get('host') or '?'}:{reader.get('port') or 3308}"


def read_floor(host_cfg: Mapping) -> dict:
    """The reader's rollback floor (``hq.sql.floor_path``): ``{sequence, head}`` or ``{}``."""
    path = _section(_section(host_cfg, "hq"), "sql").get("floor_path")
    if not path:
        return {}
    try:
        floor = json.loads(Path(path).read_text())
        return {"sequence": int(floor["sequence"]), "head": str(floor["head"])}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


# ---- the record ------------------------------------------------------------------------------


@dataclass(frozen=True)
class HiveDecisions:
    """Per-capability eligibility for one hive, as the daemon last derived it."""

    host_id: str
    decided_at: float
    config_revision: str
    #: ``capability -> ((predicate, holds), ...)`` (the decision narrowed to the capability).
    capabilities: dict[str, tuple[tuple[str, bool], ...]] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "host_id": self.host_id,
            "decided_at": self.decided_at,
            "config_revision": self.config_revision,
            "capabilities": {
                name: [[predicate, bool(holds)] for predicate, holds in predicates]
                for name, predicates in self.capabilities.items()
            },
        }

    @classmethod
    def from_dict(cls, value: Mapping) -> HiveDecisions:
        return cls(
            str(value["host_id"]),
            float(value["decided_at"]),
            str(value["config_revision"]),
            {
                str(name): tuple((str(p), bool(h)) for p, h in predicates)
                for name, predicates in dict(value["capabilities"]).items()
            },
        )


@dataclass(frozen=True)
class TenureRecord:
    binding: str
    snapshot: FleetConfigSnapshot
    floor: dict
    #: The last successful HQ read (the tenure clock starts here).
    refreshed_at: float
    #: The last attempt to read HQ, successful or not.
    last_attempt_at: float
    #: Set while the daemon's reads fail: when they started failing.
    unreachable_since: float | None = None
    hives: dict[str, HiveDecisions] = field(default_factory=dict)
    #: ``daemon`` (a refresh) or ``command`` (a per-process fallback read seeded it).
    source: str = "daemon"
    #: The policy the writer ran (informational: readers apply their own current policy).
    policy: dict = field(default_factory=dict)

    def expires_at(self, policy: TenurePolicy) -> float:
        return self.refreshed_at + policy.tenure_s

    def lapsed(self, policy: TenurePolicy, now: float) -> bool:
        return now >= self.expires_at(policy)

    def fresh(self, policy: TenurePolicy, now: float) -> bool:
        return self.refreshed_at <= now < self.refreshed_at + policy.stale_after

    def daemon_offline(self, policy: TenurePolicy, now: float) -> bool:
        """The daemon is keeping this record and its last read found HQ unreachable."""
        return (
            self.unreachable_since is not None
            and self.last_attempt_at <= now < self.last_attempt_at + policy.stale_after
        )

    def as_dict(self) -> dict:
        snapshot = self.snapshot
        return {
            "schema": SCHEMA,
            "binding": self.binding,
            "snapshot": {
                "backend_identity": snapshot.backend_identity,
                "commit_revision": snapshot.commit_revision,
                "generation": snapshot.generation,
                "digest": ordered_documents_digest(snapshot.documents),
                "documents": [[item.path, item.content] for item in snapshot.documents],
            },
            "floor": dict(self.floor),
            "refreshed_at": self.refreshed_at,
            "last_attempt_at": self.last_attempt_at,
            "unreachable_since": self.unreachable_since,
            "hives": {prefix: entry.as_dict() for prefix, entry in sorted(self.hives.items())},
            "source": self.source,
            "policy": dict(self.policy),
        }

    @classmethod
    def from_dict(cls, value: Mapping) -> TenureRecord:
        if value.get("schema") != SCHEMA:
            raise ValueError("tenure cache schema mismatch")
        raw = value["snapshot"]
        documents = tuple(FleetConfigDocument(str(p), str(c)) for p, c in raw["documents"])
        if ordered_documents_digest(documents) != raw["digest"]:
            raise ValueError("tenure cache documents digest mismatch")
        refreshed = float(value["refreshed_at"])
        snapshot = FleetConfigSnapshot(
            backend_identity=str(raw["backend_identity"]),
            commit_revision=str(raw["commit_revision"]),
            generation=str(raw["generation"]),
            fetched_at=refreshed,
            # The snapshot's own validity is the cache's: readers apply the lapse rules.
            valid_until=refreshed + 1.0,
            documents=documents,
        )
        since = value.get("unreachable_since")
        return cls(
            binding=str(value["binding"]),
            snapshot=snapshot,
            floor=dict(value.get("floor") or {}),
            refreshed_at=refreshed,
            last_attempt_at=float(value["last_attempt_at"]),
            unreachable_since=None if since is None else float(since),
            hives={
                str(prefix): HiveDecisions.from_dict(entry)
                for prefix, entry in dict(value.get("hives") or {}).items()
            },
            source=str(value.get("source", "daemon")),
            policy=dict(value.get("policy") or {}),
        )

    def served(self, policy: TenurePolicy) -> FleetConfigSnapshot:
        """The snapshot as a command receives it: valid until the tenure ends."""
        return replace(self.snapshot, valid_until=self.expires_at(policy))


# ---- storage ---------------------------------------------------------------------------------

_memo: dict[str, tuple[tuple, TenureRecord | None]] = {}
#: ``binding -> snapshot`` a per-process live read produced (used when the cache is unwritable).
_live: dict[str, FleetConfigSnapshot] = {}


def reset() -> None:
    _memo.clear()
    _live.clear()


def invalidate() -> None:
    """Drop the record (and this process's memo) after this host published a new HQ
    revision: the next read on the host is live and reseeds. Never raises."""
    reset()
    target = path()
    if target is not None:
        try:
            target.unlink()
        except OSError:
            pass


def state_dir() -> Path | None:
    try:
        # Resolved late, like hq_trust_profile: config's store reads this module.
        return importlib.import_module("beadhive.config").home() / "state"
    except Exception:  # noqa: BLE001 - no home: no cache
        return None


def path() -> Path | None:
    directory = state_dir()
    return None if directory is None else directory / STATE


def _owner_only(target: Path) -> os.stat_result | None:
    try:
        stat = os.lstat(target)
    except OSError:
        return None
    import stat as _stat

    if (
        not _stat.S_ISREG(stat.st_mode)
        or stat.st_mode & 0o077
        or (hasattr(os, "getuid") and stat.st_uid != os.getuid())
    ):
        return None
    return stat


def load(target: Path | None = None) -> TenureRecord | None:
    """The record, or ``None`` (absent, unreadable, foreign-owned, group/world accessible or
    corrupt: a doubtful record is never served). Memoized on the file's identity."""
    target = target if target is not None else path()
    if target is None:
        return None
    stat = _owner_only(target)
    if stat is None:
        _memo.pop(str(target), None)
        return None
    key = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
    cached = _memo.get(str(target))
    if cached is not None and cached[0] == key:
        return cached[1]
    try:
        record = TenureRecord.from_dict(json.loads(target.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError):
        record = None
    _memo[str(target)] = (key, record)
    return record


def _write_json(target: Path, payload: dict) -> bool:
    temporary = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    try:
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
        os.replace(temporary, target)
        return True
    except OSError:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        return False


def save(record: TenureRecord, target: Path | None = None) -> bool:
    """Atomically replace the record (owner-only). Never raises."""
    target = target if target is not None else path()
    if target is None:
        return False
    return _write_json(target, record.as_dict())


def _read_json(name: str) -> dict:
    directory = state_dir()
    if directory is None or _owner_only(directory / name) is None:
        return {}
    try:
        value = json.loads((directory / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def mark_heartbeat(now: float | None = None) -> bool:
    """Record a successful heartbeat; the daemon refreshes the cache when it sees one."""
    directory = state_dir()
    if directory is None:
        return False
    return _write_json(directory / HEARTBEAT, {"at": clock() if now is None else now})


def last_heartbeat() -> float | None:
    value = _read_json(HEARTBEAT).get("at")
    return float(value) if isinstance(value, int | float) else None


# ---- the hourly warning ----------------------------------------------------------------------


def _stamp(seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))


def warn_unreachable(
    record: TenureRecord,
    policy: TenurePolicy,
    now: float,
    *,
    since: float | None = None,
    emit: Callable[[str], None] | None = None,
) -> bool:
    """Print ``HQ unreachable since …; tenure ends at …`` at most once per hour (one shared
    stamp for every process on the host). Returns whether it printed."""
    previous = _read_json(WARNED).get("at")
    if isinstance(previous, int | float) and 0 <= now - previous < WARN_INTERVAL_S:
        return False
    since = since if since is not None else (record.unreachable_since or now)
    ends = record.expires_at(policy)
    state = "ended" if now >= ends else "ends"
    message = (
        f"⚠ HQ unreachable since {_stamp(since)}; tenure {state} at {_stamp(ends)} "
        f"(trust profile {policy.profile}; working from the elected-tenure cache)"
    )
    if now >= ends:
        lapse = ", ".join(policy.after_lapse) or "nothing"
        message += f"; after the lapse only {lapse} continues"
    (emit or (lambda line: print(line, file=sys.stderr)))(message)
    directory = state_dir()
    if directory is not None:
        _write_json(directory / WARNED, {"at": now})
    return True


# ---- the command side: snapshot ---------------------------------------------------------------


def unreachable(error: BaseException) -> bool:
    """A read failure that means "HQ is not reachable right now" (a retry or the cache may
    help), not "HQ answered and refused" (which always fails closed)."""
    from .hq_sql_config import SqlConfigUnavailable

    return isinstance(error, SqlConfigUnavailable)


def live_with_retry(
    load_live: Callable[[], FleetConfigSnapshot],
    *,
    attempts: int = RETRY_ATTEMPTS,
    backoff: tuple[float, ...] = RETRY_BACKOFF_S,
) -> FleetConfigSnapshot:
    """One live read, retried with bounded backoff while the reader is unreachable."""
    for attempt in range(attempts):
        try:
            return load_live()
        except Exception as exc:
            if not unreachable(exc) or attempt == attempts - 1:
                raise
            sleep(backoff[min(attempt, len(backoff) - 1)] if backoff else 0.0)
    raise AssertionError("unreachable")  # pragma: no cover - the loop always returns/raises


def _current(record: TenureRecord | None, host_cfg: Mapping) -> TenureRecord | None:
    """`record` when it was read through this binding, else ``None``."""
    return record if record is not None and record.binding == binding_key(host_cfg) else None


def _offline(
    record: TenureRecord,
    policy: TenurePolicy,
    now: float,
    host_cfg: Mapping,
    *,
    error: BaseException | None,
    attempts: int,
) -> FleetConfigSnapshot:
    lapsed = record.lapsed(policy, now) or not policy.offline_grace
    if not policy.allows_offline(AUTHOR, lapsed=lapsed):
        raise _unavailable(host_cfg, attempts, record=record, policy=policy, now=now) from error
    warn_unreachable(record, policy, now)
    return record.served(policy)


def _unavailable(
    host_cfg: Mapping,
    attempts: int,
    *,
    record: TenureRecord | None = None,
    policy: TenurePolicy | None = None,
    now: float | None = None,
):
    from .hq_sql_config import SqlConfigUnavailable

    tried = f"after {attempts} attempt{'s' if attempts != 1 else ''}" if attempts else "unread"
    if record is None or policy is None:
        cache = "no elected-tenure cache to fall back on"
    elif not policy.offline_grace:
        cache = f"the {policy.profile} trust profile has no offline grace"
    else:
        cache = (
            f"tenure lapsed at {_stamp(record.expires_at(policy))} under the "
            f"{policy.profile} trust profile"
        )
    return SqlConfigUnavailable(
        f"verified HQ config connection unavailable at {endpoint(host_cfg)} {tried}; {cache}"
    )


def read_snapshot(
    host_cfg: Mapping,
    load_live: Callable[[], FleetConfigSnapshot],
    *,
    now: float | None = None,
    policy: TenurePolicy | None = None,
) -> FleetConfigSnapshot:
    """The verified configuration snapshot for an ordinary command read.

    1. A current record (this binding, the reader's floor unchanged, refreshed within one
       interval) is served: no HQ connection.
    2. While the daemon reports HQ unreachable, the record is served within tenure (and after
       the lapse only where ``author`` continues), with the hourly warning: no HQ connection.
    3. Otherwise one live read (retried with bounded backoff while unreachable) seeds the cache;
       if it finds HQ unreachable the record is served by rule 2, else the error names the
       endpoint, the attempts and why the cache could not serve.
    """
    now = clock() if now is None else now
    if not enabled(host_cfg):
        return load_live()
    policy = policy if policy is not None else current_policy(host_cfg)
    key = binding_key(host_cfg)
    record = _current(load(), host_cfg)
    if record is not None:
        if record.fresh(policy, now) and record.floor == read_floor(host_cfg):
            return record.served(policy)
        if record.daemon_offline(policy, now):
            return _offline(record, policy, now, host_cfg, error=None, attempts=0)
    memo = _live.get(key)
    if memo is not None and memo.fetched_at <= now < memo.valid_until:
        return memo
    try:
        snapshot = live_with_retry(load_live)
    except Exception as exc:
        if not unreachable(exc):
            raise
        if record is None:
            raise _unavailable(host_cfg, RETRY_ATTEMPTS) from exc
        return _offline(record, policy, now, host_cfg, error=exc, attempts=RETRY_ATTEMPTS)
    if isinstance(snapshot, FleetConfigSnapshot):
        _live[key] = snapshot
        seed(host_cfg, snapshot, now=now, policy=policy, previous=record)
    return snapshot


def seed(
    host_cfg: Mapping,
    snapshot: FleetConfigSnapshot,
    *,
    now: float,
    policy: TenurePolicy,
    previous: TenureRecord | None = None,
    hives: dict[str, HiveDecisions] | None = None,
    source: str = "command",
) -> TenureRecord | None:
    """Write a record for a verified `snapshot` just read live. Keeps the previous record's
    hive decisions unless `hives` is given. The floor is read back after the load: when it
    does not name this snapshot (a concurrent newer read), nothing is written."""
    if not isinstance(snapshot, FleetConfigSnapshot):
        return None
    floor = read_floor(host_cfg)
    if floor and floor.get("head") != snapshot.commit_revision:
        return None
    record = TenureRecord(
        binding=binding_key(host_cfg),
        snapshot=snapshot,
        floor=floor,
        refreshed_at=now,
        last_attempt_at=now,
        unreachable_since=None,
        hives=dict(previous.hives if previous is not None and hives is None else hives or {}),
        source=source,
        policy=policy.as_dict(),
    )
    _clear_warned()
    return record if save(record) else None


def _clear_warned() -> None:
    directory = state_dir()
    if directory is not None:
        try:
            (directory / WARNED).unlink()
        except OSError:
            pass


# ---- the command side: eligibility -----------------------------------------------------------


Predicates = tuple[tuple[str, bool], ...]
#: The refusal :func:`offline_decision` returns once tenure no longer covers a capability.
LAPSED: Predicates = (("tenure_current", False),)


def cached_decision(
    host_id: str,
    prefix: str,
    capability: str,
    host_cfg: Mapping,
    *,
    now: float | None = None,
    policy: TenurePolicy | None = None,
) -> Predicates | None:
    """The daemon's current decision for `capability` on `prefix` (its predicates), or
    ``None`` (read live).

    Served only for :data:`CACHED_CAPABILITIES`, only from a record refreshed within one
    interval, and never under ``attested`` (the fresh authoritative reread stays)."""
    now = clock() if now is None else now
    if capability not in CACHED_CAPABILITIES or not enabled(host_cfg):
        return None
    policy = policy if policy is not None else current_policy(host_cfg)
    if policy.fresh_eligibility:
        return None
    record = _current(load(), host_cfg)
    entry = record.hives.get(prefix) if record is not None else None
    if (
        entry is None
        or entry.host_id != host_id
        or capability not in entry.capabilities
        or not entry.decided_at <= now < entry.decided_at + policy.stale_after
        or entry.config_revision != record.snapshot.commit_revision
    ):
        return None
    return entry.capabilities[capability]


def offline_decision(
    host_id: str,
    prefix: str,
    capability: str,
    host_cfg: Mapping,
    *,
    now: float | None = None,
    policy: TenurePolicy | None = None,
) -> Predicates | None:
    """The decision (its predicates) for `capability` on `prefix` when the live eligibility
    read found HQ unreachable, or ``None`` (no cached decision: the live refusal stands).

    Within tenure the cached decision applies (one warning per hour). After the lapse — or at
    once under ``attested`` — only ``author`` and ``claim`` under ``open`` and ``trusted``
    continue; anything else is refused with :data:`LAPSED` (``tenure_current``)."""
    now = clock() if now is None else now
    if not enabled(host_cfg):
        return None
    policy = policy if policy is not None else current_policy(host_cfg)
    record = _current(load(), host_cfg)
    entry = record.hives.get(prefix) if record is not None else None
    if entry is None or entry.host_id != host_id or capability not in entry.capabilities:
        return None
    lapsed = record.lapsed(policy, now) or not policy.offline_grace
    if not policy.allows_offline(capability, lapsed=lapsed):
        return LAPSED
    warn_unreachable(record, policy, now)
    return entry.capabilities[capability]


def authority_unavailable(decision) -> bool:
    """Whether a live decision refused only because HQ could not be read."""
    return decision is not None and ("authority_available", False) in tuple(decision.predicates)


# ---- the daemon side --------------------------------------------------------------------------


@dataclass
class RefreshOutcome:
    ok: bool
    reason: str
    at: float
    detail: str = ""


Decide = Callable[[Mapping, FleetConfigSnapshot, float], dict[str, HiveDecisions]]


@dataclass
class TenureRefresher:
    """Refreshes the record from HQ: on the timer and on each successful heartbeat.

    Every collaborator is injected — `host_cfg` (re-read each refresh), `load_live` (the
    verified snapshot read), `decide` (per-hive decisions) and `clock` — so the loop runs
    under a fake clock in tests."""

    host_cfg: Callable[[], Mapping]
    load_live: Callable[[Mapping], FleetConfigSnapshot]
    decide: Decide | None = None
    clock: Callable[[], float] = time.time
    next_due: float = 0.0
    heartbeat_seen: float | None = None
    last: RefreshOutcome | None = None
    refreshes: int = 0
    failures: int = 0

    def _heartbeat_advanced(self) -> bool:
        beat = last_heartbeat()
        if beat is None or beat == self.heartbeat_seen:
            return False
        first = self.heartbeat_seen is None and self.refreshes == 0 and self.failures == 0
        self.heartbeat_seen = beat
        return not first

    def due(self, now: float) -> str:
        """``timer`` / ``heartbeat`` when a refresh is due now, else ``""``."""
        if self._heartbeat_advanced():
            return "heartbeat"
        return "timer" if now >= self.next_due else ""

    def tick(self) -> RefreshOutcome | None:
        now = self.clock()
        reason = self.due(now)
        return self.refresh(reason=reason, now=now) if reason else None

    def refresh(self, *, reason: str = "timer", now: float | None = None) -> RefreshOutcome:
        now = self.clock() if now is None else now
        host = self.host_cfg()
        policy = current_policy(host)
        previous = _current(load(), host)
        self.next_due = now + policy.refresh_s
        try:
            snapshot = self.load_live(host)
        except Exception as exc:  # noqa: BLE001 - a failed refresh never stops the loop
            self.failures += 1
            if previous is not None:
                save(
                    replace(
                        previous,
                        last_attempt_at=now,
                        unreachable_since=previous.unreachable_since or now,
                    )
                )
            self.last = RefreshOutcome(False, reason, now, type(exc).__name__)
            return self.last
        # Seed the snapshot first (keeping the previous decisions), so the per-hive reads below
        # see this revision through the ordinary configuration read.
        record = seed(host, snapshot, now=now, policy=policy, previous=previous, source="daemon")
        self.refreshes += 1
        self.last = RefreshOutcome(True, reason, now)
        if self.decide is not None and record is not None:
            try:
                fresh = self.decide(host, snapshot, now)
            except Exception as exc:  # noqa: BLE001 - keep the previous decisions
                self.last.detail = f"decide: {type(exc).__name__}"
            else:
                save(replace(record, hives={**record.hives, **fresh}))
        return self.last

    def run(self, wait: Callable[[float], bool], *, poll_s: float | None = None) -> None:
        """Tick until ``wait(seconds)`` returns True (an Event's ``wait`` in the daemon thread,
        a fake timer that advances the fake clock in tests). Polls at most every `poll_s`
        (default: a sixth of the refresh interval, capped to a minute) so a heartbeat is seen
        promptly."""
        while True:
            self.tick()
            if poll_s is not None:
                interval = poll_s
            else:
                refresh = current_policy(self.host_cfg()).refresh_s
                interval = min(60.0, max(1.0, refresh / 6.0))
            if wait(interval):
                return


def status(host_cfg: Mapping, *, now: float | None = None) -> dict:
    """The cache as ``bh hq why`` / doctor can report it."""
    now = clock() if now is None else now
    policy = current_policy(host_cfg)
    record = _current(load(), host_cfg)
    if record is None:
        return {"cached": False, "policy": policy.as_dict()}
    return {
        "cached": True,
        "policy": policy.as_dict(),
        "revision": record.snapshot.commit_revision,
        "source": record.source,
        "refreshed_at": _stamp(record.refreshed_at),
        "expires_at": _stamp(record.expires_at(policy)),
        "fresh": record.fresh(policy, now),
        "lapsed": record.lapsed(policy, now),
        "unreachable_since": (
            None if record.unreachable_since is None else _stamp(record.unreachable_since)
        ),
        "hives": sorted(record.hives),
    }
