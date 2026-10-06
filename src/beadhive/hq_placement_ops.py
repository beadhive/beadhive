"""Operator/director placement actions behind ``bh hq placement`` (bh-16347.5).

Thin, Typer-free glue over :mod:`beadhive.hq_sql_placement` (bh-a94qw): every write is the
library's CAS, every refusal is the library's :class:`~beadhive.hq_sql_placement.PlacementError`.
What this module adds is the operator shape the other HQ authority verbs already have:

* **Dry run by default.** ``place`` and ``release`` preview against a verified read and write
  nothing unless ``confirm``; a preview refuses exactly what the CAS would refuse (a moved row,
  an epoch that does not increase, an unplaceable frame, an out-of-range tenure).
* **Never clamp.** An invalid epoch or tenure is refused, not adjusted.
* **Seeding stays provisioning.** The director credential holds ``UPDATE`` only, so ``seed``
  renders the one-time ``INSERT`` (:func:`~beadhive.hq_sql_placement.seed_statement`) for the
  operator's server-local provisioning account; it never executes it.
* **Conformance** runs :func:`~beadhive.hq_sql_placement.conformance` over the accounts the
  server and the principal registry name.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping

from .hq_sql_placement import (
    DEFAULT_TENURE_S,
    PlacementError,
    PlacementRecord,
    SqlPlacementDirector,
    conformance,
    seed_statement,
)

__all__ = [
    "ACTIONS",
    "check",
    "place",
    "record_view",
    "release",
    "render_statement",
    "seed",
    "show",
]

ACTIONS = ("show", "seed", "place", "release", "check")
_ACCOUNT_HOST = re.compile(r"[A-Za-z0-9.%:_-]{1,255}")
_ACCOUNT_USER = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,31}")


def record_view(record: PlacementRecord | None) -> dict:
    """A JSON-safe view of one placement row."""
    if record is None:
        return {"seeded": False}
    lease = record.lease
    return {
        "seeded": True,
        "prefix": record.prefix,
        "revision": record.revision,
        "frame_id": record.frame_id,
        "host_id": lease.host_id,
        "label": lease.label,
        "epoch": lease.epoch,
        "adopted_at": lease.adopted_at,
        "expires_at": lease.expires_at,
        "released": lease.is_tombstone,
        "source": "director" if record.director else "receiver",
        "cause": record.cause,
    }


def show(director: SqlPlacementDirector, prefix: str) -> dict:
    """The hive's current placement row (read-only)."""
    return {"prefix": prefix, **record_view(director.read(prefix))}


def _literal(value) -> str:
    if isinstance(value, bytes):
        return "X'" + value.hex() + "'"
    text = str(value)
    if not re.fullmatch(r"[A-Za-z0-9_.:-]*", text):
        raise PlacementError("seed value is not a plain identifier")  # never true for the seed
    return "'" + text + "'"


def render_statement(sql: str, params) -> str:
    """`sql` with its ``%s`` placeholders rendered as SQL literals (bytes as hex), so the
    operator can run it verbatim with ``dolt sql -q``."""
    parts = sql.split("%s")
    if len(parts) != len(params) + 1:
        raise PlacementError("seed statement shape invalid")
    out = parts[0]
    for value, rest in zip(params, parts[1:], strict=True):
        out += _literal(value) + rest
    return out + ";"


def seed(
    prefix: str,
    *,
    epoch: int | None,
    director: SqlPlacementDirector | None = None,
    at: float | None = None,
) -> dict:
    """The one-time provisioning ``INSERT`` for a never-placed hive at `epoch` — the hive's
    current ``refs/bh/epoch`` (1 when it was never fenced). Executes nothing. With a
    `director`, refuses a hive that already has a row."""
    if epoch is None:
        raise PlacementError(
            "seed requires --epoch: the hive's current refs/bh/epoch (1 when never fenced)"
        )
    sql, params = seed_statement(prefix, epoch=epoch, at=at)
    if director is not None:
        current = director.read(prefix)
        if current is not None:
            raise PlacementError(
                f"hive {prefix} is already seeded ({current.describe()}); nothing to provision"
            )
    return {
        "prefix": prefix,
        "epoch": epoch,
        "statement": render_statement(sql, params),
        "executed": False,
        "run_as": "the operator's server-local provisioning account (the director credential "
        "holds UPDATE only)",
    }


def _expect(record: PlacementRecord | None, prefix: str, expected: str) -> PlacementRecord:
    if record is None:
        raise PlacementError(f"hive {prefix} has no placement row; seed it first")
    if not expected:
        raise PlacementError(
            f"--expected-revision is required (the row is at {record.revision}; read it with "
            "`bh hq placement show`)"
        )
    if record.revision != expected:
        raise PlacementError(
            f"hive {prefix} placement moved: row is at {record.revision}, expected {expected}; "
            "re-read and decide again"
        )
    return record


def _check_tenure(tenure_s: float) -> None:
    if not 0 < tenure_s <= DEFAULT_TENURE_S:
        raise PlacementError(f"--tenure must be within (0, {int(DEFAULT_TENURE_S)}] seconds")


def place(
    director: SqlPlacementDirector,
    prefix: str,
    *,
    frame: str,
    expected: str,
    epoch: int | None = None,
    tenure_s: float = DEFAULT_TENURE_S,
    confirm: bool = False,
) -> dict:
    """Place `frame` on `prefix` at `epoch` (default row + 1; must increase). A dry run
    unless `confirm`."""
    if not frame:
        raise PlacementError("place requires --frame")
    _check_tenure(tenure_s)
    if epoch is not None and (type(epoch) is not int or epoch < 1):
        raise PlacementError("--epoch must be a positive integer")
    if confirm:
        placed = director.place(
            prefix, frame_id=frame, expected_revision=expected, epoch=epoch, tenure_s=tenure_s
        )
        return {"dry_run": False, "placed": record_view(placed)}
    survey = director.survey()
    current = _expect(survey.placements.get(prefix), prefix, expected)
    director.placeable(survey.state, survey.policies, prefix, frame)
    target = current.lease.epoch + 1 if epoch is None else epoch
    if target <= current.lease.epoch:
        raise PlacementError(
            f"placement must raise the epoch (row at {current.lease.epoch}, asked {target})"
        )
    return {
        "dry_run": True,
        "current": record_view(current),
        "would_place": {"prefix": prefix, "frame_id": frame, "epoch": target, "tenure_s": tenure_s},
    }


def release(
    director: SqlPlacementDirector, prefix: str, *, expected: str, confirm: bool = False
) -> dict:
    """Release `prefix` to a tombstone at the placed epoch. A dry run unless `confirm`."""
    if confirm:
        return {
            "dry_run": False,
            "released": record_view(director.release(prefix, expected_revision=expected)),
        }
    current = _expect(director.read(prefix), prefix, expected)
    if current.lease.is_tombstone:
        raise PlacementError(f"hive {prefix} placement is already released")
    return {
        "dry_run": True,
        "current": record_view(current),
        "would_release": {"prefix": prefix, "epoch": current.lease.epoch},
    }


def _accounts(cursor, user: str) -> list[str]:
    if not _ACCOUNT_USER.fullmatch(user):
        raise PlacementError("conformance: invalid account name")
    cursor.execute("SELECT user,host FROM mysql.user WHERE user=%s", (user,))
    accounts = []
    for name, host in cursor.fetchall():
        if name != user or not isinstance(host, str) or not _ACCOUNT_HOST.fullmatch(host):
            raise PlacementError("conformance: server returned an unexpected account")
        accounts.append(f"'{user}'@'{host}'")
    return sorted(accounts)


def check(
    settings: Mapping,
    *,
    connect: Callable[[Mapping], object] | None = None,
    session_table: Callable[[str, int], str] | None = None,
) -> dict:
    """Grant and trigger conformance (conditions 5-6) over every director and frame account.

    Runs on the operator's ``authority_writer`` binding when the settings carry one (it can read
    other accounts' grants), else the ``placement_writer``. Frames are the principal registry's
    accounts; each may write its inbox tables and its session and evidence tables (M9,
    ``bh-owqdg``: the conformance job UPDATEs its own evidence row), nothing protected."""
    from .failover_observer import session_table as default_session_table
    from .hq_sql_runtime_schema import evidence_table

    session_table = session_table or default_session_table
    director_binding = settings.get("placement_writer")
    if not director_binding:
        raise PlacementError("conformance needs hq.sql.placement_writer in the operator settings")
    binding = settings.get("authority_writer") or director_binding
    connection = _connect(binding, connect)
    try:
        with connection.cursor() as cursor:
            directors = _accounts(cursor, director_binding["user"])
            if not directors:
                raise PlacementError("conformance: the placement writer account does not exist")
            cursor.execute("SELECT principal,epoch,inbox_table FROM hq_principal_registry")
            writable: dict[str, set[str]] = {}
            for principal, epoch, inbox in cursor.fetchall():
                tables = writable.setdefault(str(principal), set())
                tables.add(str(inbox))
                tables.add(session_table(str(principal), int(epoch)))
                tables.add(evidence_table(str(principal), int(epoch)))
            frames = {
                account: sorted(tables)
                for principal, tables in sorted(writable.items())
                for account in _accounts(cursor, principal)
            }
            problems = []
            for account in directors:
                problems += conformance(
                    cursor, database=binding["database"], director=account, frames=frames
                )
        # conformance() checks the triggers once per call; keep each finding once.
        problems = list(dict.fromkeys(problems))
        return {
            "conformant": not problems,
            "director_accounts": directors,
            "frame_accounts": sorted(frames),
            "problems": problems,
        }
    except PlacementError:
        raise
    except Exception:  # noqa: BLE001 - driver text may carry credential-adjacent detail
        raise PlacementError(
            "conformance check unavailable (the account must be able to read mysql.user, "
            "other accounts' grants and information_schema.triggers)"
        ) from None
    finally:
        try:
            connection.rollback()
        except Exception:  # noqa: BLE001
            pass
        connection.close()


def _connect(binding: Mapping, connect):
    if connect is not None:
        return connect(binding)
    from .hq_sql_transport import FnoxBroker, SqlTransportError
    from .hq_sql_transport import connect as sql_connect

    try:
        return sql_connect(
            dict(binding), FnoxBroker(), deadline=time.monotonic() + binding["operation_timeout"]
        )
    except SqlTransportError:
        raise PlacementError("verified operator connection unavailable") from None
