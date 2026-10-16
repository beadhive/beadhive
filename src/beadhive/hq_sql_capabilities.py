"""Least-privilege capability preflight for the HQ operator bindings (bh-aprgo, bh-t759y).

A release upgrade crosses three privilege tiers on Dolt 2.3.5:

* **runtime read** — every account that verifies the signed authority and its durable restore
  floor (authority writer, observer, director and every frame). A floor that meets a
  same-sequence provisioning commit re-verifies it with ``dolt_ignore``, the liveness policy,
  ``dolt_schemas`` history and two-argument ``DOLT_DIFF_SUMMARY``, and Dolt authorizes that
  diff only with ``SELECT`` on **every** table of the runtime database (tables provisioned
  later included). So the requirement is ``SELECT ON <runtime>.*``: read-only, never a write.
* **session inspection** — the authority writer that applies a session route re-checks the
  provisioned incarnation without repairing it. ``information_schema.triggers`` lists a
  trigger only to a holder of ``TRIGGER``, and the account/grant checks read ``mysql``.
* **provisioning** — the server-local binding that creates tables, stamping triggers, the
  pinned frame account and its grants. Granting the frame ``SELECT, UPDATE`` on its new session
  and evidence tables is a delegation: the binding must itself hold ``UPDATE`` (and the grant
  option) at a level covering tables that do not exist yet, i.e. ``UPDATE ON <runtime>.* WITH
  GRANT OPTION``. Dolt 2.3.5 refuses the grant otherwise, after the tables, triggers and account
  already exist (bh-t759y).

Each missing capability is reported by name with the grant that satisfies it, never with an
account name, host or secret. Reads are real probes (a denial is the finding); privileges that
cannot be probed without writing are read from the session's own privilege rows. A preflight
never widens a check: a capability it reports missing makes the action refuse before it writes.
"""

from __future__ import annotations

from dataclasses import dataclass

from .hq_sql_runtime_schema import LIVENESS_POLICY_TABLE
from .hq_storage_composition import runtime_statements

#: MySQL/Dolt access-denied error codes. Dolt 2.3.5 reports most denials as 1105 with an
#: "Access denied" / "command denied" message, which :func:`denied` also recognizes.
DENIED_CODES = frozenset({1044, 1045, 1142, 1143, 1227, 1370})
#: Unknown table: the object the capability reads is not provisioned at all.
ABSENT_CODES = frozenset({1146})

DOCS = "docs/HQ.md#runtime-sql-privileges"


class CapabilityProbeError(ValueError):
    """A probe failed for a reason other than a missing privilege; the preflight is unknown."""


@dataclass(frozen=True)
class Missing:
    """One missing capability: what it is for and the grant (or step) that satisfies it."""

    capability: str
    remedy: str

    def __str__(self) -> str:
        return f"{self.capability}: {self.remedy}"


def _code_message(error: BaseException) -> tuple[int | None, str]:
    args = getattr(error, "args", ())
    if len(args) >= 2 and isinstance(args[0], int) and isinstance(args[1], str):
        return args[0], args[1].lower()
    return None, ""


def denied(error: BaseException) -> bool:
    """Whether ``error`` is a server-side privilege denial (not an outage or a bug)."""
    code, message = _code_message(error)
    if code is None:
        return False
    return code in DENIED_CODES or code == 1105 and "denied" in message


def absent(error: BaseException) -> bool:
    code, message = _code_message(error)
    return code in ABSENT_CODES or code == 1105 and "table not found" in message


def _probe(read, missing: Missing, *, absent_remedy: Missing | None = None) -> Missing | None:
    try:
        read()
    except Exception as error:  # noqa: BLE001 - classified below, otherwise refused
        if denied(error):
            return missing
        if absent_remedy is not None and absent(error):
            return absent_remedy
        raise CapabilityProbeError(
            f"capability preflight could not read {missing.capability}"
        ) from None
    return None


def runtime_read(cursor, database: str, head: str, *, session: bool = True) -> list[Missing]:
    """What the restore-floor and authority verification read, probed at ``head``.

    ``session`` adds the liveness policy, which exists only once session routes are enabled."""
    sql = runtime_statements(cursor)
    wide = f"SELECT ON `{database}`.*"
    probes = (
        (lambda: sql.authority_at(head), "signed authority read (hq_authority)"),
        (lambda: sql.routes_for_frame_at(head, ""), "principal registry read"),
        (lambda: sql.first_parents(head), "commit ancestry read (dolt_commit_ancestors)"),
        (lambda: sql.schemas_at(head), "schema history read (dolt_schemas)"),
        (lambda: sql.ignore_rows_at(head), "ignore-rule history read (dolt_ignore)"),
    )
    problems = [
        found for read, name in probes if (found := _probe(read, Missing(name, wide))) is not None
    ]
    policy = session and _probe(
        lambda: sql.liveness_policy(at=head),
        Missing(f"liveness policy history read ({LIVENESS_POLICY_TABLE})", wide),
        absent_remedy=Missing(
            f"liveness policy ({LIVENESS_POLICY_TABLE})",
            "not provisioned; run the server-local liveness schema provisioning first",
        ),
    )
    if policy:
        problems.append(policy)
    diff = _probe(
        lambda: sql.diff_summary(head, head),
        Missing(
            "restore-floor diff (two-argument DOLT_DIFF_SUMMARY)",
            f"{wide}; Dolt checks SELECT on every table of the database, including "
            "incarnation tables provisioned later, so per-table grants are never sufficient",
        ),
    )
    if diff is not None:
        problems.append(diff)
    return problems


def writer_read(cursor) -> list[Missing]:
    """The authority writer's own pre-publication reads (clean tree, floors and evidence)."""
    sql = runtime_statements(cursor)
    probes = (
        (sql.dirty, "committed working-tree check (dolt_status)"),
        (lambda: sql.floor("", "", 0), "observer floor read (hq_live_floors)"),
        (lambda: sql.latest_receipts("", "", 0), "receipt read (hq_live_receipts)"),
        (lambda: sql.public_observation("", "", 0), "observation read"),
        (lambda: sql.registration_record("", "", "", 0), "registration read"),
    )
    return [
        found
        for read, name in probes
        if (found := _probe(read, Missing(name, "SELECT on the runtime database"))) is not None
    ]


def _grants(cursor) -> list[tuple[str, str, bool]]:
    """``(schema, privilege, grantable)`` held by this session's own account (global ``*``)."""
    user, rows = runtime_statements(cursor).own_privileges()
    name, _, host = str(user).rpartition("@")
    grantee = f"'{name}'@'{host}'"
    return [
        (str(schema), str(privilege).upper(), str(grantable).upper() == "YES")
        for owner, schema, privilege, grantable in rows
        if owner == grantee
    ]


def _holds(grants, privilege: str, database: str, *, option: bool = False) -> bool:
    return any(
        schema in ("*", database)
        and held in (privilege, "ALL", "ALL PRIVILEGES")
        and (grantable or not option)
        for schema, held, grantable in grants
    )


def session_inspection(cursor, database: str) -> list[Missing]:
    """What re-validating a provisioned session route needs (apply and provision)."""
    grants = _grants(cursor)
    problems = []
    if not _holds(grants, "TRIGGER", database):
        problems.append(
            Missing(
                "incarnation trigger inspection (information_schema.triggers)",
                f"TRIGGER ON `{database}`.*; Dolt lists triggers only to holders of TRIGGER",
            )
        )
    if not _holds(grants, "SELECT", "mysql"):
        problems.append(
            Missing(
                "frame account and grant inspection (mysql.user, SHOW GRANTS FOR)",
                "SELECT ON `mysql`.*",
            )
        )
    return problems


def provisioning(cursor, database: str, config_database: str | None) -> list[Missing]:
    """What the server-local session provisioning writes: DDL, triggers, account, grants."""
    grants = _grants(cursor)
    problems = [
        Missing(f"{privilege} for incarnation provisioning", f"{privilege} ON `{database}`.*")
        for privilege in ("CREATE", "TRIGGER", "INSERT")
        if not _holds(grants, privilege, database)
    ]
    if not _holds(grants, "CREATE USER", "*"):
        problems.append(Missing("pinned frame account creation", "CREATE USER ON *.*"))
    if not _holds(grants, "UPDATE", database, option=True):
        problems.append(
            Missing(
                "frame session/evidence write grants",
                f"UPDATE ON `{database}`.* WITH GRANT OPTION; the frame's tables are created "
                "during provisioning, so a per-table grant cannot be delegated",
            )
        )
    for target in (database, config_database):
        if target and not _holds(grants, "SELECT", target, option=True):
            problems.append(
                Missing(
                    "frame read grants",
                    f"SELECT ON `{target}`.* WITH GRANT OPTION",
                )
            )
    return problems


def report(cursor, *, database: str, config_database: str | None, head: str, route: str) -> dict:
    """The redacted per-action capability report for one operator binding.

    ``route`` is the reviewed next incarnation's route family. Only a ``session`` route has a
    same-sequence provisioning commit (floor re-verification) and a re-validated incarnation;
    a legacy inbox route keeps its existing manual procedure and needs neither."""
    session = route == "session"
    read = runtime_read(cursor, database, head, session=session) + writer_read(cursor)
    inspect = session_inspection(cursor, database) if session else []
    provision = provisioning(cursor, database, config_database) if session else []
    apply_missing = read + inspect if session else []
    provision_missing = read + inspect + provision if session else []
    return {
        "route": route,
        "runtime_read": [str(item) for item in read],
        "apply": [str(item) for item in apply_missing],
        "provision": [str(item) for item in provision_missing],
        "ready": {
            "apply": not apply_missing,
            "provision": session and not provision_missing,
        },
        "docs": DOCS,
    }
