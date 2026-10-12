"""``RecordKind.FAILOVER_POLICY`` on root's ``hq_live_failover_policy`` (bh-xnn5p).

Root's failover policy (``failover_policy``, bh-4biq8) is one row per ``(scope, setting)`` --
``scope`` a hive prefix or ``*``, ``setting`` a role or ``executor_floor``, ``seconds`` a
``BIGINT`` -- read by every 0.23+ director, and the director credential holds ``SELECT, INSERT,
UPDATE, DELETE`` on that table and no other write right. :class:`DoltHqStorage` with
``runtime_tables=True`` maps the port onto it column for column:

* a record's key is the scope, its body ``{setting: seconds}``; a scope with no rows is absent;
* there is no revision or witness column, and the director may not write any other table, so a
  record's revision is the digest of its content (:func:`policy_revision`), a CAS compares that
  digest inside its transaction, and ``lookup`` can never confirm (``NotConfirmed`` -- never a
  licence to retry);
* a write is the same ``DELETE`` / ``INSERT ... ON DUPLICATE KEY UPDATE`` per changed setting
  root issued before.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any, Final

from beadhive_hq_storage import Body, InvalidRequest, Revision

__all__ = [
    "POLICY_TABLE",
    "check_policy_body",
    "failover_policy_grant",
    "policy_bodies",
    "policy_revision",
    "policy_statements",
]

POLICY_TABLE: Final = "hq_live_failover_policy"
"""Root ``hq_sql_runtime_schema.FAILOVER_POLICY_TABLE``."""
SELECT_ROWS: Final = f"SELECT scope,setting,seconds FROM {POLICY_TABLE}"
_BIGINT = 2**63


def check_policy_body(body: Mapping[str, Any]) -> dict[str, int]:
    """``{setting: seconds}`` with a non-empty setting of at most 32 characters and a
    ``BIGINT`` whole number (never a bool); anything else is refused before any I/O."""
    out: dict[str, int] = {}
    for setting, seconds in body.items():
        if not isinstance(setting, str) or not 0 < len(setting) <= 32:
            raise InvalidRequest("a failover policy setting is a name of at most 32 characters")
        if type(seconds) is not int or not -_BIGINT <= seconds < _BIGINT:
            raise InvalidRequest(f"failover policy {setting!r} must be whole seconds")
        out[setting] = seconds
    return out


def policy_revision(scope: str, body: Body) -> Revision:
    """The content digest a scope's rows carry as their revision (64 hex)."""
    material = json.dumps(
        {"kind": "failover_policy", "scope": scope, "body": dict(body)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return Revision(hashlib.sha256(material).hexdigest())


def _text(value: object) -> str:
    if isinstance(value, bytes | bytearray | memoryview):
        return bytes(value).decode()
    return str(value)


def policy_bodies(rows: Iterable[tuple[Any, Any, Any]]) -> dict[str, dict[str, int]]:
    """``{scope: {setting: seconds}}`` from raw ``(scope, setting, seconds)`` rows."""
    out: dict[str, dict[str, int]] = {}
    for scope, setting, seconds in rows:
        out.setdefault(_text(scope), {})[_text(setting)] = int(seconds)
    return out


def policy_statements(
    scope: str, current: Mapping[str, int], body: Mapping[str, int]
) -> list[tuple[str, tuple[Any, ...]]]:
    """The writes moving ``scope`` from ``current`` to ``body``: a ``DELETE`` for a setting
    that goes, an upsert for one that is new or changed, in setting order."""
    out: list[tuple[str, tuple[Any, ...]]] = []
    for setting in sorted(set(current) | set(body)):
        if setting not in body:
            out.append(
                (f"DELETE FROM {POLICY_TABLE} WHERE scope=%s AND setting=%s", (scope, setting))
            )
        elif current.get(setting) != body[setting]:
            out.append(
                (
                    f"INSERT INTO {POLICY_TABLE} (scope,setting,seconds) VALUES (%s,%s,%s) "
                    "ON DUPLICATE KEY UPDATE seconds=VALUES(seconds)",
                    (scope, setting, body[setting]),
                )
            )
    return out


def failover_policy_grant(target: str, user: str) -> str:
    """The director's grant on the policy table (root renders, never executes, it; ``target``
    and ``user`` are root-validated identifiers, the host a placeholder)."""
    return f"GRANT SELECT, INSERT, UPDATE, DELETE ON {target} TO '{user}'@'<host>'"
