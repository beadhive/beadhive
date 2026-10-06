"""The product in-data fence adapter: install, guard check, identity, and the M1 data switch.

bh-uz46l (P-M1), ADR ``docs/design/hive-writer-partitioning-adr.md`` §2 and conditions 1–3, 9
and 12. One :class:`FenceNode` is one node's copy of a hive's Dolt data, reached through one of
two engines, exactly as the bead names them:

* **embedded mode** — :class:`DoltCliEngine`: the Dolt CLI on ``.beads/embeddeddolt/<db>`` (bd 1.3
  has no ``bd sql`` in embedded mode). A statement batch is one ``delimiter //`` script; a commit
  is ``dolt add -A`` + ``dolt commit``.
* **server mode** — :class:`BdServerEngine`: statements through the server one at a time
  (``bd sql``), then ``bd dolt commit`` so the guard's marks travel with the write (``bh-vje85``
  E7).

What a node does:

* :meth:`FenceNode.install` — the fence + guard in the composed order, idempotent
  (:mod:`beadhive.fence_schema`), then checks that exactly :data:`TRIGGER_COUNT` = 44 ``bh_*``
  triggers are present (:meth:`FenceNode.guard_report`). :meth:`FenceNode.require_writer_ready`
  is the refusal a node short of 44 gets when asked to act as writer (condition 9; step 2 of
  adopt asks through :meth:`FenceNode.trigger_count`).
* :meth:`FenceNode.provision_ident` — this node's ``bh_local_ident``. Refuses until the
  ``dolt_ignore`` row ``bh_local_%`` is COMMITTED on ``HEAD``, and refuses if the table then
  shows up in ``dolt_status``: the identity is never staged, committed or pushed.
* :class:`~beadhive.writer_adopt.FenceData` (M2's port) and
  :class:`~beadhive.failover_reclaim.ReclaimData` (M3's port) for the coexistence adopt, and
  :class:`~beadhive.fence_audit.AuditReader` for :func:`~beadhive.fence_audit.fence_audit`.

**The data switch** — :func:`resolve_cut_over` registers itself as
:mod:`beadhive.fence_data_port`'s default resolver when this module is imported (:func:`register`;
every bh process entrypoint imports it before use). The port's own built-in default is "no
adapter", so a process that never imports this module treats every hive as legacy. The resolver
answers a :class:`FenceNode` ONLY for a hive whose local data already carries ``bh_writer``;
every other hive — no local store, an unknown mode, a
store without ``bh_writer``, or a probe that fails before the hive was ever seen cut over —
answers ``None`` and keeps today's legacy behaviour unchanged. Nothing here reads a host or fleet
config key: the hive's own data is the switch (condition 12). The probe is memoised per store
on a cheap filesystem fingerprint of ``.dolt/noms`` (manifest bytes + entry sizes), so an
unchanged store is probed once per process.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from . import fence_data_port, fence_schema, log, store_locator, writer_adopt
from .fence_schema import (
    IGNORE_PATTERN,
    INSTALL_COMMIT_MESSAGE,
    LOCAL_IDENT_TABLE,
    TRIGGER_COUNT,
    quote,
)
from .run import run
from .writer_adopt import DataUnreachable, WriterRow

__all__ = [
    "DEFAULT_TIMEOUT",
    "BdServerEngine",
    "DoltCliEngine",
    "FenceEngine",
    "FenceError",
    "FenceNode",
    "GuardIncomplete",
    "GuardReport",
    "IdentNotIgnored",
    "IdentStaged",
    "InstallReport",
    "SqlFailed",
    "engine_for",
    "node_for",
    "register",
    "reset_probe_cache",
    "resolve_cut_over",
]

DEFAULT_TIMEOUT = 120.0
_REMOTE = "origin"
_BRANCH = "main"
#: Push output that means "lost the race" (a non-fast-forward), not "could not reach the remote".
_NON_FF_MARKERS = (
    "non-fast-forward",
    "rejected",
    "fetch first",
    "behind its remote",
    "behind the remote",
)


# =============================================================================================
# Errors
# =============================================================================================


class FenceError(RuntimeError):
    """The in-data fence could not be installed, read or provisioned."""


class SqlFailed(FenceError):
    """A statement failed on the node's engine."""


class GuardIncomplete(FenceError):
    """This node carries fewer than the 44 ``bh_*`` triggers (or no guard procedure): it
    refuses to act as writer (condition 9, ``bh-jbb6r`` E6)."""


class IdentNotIgnored(FenceError):
    """``bh_local_ident`` provisioning refused: the ``dolt_ignore`` row ``bh_local_%`` is not
    committed on ``HEAD``, so the identity table could be staged and pushed."""


class IdentStaged(FenceError):
    """``bh_local_ident`` shows up in ``dolt_status`` — it would be committed and pushed."""


def _missing_table(exc: Exception, table: str) -> bool:
    text = str(exc).lower()
    return "not found" in text and table.lower() in text


# =============================================================================================
# Engines
# =============================================================================================


class FenceEngine(Protocol):
    """How one node's Dolt data is reached (embedded CLI or bd's server)."""

    def query(self, sql: str) -> list[dict]: ...

    def execute(self, statements: Sequence[str]) -> None: ...

    def commit(self, message: str) -> bool:
        """Commit the working set; ``False`` when there was nothing to commit."""
        ...

    def fetch(self) -> None: ...

    def reset_to_remote(self) -> None: ...

    def push(self) -> bool: ...

    @property
    def store(self) -> Path: ...


def _rows(stdout: str) -> list[dict]:
    text = (stdout or "").strip()
    if not text:
        return []
    start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
    if start < 0:
        raise SqlFailed(f"unparseable SQL output: {text[:200]}")
    data = json.loads(text[start:])
    rows = data.get("rows", []) if isinstance(data, dict) else data
    return [dict(r) for r in rows or []]


def _output(res: subprocess.CompletedProcess) -> str:
    return f"{res.stdout or ''}{res.stderr or ''}".strip()


def _push_outcome(res: subprocess.CompletedProcess, where: str) -> bool:
    if res.returncode == 0:
        return True
    text = _output(res)
    if any(marker in text.lower() for marker in _NON_FF_MARKERS):
        return False
    raise DataUnreachable(f"push from {where} failed: {text[:400]}")


class DoltCliEngine:
    """Embedded mode: the Dolt CLI run in ``.beads/embeddeddolt/<db>``."""

    def __init__(
        self,
        db_dir: Path,
        *,
        remote: str = _REMOTE,
        branch: str = _BRANCH,
        env: Mapping[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.db_dir = Path(db_dir)
        self.remote, self.branch = remote, branch
        self.env = dict(env) if env is not None else None
        self.timeout = timeout

    @property
    def store(self) -> Path:
        return self.db_dir

    def _dolt(self, *args: str, text_input: str | None = None) -> subprocess.CompletedProcess:
        try:
            return run(
                ["dolt", *args],
                check=False,
                capture=True,
                cwd=str(self.db_dir),
                env=self.env,
                text_input=text_input,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise SqlFailed(f"dolt {' '.join(args[:2])} timed out after {self.timeout}s") from exc

    def query(self, sql: str) -> list[dict]:
        res = self._dolt("sql", "-r", "json", "-q", sql)
        if res.returncode != 0:
            raise SqlFailed(f"{self.db_dir}: {_output(res)[:400]}")
        return _rows(res.stdout)

    def execute(self, statements: Sequence[str]) -> None:
        if not statements:
            return
        res = self._dolt("sql", text_input=fence_schema.render_script(statements))
        if res.returncode != 0:
            raise SqlFailed(f"{self.db_dir}: {_output(res)[:400]}")

    def commit(self, message: str) -> bool:
        if not self.query("SELECT table_name FROM dolt_status"):
            return False
        for args in (("add", "-A"), ("commit", "-m", message)):
            res = self._dolt(*args)
            if res.returncode != 0:
                raise SqlFailed(f"dolt {args[0]} in {self.db_dir}: {_output(res)[:400]}")
        return True

    def fetch(self) -> None:
        res = self._dolt("fetch", self.remote)
        if res.returncode != 0:
            raise DataUnreachable(f"fetch {self.remote} in {self.db_dir}: {_output(res)[:400]}")

    def reset_to_remote(self) -> None:
        self._dolt("merge", "--abort")  # no-op unless a refused pull left a merge open
        self.fetch()
        res = self._dolt("reset", "--hard", f"{self.remote}/{self.branch}")
        if res.returncode != 0:
            raise DataUnreachable(f"reset to {self.remote}/{self.branch}: {_output(res)[:400]}")

    def push(self) -> bool:
        return _push_outcome(self._dolt("push", self.remote, self.branch), str(self.db_dir))


class BdServerEngine:
    """Server mode: statements through bd's ``dolt sql-server`` (``bd sql``), commits by
    ``bd dolt commit``."""

    def __init__(
        self,
        hive_dir: Path,
        *,
        remote: str = _REMOTE,
        branch: str = _BRANCH,
        env: Mapping[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        db_dir: Path | None = None,
    ):
        self.hive_dir = Path(hive_dir)
        self.remote, self.branch = remote, branch
        self.env = dict(env) if env is not None else None
        self.timeout = timeout
        self._db_dir = db_dir

    @property
    def store(self) -> Path:
        return self._db_dir if self._db_dir is not None else self.hive_dir

    def _bd(self, *args: str) -> subprocess.CompletedProcess:
        """``bd -C <hive> <args>``.

        bd-seam-justified: the fence adapter sits below guard/host_adopt in the import graph
        (the write guard consults it), and :mod:`beadhive.bd` imports :mod:`beadhive.guard`, so
        routing through the high-level bd adapter would close that graph into a cycle."""
        try:
            return run(
                ["bd", "-C", str(self.hive_dir), *args],
                check=False,
                capture=True,
                env=self.env,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise SqlFailed(f"bd {' '.join(args[:2])} timed out after {self.timeout}s") from exc

    def query(self, sql: str) -> list[dict]:
        res = self._bd("sql", "--json", sql)
        if res.returncode != 0:
            raise SqlFailed(f"{self.hive_dir}: {_output(res)[:400]}")
        return _rows(res.stdout)

    def execute(self, statements: Sequence[str]) -> None:
        for statement in statements:
            res = self._bd("sql", statement)
            if res.returncode != 0:
                raise SqlFailed(f"{self.hive_dir}: {statement[:80]!r}: {_output(res)[:400]}")

    def commit(self, message: str) -> bool:
        if not self.query("SELECT table_name FROM dolt_status"):
            return False
        res = self._bd("dolt", "commit", "-m", message)
        if res.returncode != 0:
            raise SqlFailed(f"bd dolt commit in {self.hive_dir}: {_output(res)[:400]}")
        return True

    def fetch(self) -> None:
        res = self._bd("sql", f"CALL DOLT_FETCH({quote(self.remote)})")
        if res.returncode != 0:
            raise DataUnreachable(f"fetch {self.remote} via {self.hive_dir}: {_output(res)[:400]}")

    def reset_to_remote(self) -> None:
        self.fetch()
        target = quote(f"{self.remote}/{self.branch}")
        res = self._bd("sql", f"CALL DOLT_RESET('--hard', {target})")
        if res.returncode != 0:
            raise DataUnreachable(f"reset to {self.remote}/{self.branch}: {_output(res)[:400]}")

    def push(self) -> bool:
        args = ["dolt", "push"] + (["--remote", self.remote] if self.remote != _REMOTE else [])
        return _push_outcome(self._bd(*args), str(self.hive_dir))


# =============================================================================================
# The node
# =============================================================================================


@dataclass(frozen=True)
class GuardReport:
    """Which ``bh_*`` triggers (and the guard procedure) one node carries."""

    present: frozenset[str]
    procedure: bool

    @property
    def expected(self) -> tuple[str, ...]:
        return tuple(fence_schema.trigger_names())

    @property
    def missing(self) -> tuple[str, ...]:
        return tuple(n for n in self.expected if n not in self.present)

    @property
    def extra(self) -> tuple[str, ...]:
        return tuple(sorted(self.present - set(self.expected)))

    @property
    def count(self) -> int:
        """Expected triggers present (an unknown ``bh_*`` trigger never makes up for one)."""
        return TRIGGER_COUNT - len(self.missing)

    @property
    def total(self) -> int:
        """Every ``bh_*`` trigger on the node."""
        return len(self.present)

    @property
    def complete(self) -> bool:
        return self.procedure and not self.missing

    def describe(self) -> str:
        head = f"{self.count} of {TRIGGER_COUNT} bh_* triggers installed"
        extra = []
        if not self.procedure:
            extra.append(f"no {fence_schema.GUARD_PROCEDURE}() procedure")
        if self.missing:
            extra.append("missing " + ", ".join(self.missing[:6]))
        if self.extra:
            extra.append("unexpected " + ", ".join(self.extra[:6]))
        return head + (f" ({'; '.join(extra)})" if extra else "")


@dataclass(frozen=True)
class InstallReport:
    #: True when this install created the fence (seeded ``bh_writer``); False on a re-install.
    seeded: bool
    #: True when the install produced a commit (False: nothing changed).
    committed: bool
    writer: WriterRow
    guard: GuardReport


class FenceNode:
    """One node's copy of a hive's data, through one :class:`FenceEngine`."""

    def __init__(self, engine: FenceEngine, *, remote: str = _REMOTE, branch: str = _BRANCH):
        self.engine = engine
        self.remote, self.branch = remote, branch

    def __repr__(self) -> str:
        return f"FenceNode({type(self.engine).__name__} {self.engine.store})"

    # ---- AuditReader ---------------------------------------------------------------------

    def query(self, sql: str) -> list[dict]:
        return self.engine.query(sql)

    def fetch(self) -> None:
        self.engine.fetch()

    # ---- reads ---------------------------------------------------------------------------

    def is_cut_over(self) -> bool:
        """Whether local ``main``'s working data carries ``bh_writer``."""
        rows = self.query(
            "SELECT COUNT(*) AS n FROM information_schema.tables "
            "WHERE table_schema = database() AND table_name = 'bh_writer'"
        )
        return bool(rows and int(next(iter(rows[0].values())) or 0))

    def _writer(self, suffix: str = "") -> WriterRow | None:
        try:
            rows = self.query(f"SELECT frame, epoch, revision FROM bh_writer{suffix} WHERE id = 1")
        except SqlFailed as exc:
            if _missing_table(exc, "bh_writer"):
                return None
            raise
        if not rows:
            return None
        row = rows[0]
        return WriterRow(str(row["frame"]), int(row["epoch"]), str(row.get("revision") or ""))

    def guard_report(self) -> GuardReport:
        rows = self.query(
            "SELECT trigger_name FROM information_schema.triggers WHERE trigger_schema = database()"
        )
        present = frozenset(
            str(next(iter(r.values())))
            for r in rows
            if str(next(iter(r.values()), "")).startswith("bh_")
        )
        procs = self.query(
            "SELECT routine_name FROM information_schema.routines "
            f"WHERE routine_schema = database() AND routine_name = "
            f"{quote(fence_schema.GUARD_PROCEDURE)}"
        )
        return GuardReport(present=present, procedure=bool(procs))

    def require_writer_ready(self) -> GuardReport:
        """The guard check a node must pass before it acts as writer (condition 9)."""
        report = self.guard_report()
        if not report.complete:
            raise GuardIncomplete(
                f"refusing to act as writer: {report.describe()} — re-run the fence install "
                "on this node"
            )
        return report

    def ident(self) -> tuple[str, str] | None:
        """This node's ``(frame, role)`` from ``bh_local_ident``, or ``None`` when unprovisioned."""
        try:
            rows = self.query(f"SELECT frame, role FROM {LOCAL_IDENT_TABLE} WHERE id = 1")
        except SqlFailed as exc:
            if _missing_table(exc, LOCAL_IDENT_TABLE):
                return None
            raise
        return (str(rows[0]["frame"]), str(rows[0]["role"])) if rows else None

    def ignore_committed(self) -> bool:
        """Whether ``dolt_ignore`` carries ``bh_local_%`` as ignored, committed on ``HEAD``
        and still in the working set."""
        where = f"WHERE pattern = {quote(IGNORE_PATTERN)}"
        try:
            head = self.query(f"SELECT ignored FROM dolt_ignore AS OF 'HEAD' {where}")
            working = self.query(f"SELECT ignored FROM dolt_ignore {where}")
        except SqlFailed as exc:
            if _missing_table(exc, "dolt_ignore"):
                return False
            raise

        def on(rows: list[dict]) -> bool:
            return bool(rows) and str(rows[0].get("ignored")).lower() in ("1", "true")

        return on(head) and on(working)

    def staged_local_tables(self) -> list[str]:
        """``bh_local_*`` tables ``dolt_status`` lists (must always be empty)."""
        rows = self.query("SELECT table_name FROM dolt_status")
        return sorted(
            {
                str(r["table_name"])
                for r in rows
                if str(r.get("table_name", "")).startswith("bh_local_")
            }
        )

    # ---- install + provisioning ----------------------------------------------------------

    def install(
        self,
        writer: str,
        epoch: int,
        *,
        revision: str | None = None,
        message: str = INSTALL_COMMIT_MESSAGE,
    ) -> InstallReport:
        """Install (or re-install) the fence and guard on this node, in the composed order, and
        commit it. Not pushed: publishing is the caller's step.

        Idempotent: the tables are created only if absent and never dropped; the seed rows
        (``writer`` at ``epoch``, a fresh revision, the ``adopt-<epoch>`` sentinel) are written
        only when this node has no ``bh_writer`` yet; the procedure and every trigger are
        drop-then-create. A re-install ignores ``writer`` / ``epoch`` — only adopt moves the
        writer. Run it on a node synced to the remote head: seeding a node that merely lags a
        remote that is already cut over forks the fence.

        A drop-then-create leaves a moment without that trigger, so re-install a writer while
        it is quiet. Raises :class:`GuardIncomplete` when the install did not leave all 44
        ``bh_*`` triggers and the procedure in place."""
        current = self._writer()
        seed = None
        if current is None:
            fresh = revision or writer_adopt.fresh_revision(writer, int(epoch))
            seed = (writer, int(epoch), fresh)
        self.engine.execute(fence_schema.install_statements(seed))
        committed = self.engine.commit(message)
        report = self.require_writer_ready()
        row = self._writer()
        if row is None:  # pragma: no cover - the install just created it
            raise FenceError("install left no bh_writer row")
        return InstallReport(seeded=seed is not None, committed=committed, writer=row, guard=report)

    def provision_ident(self, frame: str, role: str = "replica") -> None:
        """Give this node its identity row (``dolt_ignore``'d: never committed or pushed).

        Refuses (:class:`IdentNotIgnored`) unless the ``bh_local_%`` ignore row is committed on
        ``HEAD`` — before that, the new table would show in ``dolt_status`` and the next
        ``commit -A`` would publish it. Refuses (:class:`IdentStaged`) if the table shows in
        ``dolt_status`` anyway. Do this right after the node joins, before any bd write
        (``bh-sieai`` E6); until then the guard fails closed on ``main``."""
        if not self.ignore_committed():
            raise IdentNotIgnored(
                f"refusing to provision {LOCAL_IDENT_TABLE}: the dolt_ignore row "
                f"'{IGNORE_PATTERN}' is not committed on HEAD of {self.engine.store} — install "
                "and commit the fence (or pull it) first"
            )
        self.engine.execute(fence_schema.ident_statements(frame, role))
        staged = self.staged_local_tables()
        if staged:
            raise IdentStaged(
                f"{', '.join(staged)} shows in dolt_status on {self.engine.store}: it would be "
                "committed and pushed — check dolt_ignore before any commit"
            )

    # ---- writer_adopt.FenceData ----------------------------------------------------------

    def sync_to_remote(self) -> None:
        try:
            self.engine.reset_to_remote()
        except SqlFailed as exc:
            raise DataUnreachable(str(exc)) from exc

    def writer(self) -> WriterRow | None:
        return self._writer()

    def remote_writer(self) -> WriterRow | None:
        self.engine.fetch()
        return self._writer(f" AS OF {quote(f'{self.remote}/{self.branch}')}")

    def history_max_epoch(self) -> int:
        """The highest ``bh_writer.epoch`` local ``main``'s history ever carried (0 if none).
        Read from ``dolt_history_bh_writer`` (one row per commit), never ``dolt_diff``, which
        drops commits on a merged history."""
        try:
            rows = self.query(
                "SELECT COALESCE(MAX(epoch), 0) AS m FROM dolt_history_bh_writer WHERE id = 1"
            )
        except SqlFailed as exc:
            if _missing_table(exc, "bh_writer"):
                return 0
            raise
        return int(next(iter(rows[0].values())) or 0) if rows else 0

    def trigger_count(self) -> int:
        return self.guard_report().count

    def commit_bump(self, statements: Sequence[str], message: str) -> None:
        self.engine.execute(list(statements))
        if not self.engine.commit(message):
            raise FenceError(f"adopt bump {message!r} changed nothing on {self.engine.store}")

    def push(self) -> bool:
        return self.engine.push()

    # ---- failover_reclaim.ReclaimData ----------------------------------------------------

    def config_rows(self) -> dict[str, str]:
        rows = self.query("SELECT `key` AS k, `value` AS v FROM config WHERE `key` LIKE 'bh.%'")
        return {str(r["k"]): str(r["v"]) for r in rows}

    def claims(self) -> list:
        from .failover_reclaim import Claim  # lazy: reclaim is only needed on a failover adopt

        rows = self.query(
            "SELECT i.id AS id, i.assignee AS assignee, l.label AS label FROM issues i "
            "LEFT JOIN labels l ON l.issue_id = i.id WHERE i.status = 'in_progress'"
        )
        by: dict[str, tuple[str, set[str]]] = {}
        for r in rows:
            assignee, labels = by.setdefault(str(r["id"]), (str(r.get("assignee") or ""), set()))
            if r.get("label"):
                labels.add(str(r["label"]))
        return [Claim(bead, a, frozenset(labels)) for bead, (a, labels) in sorted(by.items())]


# =============================================================================================
# Locating a hive's store, and the data switch
# =============================================================================================


def engine_for(hive_dir: Path) -> FenceEngine | None:
    """The engine for ``hive_dir``'s store per bd's own persisted mode, or ``None`` when the
    mode is unknown or the store is not on this host. Filesystem facts only, no subprocess."""
    hive_dir = Path(hive_dir)
    mode = store_locator.dolt_mode(hive_dir)
    if mode == "embedded":
        db = store_locator.embedded_database_dir(hive_dir)
        return DoltCliEngine(db) if (db / ".dolt").is_dir() else None
    if mode == "server":
        db = store_locator.database_dir(hive_dir)
        return BdServerEngine(hive_dir, db_dir=db if (db / ".dolt").is_dir() else None)
    return None


def node_for(hive_dir: Path) -> FenceNode | None:
    """A :class:`FenceNode` for ``hive_dir`` whether or not it is cut over (no probe)."""
    engine = engine_for(hive_dir)
    return FenceNode(engine) if engine is not None else None


#: ``store -> (fingerprint, cut_over)`` for the current process, and the stores ever seen cut
#: over (a later failing probe on one of those keeps the adapter, so it fails CLOSED there).
_probes: dict[str, tuple[tuple, bool]] = {}
_seen_cut_over: set[str] = set()


def reset_probe_cache() -> None:
    """Forget every memoised probe (tests; a long-lived process after an operator cutover)."""
    _probes.clear()
    _seen_cut_over.clear()


def _fingerprint(store: Path) -> tuple | None:
    """A cheap fingerprint of a Dolt store's data: the ``manifest`` bytes plus ``(name, size)``
    of every ``.dolt/noms`` entry. Every write appends to the chunk journal (or rewrites the
    manifest / adds a table file), so it moves; a READ does not. Never mtimes: Dolt touches
    ``manifest`` and the journal on every open, read-only queries included (measured, 2.3.5),
    so an mtime key would re-probe on every call."""
    noms = Path(store) / ".dolt" / "noms"
    try:
        manifest = (noms / "manifest").read_bytes()
        with os.scandir(noms) as entries:
            sizes = tuple(sorted((e.name, e.stat().st_size) for e in entries))
    except OSError:
        return None
    return (manifest, sizes)


def resolve_cut_over(prefix: str, hive_dir: Path) -> FenceNode | None:
    """The product :data:`~beadhive.host_adopt.FenceDataResolver`: a :class:`FenceNode` for a
    hive whose local data carries ``bh_writer``, else ``None`` (legacy, today's behaviour).

    Never raises. A failing probe answers ``None`` unless this process has already seen the
    store cut over — then it answers the node, whose own reads fail closed."""
    node = node_for(hive_dir)
    if node is None:
        return None
    key = str(node.engine.store)
    stamp = _fingerprint(node.engine.store)
    cached = _probes.get(key)
    if stamp is not None and cached is not None and cached[0] == stamp:
        return node if cached[1] else None
    try:
        cut_over = node.is_cut_over()
    except Exception as exc:  # noqa: BLE001 — a probe failure must never break a legacy hive
        log.get_logger(__name__).warning(
            "fence_data_probe_failed",
            hive_prefix=prefix,
            store=key,
            error=str(exc)[:300],
            reason=(
                "could not read whether the hive's data carries bh_writer; "
                + (
                    "it was seen cut over, so the adapter stays (fails closed)"
                    if key in _seen_cut_over
                    else "treating it as legacy (today's behaviour)"
                )
            ),
        )
        return node if key in _seen_cut_over else None
    if stamp is not None:
        _probes[key] = (stamp, cut_over)
    if cut_over:
        _seen_cut_over.add(key)
        return node
    _seen_cut_over.discard(key)
    return None


def register() -> None:
    """Register :func:`resolve_cut_over` as :mod:`beadhive.fence_data_port`'s default resolver.

    Idempotent; runs at import. Process entrypoints that can reach the guard, lease or adopt
    paths call it once before use, so the product adapter is in place in every bh process."""
    fence_data_port.register_default_resolver(resolve_cut_over)


register()
