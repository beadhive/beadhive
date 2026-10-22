"""SQL for the signed release catalog table ``hq_release_catalog`` (bh-vfrem.4).

Root (``hq_release_catalog``) owns the identity policy, the Sigstore verification and the rules;
this module owns the statements it runs against the HQ runtime database, the separately
credentialed catalog account's grants, and the restore-floor diff probes. The table is committed
(not an ``hq_live_*`` table), append-only, and written only by the catalog binding, which holds
``INSERT`` on this table and nothing else. Frames read it with ``SELECT``.
"""

from __future__ import annotations

import re
from typing import Final

__all__ = [
    "CATALOG_COLUMNS",
    "CATALOG_DIFF_SQL",
    "CATALOG_INSERT_SQL",
    "CATALOG_ROWS_SQL",
    "RELEASE_CATALOG_TABLE",
    "catalog_account_statements",
    "catalog_select_statement",
]

RELEASE_CATALOG_TABLE: Final = "hq_release_catalog"
"""Root ``hq_sql_runtime_schema.RELEASE_CATALOG_TABLE``."""
CATALOG_COLUMNS: Final = (
    "catalog",
    "version",
    "measured_digest",
    "status",
    "algorithm",
    "wheel_sha256",
    "commit",
    "bundle",
    "reason",
    "recorded_at",
)
"""Column order of :data:`CATALOG_INSERT_SQL` and :data:`CATALOG_ROWS_SQL`."""

_LIST = ",".join(f"`{name}`" for name in CATALOG_COLUMNS)
CATALOG_INSERT_SQL: Final = (
    f"INSERT INTO {RELEASE_CATALOG_TABLE} ({_LIST}) VALUES "
    f"({','.join(['%s'] * len(CATALOG_COLUMNS))})"
)
CATALOG_ROWS_SQL: Final = f"SELECT {_LIST} FROM {RELEASE_CATALOG_TABLE}"
"""Every row; callers append ``WHERE catalog = %s AND version = %s`` or ``AS OF %s``."""
CATALOG_DIFF_SQL: Final = (
    f"SELECT DISTINCT diff_type FROM DOLT_DIFF(%s, %s, '{RELEASE_CATALOG_TABLE}')"
)
"""Row-level diff types between two revisions: the restore floor accepts ``{'added'}`` only."""

_ACCOUNT = re.compile(r"'[A-Za-z0-9_]{1,32}'@'[A-Za-z0-9_.:-]{1,255}'")
_DATABASE = re.compile(r"[A-Za-z0-9_]{1,64}")


def catalog_select_statement(account: str, database: str) -> str:
    """The frame's read of the catalog (a frame also holds database-wide ``SELECT``)."""
    return f"GRANT SELECT ON `{database}`.`{RELEASE_CATALOG_TABLE}` TO {account}"


def catalog_account_statements(account: str, database: str) -> tuple[str, ...]:
    """The catalog binding's TLS-required account: database-wide read (the writer reads the
    catalog and the working-tree status), ``INSERT`` on this one table, and ``EXECUTE`` on the
    ``dolt_add`` / ``dolt_commit`` procedures (Dolt refuses both to an account without it, the
    authority writer's own shape). Never ``UPDATE``, ``DELETE``, ``TRIGGER``, ``CREATE`` or a
    grant option. ``CREATE USER`` binds the password as its one ``%s`` parameter."""
    if not _ACCOUNT.fullmatch(account) or not _DATABASE.fullmatch(database):
        raise ValueError("invalid catalog account or database identifier")
    return (
        f"CREATE USER {account} IDENTIFIED BY %s REQUIRE SSL",
        f"GRANT SELECT ON `{database}`.* TO {account}",
        f"GRANT INSERT ON `{database}`.`{RELEASE_CATALOG_TABLE}` TO {account}",
        f"GRANT EXECUTE ON PROCEDURE `{database}`.`dolt_add` TO {account}",
        f"GRANT EXECUTE ON PROCEDURE `{database}`.`dolt_commit` TO {account}",
    )
