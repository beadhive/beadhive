"""The in-data epoch fence and write guard DDL — product version (bh-uz46l, P-M1).

ADR ``docs/design/hive-writer-partitioning-adr.md`` §2 and binding conditions 1–3 and 9. This is
the composed prototype (``tests/harness/composed_fence.py``, ``bh-jbb6r``) turned into product
DDL, statement for statement:

* **Fence tables** (``bh-vje85``), versioned on ``main``:

  - ``bh_writer (id = 1, frame, epoch, revision)`` — rewritten only by adopt;
  - ``bh_epoch_live (id PK, epoch UNIQUE)`` — a singleton (``bh-cvk70`` E12);
  - ``bh_write_mark (id uuid, epoch FK → bh_epoch_live.epoch, tbl)`` — one per guarded write,
    so a commit stamped by a retired epoch is a foreign-key violation, not a clean merge.

* **The write guard** (``bh-sieai``): the ``bh_guard_check()`` procedure plus 42 ``BEFORE``
  triggers on the 14 versioned bd tables in :data:`GUARDED_TABLES`. Each trigger inlines the
  mark insert and ``CALL`` is its LAST statement; the node identity is read from the
  ``dolt_ignore``'d ``bh_local_ident`` inside the procedure only.
* **Monotonic ``BEFORE UPDATE`` triggers** on ``bh_writer`` and ``bh_epoch_live``.

That is :data:`TRIGGER_COUNT` = 44 ``bh_*`` triggers. Every statement here is pinned by the F3
trigger-semantics canary (``tests/test_fence_trigger_canary_int.py``): Dolt 2.3.5 skips
statements after a ``CALL`` in a trigger, drops procedure DML inside an explicit transaction,
fails open on ``@user`` variables in an ``IF``, and breaks a scalar subquery in ``VALUES`` — so
fence reads go through DECLAREd locals (``SELECT … INTO``), never ``@`` variables or subqueries
(``bh-vje85`` E3).

**Install order** (``bh-jbb6r``): fence tables (and, on a first install, their seed rows), then
the guard, then the monotonic triggers last. The install is idempotent: tables are
``CREATE … IF NOT EXISTS`` (the fence's DATA is never dropped), seed rows are written only when
``bh_writer`` is absent (decided by the caller from a read, not by a subquery), and every
procedure and trigger is drop-then-create.

Pure: strings and names only. Running them is :mod:`beadhive.fence_data`'s job.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

__all__ = [
    "ADOPT_SENTINEL_PREFIX",
    "EVENTS",
    "FENCE_TABLES",
    "GUARDED_TABLES",
    "GUARD_PROCEDURE",
    "GUARD_REFUSAL",
    "IGNORE_PATTERN",
    "INSTALL_COMMIT_MESSAGE",
    "LOCAL_IDENT_TABLE",
    "MONOTONIC_REFUSAL",
    "MONOTONIC_TRIGGERS",
    "ROLES",
    "TRIGGER_COUNT",
    "UNGUARDED_BD_TABLES",
    "fence_table_statements",
    "guard_statements",
    "guard_trigger_names",
    "ident_statements",
    "install_statements",
    "monotonic_statements",
    "quote",
    "render_script",
    "seed_statements",
    "trigger_names",
]

#: Every VERSIONED table bd 1.3 writes (schema v66), less ``child_counters`` and ``metadata``
#: (the ``bh-sieai`` set, condition 9). The ``dolt_ignore``'d ones — ``events``, ``leases``,
#: ``local_metadata``, ``repo_mtimes``, ``bd_events_*``, ``ignored_schema_migrations``,
#: ``wisps``/``wisp_*`` — never merge or push, so a guard there would fence nothing;
#: ``schema_migrations`` is bd's own cursor.
GUARDED_TABLES: tuple[str, ...] = (
    "issues",
    "dependencies",
    "labels",
    "comments",
    "config",
    "issue_counter",
    "issue_snapshots",
    "compaction_snapshots",
    "custom_statuses",
    "custom_types",
    "federation_peers",
    "routes",
    "interactions",
    "provenance_events",
)

#: Versioned bd tables deliberately left unguarded (``bh-sieai`` E6):
#:
#: * ``child_counters`` — Dolt refuses ANY multi-table ``DELETE`` on a table with triggers, and
#:   bd's clone-local migration 0011 runs one on every fresh clone (a guard there stops
#:   ``bd init`` joining the hive). A counter bump only happens inside the transaction of the
#:   guarded child ``INSERT`` into ``issues``, which refuses the whole transaction.
#: * ``metadata`` — bd writes clone-local values there on every join and import, from
#:   non-writers too.
UNGUARDED_BD_TABLES: tuple[str, ...] = ("child_counters", "metadata")

EVENTS: tuple[str, ...] = ("insert", "update", "delete")
FENCE_TABLES: tuple[str, ...] = ("bh_writer", "bh_epoch_live", "bh_write_mark")
LOCAL_IDENT_TABLE = "bh_local_ident"
#: The ``dolt_ignore`` pattern that keeps every node-local ``bh_local_*`` table out of commits.
IGNORE_PATTERN = "bh_local_%"
GUARD_PROCEDURE = "bh_guard_check"
MONOTONIC_TRIGGERS: tuple[str, ...] = ("bh_writer_monotonic", "bh_epoch_live_monotonic")
#: ``replica`` (the default) is guarded; ``branch`` marks a branch-path frame whose local
#: ``main`` is a staging branch bh publishes to a frame-private ref (``bh-sieai`` R2).
ROLES: tuple[str, ...] = ("replica", "branch")
#: Every bump inserts a ``bh_write_mark`` row with this id prefix (condition 1).
ADOPT_SENTINEL_PREFIX = "adopt-"
INSTALL_COMMIT_MESSAGE = "bh: install in-data epoch fence and write guard"

GUARD_REFUSAL = "bh-guard: this replica is not the bh_writer for main"
MONOTONIC_REFUSAL = "bh: fence epoch must increase"

_DELIMITER = "//"


def quote(value: object) -> str:
    """A SQL string literal (backslash and quote escaped)."""
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


def _ident(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError(f"not a plain SQL identifier: {name!r}")
    return name


def guard_trigger_names(tables: Iterable[str] = GUARDED_TABLES) -> list[str]:
    """``bh_guard_<table>_<ins|upd|del>`` for every guarded table and event."""
    return [f"bh_guard_{_ident(t)}_{ev[:3]}" for t in tables for ev in EVENTS]


def trigger_names() -> list[str]:
    """All :data:`TRIGGER_COUNT` ``bh_*`` triggers a complete install carries."""
    return [*guard_trigger_names(), *MONOTONIC_TRIGGERS]


#: 42 guard triggers + 2 monotonic fence triggers. A node short of them refuses to act as writer.
TRIGGER_COUNT = len(trigger_names())


# ---- statements ------------------------------------------------------------------------------


def fence_table_statements() -> list[str]:
    """The three fence tables and the ``dolt_ignore`` row, all idempotent."""
    return [
        "CREATE TABLE IF NOT EXISTS bh_writer (id INT PRIMARY KEY, frame VARCHAR(64) NOT NULL, "
        "epoch BIGINT NOT NULL, revision VARCHAR(64) NOT NULL)",
        "CREATE TABLE IF NOT EXISTS bh_epoch_live (id INT PRIMARY KEY, epoch BIGINT NOT NULL, "
        "UNIQUE KEY bh_epoch_live_epoch (epoch))",
        "CREATE TABLE IF NOT EXISTS bh_write_mark (id VARCHAR(64) PRIMARY KEY, "
        "epoch BIGINT NOT NULL, tbl VARCHAR(64) NOT NULL DEFAULT '', "
        "CONSTRAINT bh_write_mark_epoch FOREIGN KEY (epoch) REFERENCES bh_epoch_live (epoch))",
        f"REPLACE INTO dolt_ignore VALUES ({quote(IGNORE_PATTERN)}, 1)",
    ]


def seed_statements(writer: str, epoch: int, revision: str) -> list[str]:
    """First-install seed: name ``writer`` at ``epoch`` and insert the ``adopt-<epoch>``
    sentinel. Only for a node whose ``bh_writer`` does not exist yet (the caller reads that)."""
    epoch = int(epoch)
    if epoch < 1:
        raise ValueError(f"fence epoch must be >= 1 (got {epoch})")
    return [
        f"INSERT INTO bh_writer VALUES (1, {quote(writer)}, {epoch}, {quote(revision)})",
        f"INSERT INTO bh_epoch_live VALUES (1, {epoch})",
        f"INSERT INTO bh_write_mark (id, epoch, tbl) VALUES "
        f"({quote(ADOPT_SENTINEL_PREFIX + str(epoch))}, {epoch}, 'bh_writer')",
    ]


_PROCEDURE = f"""CREATE PROCEDURE {GUARD_PROCEDURE}()
begin
  declare w varchar(64);
  declare me varchar(64);
  declare r varchar(16);
  select frame into w from bh_writer where id = 1;
  select frame, role into me, r from {LOCAL_IDENT_TABLE} where id = 1;
  if coalesce(r, '') <> 'branch' and (w is null or me is null or w <> me) then
    signal sqlstate '45000' set message_text = '{GUARD_REFUSAL}';
  end if;
end"""

_GUARD_TRIGGER = """CREATE TRIGGER {name} BEFORE {event} ON `{table}` FOR EACH ROW
begin
  declare e bigint;
  if coalesce(active_branch(), 'main') = 'main' then
    select epoch into e from bh_writer where id = 1;
    insert into bh_write_mark (id, epoch, tbl) values (uuid(), e, '{table}');
    call bh_guard_check();
  end if;
end"""

_MONOTONIC_TRIGGER = f"""CREATE TRIGGER {{name}} BEFORE UPDATE ON {{table}} FOR EACH ROW
begin
  if new.epoch <= old.epoch then
    signal sqlstate '45000' set message_text = '{MONOTONIC_REFUSAL}';
  end if;
end"""


def guard_statements(tables: Sequence[str] = GUARDED_TABLES) -> list[str]:
    """The ``bh-sieai`` guard, drop-then-create: the procedure, then 3 triggers per table.

    In every trigger the mark is inserted inline BEFORE the ``CALL`` and the ``CALL`` is the last
    statement (Dolt skips statements after it, and drops procedure DML inside bd's explicit
    transactions). The fence is read into a DECLAREd local, never an ``@`` variable."""
    out = [f"DROP PROCEDURE IF EXISTS {GUARD_PROCEDURE}", _PROCEDURE]
    for table in tables:
        for event in EVENTS:
            name = f"bh_guard_{_ident(table)}_{event[:3]}"
            out.append(f"DROP TRIGGER IF EXISTS {name}")
            out.append(_GUARD_TRIGGER.format(name=name, event=event.upper(), table=table))
    return out


def monotonic_statements() -> list[str]:
    """The two monotonic ``BEFORE UPDATE`` fence triggers, drop-then-create (installed LAST)."""
    out: list[str] = []
    for name, table in zip(MONOTONIC_TRIGGERS, ("bh_writer", "bh_epoch_live"), strict=True):
        out.append(f"DROP TRIGGER IF EXISTS {name}")
        out.append(_MONOTONIC_TRIGGER.format(name=name, table=table))
    return out


def install_statements(seed: tuple[str, int, str] | None = None) -> list[str]:
    """The whole install in the composed order (``bh-jbb6r``): fence tables, the seed rows when
    ``seed = (writer, epoch, revision)`` is given (first install only), the guard, then the
    monotonic triggers last."""
    out = fence_table_statements()
    if seed is not None:
        out += seed_statements(*seed)
    return out + guard_statements() + monotonic_statements()


def ident_statements(frame: str, role: str = "replica") -> list[str]:
    """This node's ``bh_local_ident`` row. Never staged: run only once the ``dolt_ignore`` row
    is committed (:func:`beadhive.fence_data.FenceNode.provision_ident` enforces it)."""
    if role not in ROLES:
        raise ValueError(f"unknown guard role {role!r} (expected one of {ROLES})")
    if not frame:
        raise ValueError("a node identity needs a frame name")
    return [
        f"CREATE TABLE IF NOT EXISTS {LOCAL_IDENT_TABLE} (id INT PRIMARY KEY, "
        "frame VARCHAR(64), role VARCHAR(16) DEFAULT 'replica')",
        f"REPLACE INTO {LOCAL_IDENT_TABLE} VALUES (1, {quote(frame)}, {quote(role)})",
    ]


def render_script(statements: Sequence[str]) -> str:
    """One ``delimiter //`` script for ``dolt sql`` (the Dolt CLI splits on ``;`` even inside
    ``BEGIN … END``). Each statement ends ``//``, so a trigger reads ``…\\nend//``."""
    body = "".join(f"{s}{_DELIMITER}\n" for s in statements)
    return f"delimiter {_DELIMITER}\n{body}delimiter ;\n"
