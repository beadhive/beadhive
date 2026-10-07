"""Per-hive state/work pairing policy, read from the hive's own Dolt data (M14 D8/D11).

Condition 18 of ``docs/design/hive-writer-partitioning-adr.md`` and its decision record
``docs/spikes/bh-55vvh-state-work-pairing.md`` (M14) put every pairing and reclaim switch in the
hive's **own** data — bd ``config`` rows namespaced ``bh.`` — never in ``host.yaml``, fleet config
or the environment:

* unknown host keys broke 0.21.3 readers;
* a fleet-config publish fences every frame until authority renewal;
* a per-frame value could leave frames of one hive disagreeing about the invariant.

bd's ``config`` table is a versioned Dolt table (it is not in ``dolt_ignore``; verified against bd
1.3.0 for M14b), so the rows travel with ``main`` to every frame and to a new primary, and the
``bh_policy`` fallback table D8 describes is not needed.

**Everything is opt-in and off by default** (the operator's global rule, 2026-10-06). Every key
has a configurable default; sub-keys take effect only while their master switch is on. Values are
validated on read: an unknown or ill-typed value is **refused, never clamped**, reads as the key's
off state, and is reported (``bh doctor`` / ``bh hive policy list``).

Typer-free: :func:`read` is a pure parse over one ``bd config list --json`` read, and
:func:`parse` is the testable core.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

#: The bd config namespace every key below lives under (``bh.pairing.enabled``, ...).
PREFIX = "bh."

RECLAIM_MODES = ("off", "report", "apply")
SIGNATURE_POLICIES = ("off", "strict")
DEFAULT_SECRET_SCAN = "gitleaks git --no-banner --log-opts={range}"

_REMOTE_RE = re.compile(r"^[A-Za-z0-9._-]*$")


def _bool(raw: str) -> bool:
    value = raw.strip().lower()
    if value in ("true", "1", "yes", "on"):
        return True
    if value in ("false", "0", "no", "off"):
        return False
    raise ValueError(f"expected a boolean, got {raw!r}")


def _nonneg_int(raw: str) -> int:
    value = raw.strip()
    if not value.isdigit():
        raise ValueError(f"expected an integer >= 0, got {raw!r}")
    return int(value)


def _choice(choices: tuple[str, ...]) -> Callable[[str], str]:
    def parse(raw: str) -> str:
        value = raw.strip().lower()
        if value not in choices:
            raise ValueError(f"expected one of {'|'.join(choices)}, got {raw!r}")
        return value

    return parse


def _remote(raw: str) -> str:
    value = raw.strip()
    if not _REMOTE_RE.match(value):
        raise ValueError(f"expected a git remote name, got {raw!r}")
    if value == "upstream":
        # Backups are a push; `upstream` is the pull-only remote of an external hive (D1).
        raise ValueError("the backup remote may never be 'upstream'")
    return value


def _command(raw: str) -> str:
    value = raw.strip()
    if not value:
        raise ValueError("expected a scanner command, got an empty value")
    if "{range}" not in value:
        raise ValueError("the scanner command must carry a {range} placeholder")
    return value


@dataclass(frozen=True)
class KeySpec:
    """One policy key: its parser, its default, and the value a refused entry reads as."""

    key: str
    parse: Callable[[str], Any]
    default: Any
    refused: Any
    kind: str
    used_by: str
    help: str


# The authoritative key table (M14 D11). ``refused`` is each key's OFF state: what an invalid
# value reads as. For the scan switch "off" is the *safe* state — a refused scan switch keeps
# scanning rather than silently publishing unscanned work.
KEYS: dict[str, KeySpec] = {
    spec.key: spec
    for spec in (
        KeySpec(
            "pairing.enabled",
            _bool,
            False,
            False,
            "bool",
            "M14b",
            "Master switch: backup-before-state (rule P), checkpoints, claim-frame, pairing audit.",
        ),
        KeySpec(
            "pairing.remote",
            _remote,
            "",
            "",
            "remote name",
            "M14b",
            "Backup remote; empty = work.push_remote (normally origin). Never 'upstream'.",
        ),
        KeySpec(
            "pairing.secret_scan.enabled",
            _bool,
            True,
            True,
            "bool",
            "M14b",
            "Scan the new commits before every backup push; a finding or a missing "
            "scanner refuses the push.",
        ),
        KeySpec(
            "pairing.secret_scan.command",
            _command,
            DEFAULT_SECRET_SCAN,
            DEFAULT_SECRET_SCAN,
            "argv template with {range}",
            "M14b",
            "The secret scanner run over <remote tip>..<sha> before a backup push.",
        ),
        KeySpec(
            "pairing.checkpoint.on_commit",
            _bool,
            True,
            False,
            "bool",
            "M14b",
            "post-commit checkpoint push (`bh work backup --from-hook`).",
        ),
        KeySpec(
            "pairing.checkpoint.interval_seconds",
            _nonneg_int,
            300,
            0,
            "int >= 0",
            "M14b",
            "Timer checkpoint on the worker heartbeat tick; 0 = off.",
        ),
        KeySpec(
            "pairing.resume.signature_policy",
            _choice(SIGNATURE_POLICIES),
            "off",
            "off",
            "off|strict",
            "M14b",
            "strict: a backup with commits not signed by an allowed signer is suspect, "
            "never auto-resumed.",
        ),
        KeySpec(
            "pairing.retention.superseded_days",
            _nonneg_int,
            14,
            0,
            "int >= 0",
            "M14b",
            "Grace before deleting other frames' refs once a resumed bead lands.",
        ),
        KeySpec(
            "pairing.retention.unlanded_days",
            _nonneg_int,
            30,
            0,
            "int >= 0",
            "M14b",
            "Grace before deleting the refs of a bead closed without landing.",
        ),
        KeySpec(
            "pairing.retention.orphan_days",
            _nonneg_int,
            0,
            0,
            "int >= 0",
            "M14b",
            "Auto-delete orphan work after N days; 0 = never.",
        ),
        KeySpec(
            "reclaim.failover.mode",
            _choice(RECLAIM_MODES),
            "off",
            "off",
            "off|report|apply",
            "M3",
            "Failover adopt reclaim (D5a); apply requires pairing.enabled.",
        ),
        KeySpec(
            "reclaim.sweep.mode",
            _choice(RECLAIM_MODES),
            "off",
            "off",
            "off|report|apply",
            "M3",
            "`bh fleet reclaim --frame` sweep (D5b); apply requires pairing.enabled.",
        ),
    )
}


class PolicyError(ValueError):
    """A policy key or value refused at write time (unknown key, ill-typed value)."""


@dataclass(frozen=True)
class Refusal:
    key: str
    raw: str
    reason: str


@dataclass(frozen=True)
class PairingPolicy:
    """The typed, validated policy one command acts on. Construct with :func:`parse`."""

    values: Mapping[str, Any] = field(default_factory=dict)
    explicit: frozenset[str] = frozenset()
    refused: tuple[Refusal, ...] = ()

    def get(self, key: str) -> Any:
        if key in self.values:
            return self.values[key]
        return KEYS[key].default

    @property
    def enabled(self) -> bool:
        return bool(self.get("pairing.enabled"))

    @property
    def remote(self) -> str:
        return str(self.get("pairing.remote"))

    @property
    def secret_scan_enabled(self) -> bool:
        return bool(self.get("pairing.secret_scan.enabled"))

    @property
    def secret_scan_command(self) -> str:
        return str(self.get("pairing.secret_scan.command"))

    @property
    def checkpoint_on_commit(self) -> bool:
        return self.enabled and bool(self.get("pairing.checkpoint.on_commit"))

    @property
    def checkpoint_interval_seconds(self) -> int:
        return int(self.get("pairing.checkpoint.interval_seconds")) if self.enabled else 0

    @property
    def signature_policy(self) -> str:
        return str(self.get("pairing.resume.signature_policy"))

    def retention_days(self, name: str) -> int | None:
        """Grace in days for ``superseded`` / ``unlanded`` / ``orphan``; None = never delete.

        ``orphan`` 0 means never (D7). A refused retention value reads as never, the off state
        of a deletion — a typo must not turn into "delete immediately"."""
        key = f"pairing.retention.{name}_days"
        if any(r.key == key for r in self.refused):
            return None
        days = int(self.get(key))
        if name == "orphan" and days == 0:
            return None
        return days

    def reclaim_mode(self, which: str) -> str:
        """Effective ``reclaim.<which>.mode``: ``apply`` without pairing degrades to ``report``."""
        mode = str(self.get(f"reclaim.{which}.mode"))
        if mode == "apply" and not self.enabled:
            return "report"
        return mode


#: The 0.22.x policy: every master switch off.
DEFAULT = PairingPolicy()


def parse(config_rows: Mapping[str, Any] | None) -> PairingPolicy:
    """Parse the ``bh.``-prefixed rows of a ``bd config list`` map into a :class:`PairingPolicy`.

    Rows outside the namespace are ignored. Unknown ``bh.pairing.*`` / ``bh.reclaim.*`` keys and
    ill-typed values are refused (recorded, never clamped); a refused key reads as its off state.
    """
    values: dict[str, Any] = {}
    explicit: set[str] = set()
    refused: list[Refusal] = []
    for raw_key, raw_value in (config_rows or {}).items():
        if not isinstance(raw_key, str) or not raw_key.startswith(PREFIX):
            continue
        key = raw_key[len(PREFIX) :]
        if not key.startswith(("pairing.", "reclaim.")):
            continue  # another bh.* namespace, not ours to judge
        spec = KEYS.get(key)
        if spec is None:
            refused.append(Refusal(key, str(raw_value), "unknown key"))
            continue
        try:
            values[key] = spec.parse(str(raw_value))
        except ValueError as exc:
            refused.append(Refusal(key, str(raw_value), str(exc)))
            values[key] = spec.refused
            continue
        explicit.add(key)
    return PairingPolicy(values, frozenset(explicit), tuple(refused))


def validate(key: str, value: str) -> str:
    """Canonical stored text for ``key=value``, or :class:`PolicyError` (write-time check)."""
    spec = KEYS.get(key)
    if spec is None:
        raise PolicyError(f"unknown policy key {key!r} (known: {', '.join(sorted(KEYS))})")
    try:
        parsed = spec.parse(value)
    except ValueError as exc:
        raise PolicyError(f"{key}: {exc}") from None
    if isinstance(parsed, bool):
        return "true" if parsed else "false"
    return str(parsed)


def read(main) -> PairingPolicy:
    """The policy stored in the hive at ``main`` (one ``bd config list --json`` read).

    Any failure to read reads as :data:`DEFAULT` — every switch off, exactly 0.22.x behaviour —
    because a hive whose data cannot be read has not opted into anything."""
    from . import bd_cli

    try:
        rows = bd_cli.routes(main).config_list()
    except Exception:
        return DEFAULT
    return parse(rows if isinstance(rows, dict) else None)


def write(main, key: str, value: str, *, actor: str = "") -> str:
    """Validate and store ``bh.<key>=value`` in the hive's data; returns the stored text."""
    from . import bd, bd_cli

    stored = validate(key, value)
    res = bd_cli.routes(main).config_set(PREFIX + key, stored, actor=actor)
    if res.returncode != 0:
        raise PolicyError(f"bd config set {PREFIX}{key} failed: {bd.err_line(res)}")
    return stored


def unset(main, key: str, *, actor: str = "") -> None:
    """Drop ``bh.<key>`` so it reads as its default again."""
    from . import bd, bd_cli

    if key not in KEYS:
        raise PolicyError(f"unknown policy key {key!r}")
    res = bd_cli.routes(main).config_unset(PREFIX + key, actor=actor)
    if res.returncode != 0:
        raise PolicyError(f"bd config unset {PREFIX}{key} failed: {bd.err_line(res)}")


def rows(policy: PairingPolicy) -> list[dict[str, Any]]:
    """One row per key for ``bh hive policy list``: effective value, default, source."""
    refused = {r.key: r for r in policy.refused}
    out = []
    for key, spec in KEYS.items():
        row = {
            "key": key,
            "value": policy.get(key),
            "default": spec.default,
            "source": "hive"
            if key in policy.explicit
            else ("refused" if key in refused else "default"),
            "type": spec.kind,
            "used_by": spec.used_by,
        }
        if key in refused:
            row["refused"] = {"raw": refused[key].raw, "reason": refused[key].reason}
        out.append(row)
    for r in policy.refused:
        if r.key not in KEYS:
            out.append(
                {
                    "key": r.key,
                    "value": None,
                    "default": None,
                    "source": "refused",
                    "type": "",
                    "used_by": "",
                    "refused": {"raw": r.raw, "reason": r.reason},
                }
            )
    return out
