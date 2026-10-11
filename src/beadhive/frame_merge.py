"""The writer merges ``open``-policy frame refs into ``main`` without ``--strategy`` (bh-sfp9o.5).

Trust-profiles ADR section 4, "Merging frame refs". Under the ``open`` write policy a replica
publishes its own ``main`` to ``refs/dolt/frame/<id>`` (:mod:`beadhive.write_policy_publish`).
The hive's writer merges each such ref into its local ``main`` here:

* **Never ``--strategy``.** ``bd vc merge --strategy`` resolves a conflict by silently dropping
  the losing side — a whole child bead, or one frame's claim (``bh-sieai`` section 9). Every
  merge first previews its conflicts (``DOLT_PREVIEW_MERGE_CONFLICTS``). A ref with any
  conflict is left unmerged, and its conflicting rows are recorded as a typed outcome: the
  table, the bead, and both sides' values (only the columns that differ). Two frames claiming
  one bead is reported as a **double claim** naming both holders.
* **A clean ref lands** as one ``--no-ff`` merge commit made in ONE SQL session, the way the
  orphan merge does it (:func:`beadhive.fence_orphan.merge_orphan`): the frame's write marks are
  re-stamped at the live epoch, and the subject starts ``bh: merge frame/`` so the fence audit
  sanctions it. A merge that did not land as exactly that commit is rolled back to the
  pre-merge ``main``.
* **Clone-local bookkeeping** is never merged: bd writes a clone's own ``metadata.clone_id`` on
  every join, so every frame ref after the first conflicts on it. It is frame-local: the merge
  always keeps the writer's value and the outcome says so; it is bd's per-clone identity, not
  bead data (``bh-xwy5x`` decision, condition 2).
* **An id collision is never resolved by taking a side** (``bh-xwy5x`` decision, condition 3):
  when ``main`` and the frame each created a different bead under one id, either side would drop
  a bead. The conflict is recorded as a collision naming both beads, and :func:`resolve` refuses
  it: the frame re-creates its bead under a new id and publishes again.
* **The operator resolves** a recorded conflict per bead (:func:`resolve`): take ``main``'s side
  or the frame's side. There is no default; a resolve without an explicit side is refused.
  Once every recorded bead of a frame has a side the merge completes, inside the same one
  session: ``main``'s side drops that bead's conflict rows (``ours`` stays), the frame's side
  takes the frame's rows through ``DOLT_CONFLICTS_RESOLVE('--theirs')`` — never DML on a guarded
  table, whose trigger would close the open merge.
* **Isolation.** Each frame ref is merged on its own; a conflicting or failing ref never stops
  the others, and nothing here is on a verb's path. :func:`after_pull` is the seam the
  ``hive.sync`` loop (``bh-midl0``) calls when auto-pull runs on the writer: it never raises.

Outcomes are recorded per hive and frame in ``$BH_HOME/state/frame-merge.json`` (the
``bh-nc052.2`` contract shape: a status, the attempt time, the heads involved, positive conflict
evidence and the error), read by ``bh doctor`` and ``bh hq why``. Merges land on the writer's
local ``main``; publishing it is the writer's managed push, as for any other write.

Typer-free; the CLI is :mod:`beadhive.hive_frames_cli` (``bh hive frames``).
"""

from __future__ import annotations

import contextlib
import copy
import fcntl
import importlib
import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from . import log
from .fence_data import FenceError, FenceNode, SqlFailed
from .fence_schema import quote
from .writer_adopt import DataUnreachable

__all__ = [
    "CLONE_LOCAL",
    "MAIN",
    "FRAME",
    "SIDES",
    "STATE_FILE",
    "ConflictRow",
    "DoubleClaim",
    "FrameMergeOutcome",
    "FrameMergeRefused",
    "Status",
    "after_pull",
    "doctor_data",
    "list_frames",
    "load",
    "merge_frame",
    "merge_hive",
    "resolve",
    "why_summary",
]

#: The two sides an operator may take for one bead.
MAIN = "main"
FRAME = "frame"
SIDES = (MAIN, FRAME)

STATE_FILE = "frame-merge.json"

#: ``(table, key)`` rows bd writes per clone: never merged from a frame, main's value kept.
CLONE_LOCAL: tuple[tuple[str, str], ...] = (("metadata", "clone_id"),)

_FRAME_REF_PREFIX = "refs/dolt/frame/"
_FRAME_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_HASH = re.compile(r"[0-9a-v]{32}")
_TABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
#: The conflict-preview columns that are bookkeeping, not row values.
_PREVIEW_META = frozenset({"dolt_conflict_id", "from_root_ish", "our_diff_type", "their_diff_type"})
#: Columns that change on every write; they never decide that two rows differ.
_NOISE_COLUMNS = frozenset({"updated_at", "row_lock", "content_hash"})
_LS_REMOTE_TIMEOUT = 60.0


class Status:
    """A frame ref's merge outcome."""

    MERGED = "merged"
    UP_TO_DATE = "up_to_date"
    CONFLICT = "conflict"
    FAILED = "failed"

    ALL = (MERGED, UP_TO_DATE, CONFLICT, FAILED)


class FrameMergeRefused(FenceError):
    """The merge or resolve was refused before anything was written."""


# =============================================================================================
# The typed outcome
# =============================================================================================


@dataclass(frozen=True)
class ConflictRow:
    """One conflicting row of a frame ref against ``main``."""

    table: str
    #: The bead the row belongs to; ``""`` for a row that is no bead's (its key is in ``key``).
    bead: str
    #: The row's primary key, or the bead column's value.
    key: str
    #: The columns that differ, with ``main``'s and the frame's value.
    main: Mapping[str, Any]
    frame: Mapping[str, Any]
    #: How each side changed the row since their common ancestor (``added`` / ``modified`` /
    #: ``removed``).
    main_change: str = ""
    frame_change: str = ""
    #: ``True`` for a schema conflict (no row values; resolved by hand only).
    schema: bool = False
    #: ``True`` when ``main`` and the frame each ADDED a different bead under this id: taking
    #: either side would drop a bead, so no side is ever taken (``bh-xwy5x`` condition 3).
    collision: bool = False

    def describe_collision(self) -> str:
        ours, theirs = self.main.get("title"), self.frame.get("title")
        return (
            f"id collision on {self.bead}: main and the frame each created a different bead "
            f"under it (main {ours!r}, frame {theirs!r}); taking either side would drop one"
        )

    @property
    def subject(self) -> str:
        """What the operator resolves: the bead, else ``<table>:<key>``."""
        return self.bead or f"{self.table}:{self.key}"

    def as_dict(self) -> dict:
        return {
            "table": self.table,
            "bead": self.bead,
            "key": self.key,
            "subject": self.subject,
            "main": dict(self.main),
            "frame": dict(self.frame),
            "main_change": self.main_change,
            "frame_change": self.frame_change,
            "schema": self.schema,
            "collision": self.collision,
        }

    @classmethod
    def from_dict(cls, data: Mapping) -> ConflictRow:
        return cls(
            table=str(data.get("table") or ""),
            bead=str(data.get("bead") or ""),
            key=str(data.get("key") or ""),
            main=dict(data.get("main") or {}),
            frame=dict(data.get("frame") or {}),
            main_change=str(data.get("main_change") or ""),
            frame_change=str(data.get("frame_change") or ""),
            schema=bool(data.get("schema")),
            collision=bool(data.get("collision")),
        )


@dataclass(frozen=True)
class DoubleClaim:
    """Two frames claimed one bead: ``main`` holds one claim, the frame ref the other."""

    bead: str
    main_holder: str
    frame_holder: str
    frame: str

    @property
    def holders(self) -> tuple[str, str]:
        return (self.main_holder, self.frame_holder)

    def describe(self) -> str:
        return (
            f"{self.bead} is claimed twice: by {self.main_holder} on main and by "
            f"{self.frame_holder} on refs/dolt/frame/{self.frame}"
        )

    def as_dict(self) -> dict:
        return {
            "bead": self.bead,
            "holders": list(self.holders),
            "main_holder": self.main_holder,
            "frame_holder": self.frame_holder,
            "frame": self.frame,
        }

    @classmethod
    def from_dict(cls, data: Mapping) -> DoubleClaim:
        return cls(
            bead=str(data.get("bead") or ""),
            main_holder=str(data.get("main_holder") or ""),
            frame_holder=str(data.get("frame_holder") or ""),
            frame=str(data.get("frame") or ""),
        )


@dataclass(frozen=True)
class FrameMergeOutcome:
    """The typed outcome of one frame ref's merge attempt (``bh-nc052.2`` contract shape)."""

    hive: str
    frame: str
    status: str
    #: When the attempt ran (epoch seconds, from the injected clock).
    at: float
    #: The frame ref's head and ``main`` before the attempt.
    frame_head: str = ""
    main_before: str = ""
    #: The merge commit, for ``merged``.
    commit: str = ""
    conflicts: tuple[ConflictRow, ...] = ()
    double_claims: tuple[DoubleClaim, ...] = ()
    #: The side the operator took per subject (bead), kept until the merge completes.
    resolutions: Mapping[str, str] = field(default_factory=dict)
    #: Clone-local rows the frame changed; main's value was kept.
    kept_main: tuple[str, ...] = ()
    #: Write marks the merge re-stamped at the live epoch.
    restamped: int = 0
    error: str = ""

    def __post_init__(self) -> None:
        if self.status not in Status.ALL:
            raise ValueError(f"unknown frame merge status {self.status!r}")

    @property
    def ref(self) -> str:
        return f"{_FRAME_REF_PREFIX}{self.frame}"

    @property
    def subjects(self) -> tuple[str, ...]:
        """The beads (or ``<table>:<key>`` rows) that need a side, in order."""
        seen: dict[str, None] = {}
        for row in self.conflicts:
            seen.setdefault(row.subject, None)
        return tuple(seen)

    @property
    def unresolved(self) -> tuple[str, ...]:
        return tuple(s for s in self.subjects if s not in self.resolutions)

    @property
    def collisions(self) -> tuple[ConflictRow, ...]:
        return tuple(row for row in self.conflicts if row.collision)

    @property
    def held(self) -> bool:
        """The ref is held unmerged and needs the operator."""
        return self.status in (Status.CONFLICT, Status.FAILED)

    def describe(self) -> str:
        if self.status == Status.MERGED:
            tail = f" (kept main's {', '.join(self.kept_main)})" if self.kept_main else ""
            return f"{self.ref} merged into main as {self.commit[:12]}{tail}"
        if self.status == Status.UP_TO_DATE:
            return f"{self.ref} is already in main"
        if self.status == Status.CONFLICT:
            beads = ", ".join(self.subjects) or "?"
            claims = "".join(f"; {claim.describe()}" for claim in self.double_claims)
            claims += "".join(f"; {row.describe_collision()}" for row in self.collisions)
            return (
                f"{self.ref} is held unmerged: {len(self.conflicts)} conflicting row(s) on "
                f"{beads}{claims}"
            )
        return f"{self.ref} did not merge: {self.error}"

    def as_dict(self) -> dict:
        return {
            "hive": self.hive,
            "frame": self.frame,
            "ref": self.ref,
            "status": self.status,
            "at": self.at,
            "frame_head": self.frame_head,
            "main_before": self.main_before,
            "commit": self.commit,
            "conflicts": [row.as_dict() for row in self.conflicts],
            "double_claims": [claim.as_dict() for claim in self.double_claims],
            "resolutions": dict(self.resolutions),
            "unresolved": list(self.unresolved),
            "kept_main": list(self.kept_main),
            "restamped": self.restamped,
            "error": self.error,
            "summary": self.describe(),
        }

    @classmethod
    def from_dict(cls, data: Mapping) -> FrameMergeOutcome:
        status = str(data.get("status") or Status.FAILED)
        return cls(
            hive=str(data.get("hive") or ""),
            frame=str(data.get("frame") or ""),
            status=status if status in Status.ALL else Status.FAILED,
            at=float(data.get("at") or 0.0),
            frame_head=str(data.get("frame_head") or ""),
            main_before=str(data.get("main_before") or ""),
            commit=str(data.get("commit") or ""),
            conflicts=tuple(ConflictRow.from_dict(r) for r in data.get("conflicts") or ()),
            double_claims=tuple(DoubleClaim.from_dict(c) for c in data.get("double_claims") or ()),
            resolutions={str(k): str(v) for k, v in (data.get("resolutions") or {}).items()},
            kept_main=tuple(str(k) for k in data.get("kept_main") or ()),
            restamped=int(data.get("restamped") or 0),
            error=str(data.get("error") or ""),
        )


# =============================================================================================
# The record ($BH_HOME/state/frame-merge.json)
# =============================================================================================


def _state_path() -> Path | None:
    try:
        return Path(importlib.import_module("beadhive.config").home()) / "state" / STATE_FILE
    except Exception:  # noqa: BLE001 - no home: nothing is recorded
        return None


@contextlib.contextmanager
def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(path.with_name(f"{path.name}.lock"), "a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(path: Path, data: Mapping) -> None:
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def load(*, path: Path | None = None) -> dict[str, dict[str, FrameMergeOutcome]]:
    """Every recorded outcome: ``{hive: {frame: outcome}}``. Never raises."""
    path = path or _state_path()
    if path is None:
        return {}
    out: dict[str, dict[str, FrameMergeOutcome]] = {}
    for hive, frames in _read(path).items():
        if not isinstance(frames, Mapping):
            continue
        for frame, data in frames.items():
            if not isinstance(data, Mapping):
                continue
            try:
                out.setdefault(str(hive), {})[str(frame)] = FrameMergeOutcome.from_dict(data)
            except (TypeError, ValueError):
                continue
    return out


def recorded(hive: str, frame: str, *, path: Path | None = None) -> FrameMergeOutcome | None:
    return load(path=path).get(hive, {}).get(frame)


def record(outcome: FrameMergeOutcome, *, path: Path | None = None) -> None:
    """Record `outcome` as the latest for its hive and frame. Never raises."""
    path = path or _state_path()
    if path is None:
        return
    try:
        with _locked(path):
            data = _read(path)
            data.setdefault(outcome.hive, {})[outcome.frame] = outcome.as_dict()
            _write(path, data)
    except OSError as exc:
        log.get_logger(__name__).warning("frame_merge_unrecorded", error=str(exc))


# =============================================================================================
# Reads
# =============================================================================================


def _scalar(rows: list[dict]) -> object:
    return next(iter(rows[0].values())) if rows and rows[0] else None


def _hashof(node: FenceNode, rev: str) -> str:
    text = str(_scalar(node.query(f"SELECT hashof({quote(rev)}) AS h")) or "")
    if not _HASH.fullmatch(text):
        raise SqlFailed(f"hashof({rev}) is not a commit hash: {text!r}")
    return text


def _contains(node: FenceNode, commit: str, rev: str) -> bool:
    c = quote(commit)
    rows = node.query(f"SELECT dolt_merge_base({c}, {quote(rev)}) = {c} AS m")
    return str(_scalar(rows)).strip().lower() in {"1", "true"}


def _frame_id(frame: str) -> str:
    if not _FRAME_ID.fullmatch(frame or ""):
        raise FrameMergeRefused(f"frame id {frame!r} cannot name a frame-private data ref")
    return frame


def _git_url(dolt_url: str) -> str:
    """The plain Git URL behind a Dolt ``git+…`` remote URL, else ``""``."""
    url = str(dolt_url or "")
    if not url.startswith("git+"):
        return ""
    return url[len("git+") :].rstrip("/")


def list_frames(node: FenceNode, *, runner: Callable[..., Any] | None = None) -> list[str]:
    """The frame ids with a published ``refs/dolt/frame/<id>`` on the hive's Git remote.

    Read-only (``git ls-remote``). A hive whose remote is not a Git remote has no frame refs.
    Raises :class:`DataUnreachable` when the remote cannot be listed."""
    rows = node.query("SELECT name, url FROM dolt_remotes")
    origin = next((r for r in rows if str(r.get("name")) == node.remote), None)
    url = _git_url(str((origin or {}).get("url") or ""))
    if not url:
        return []
    if runner is None:
        runner = importlib.import_module("beadhive.run").run
    env = getattr(node.engine, "env", None)
    try:
        res = runner(
            ["git", "ls-remote", url, f"{_FRAME_REF_PREFIX}*"],
            check=False,
            capture=True,
            env=env,
            timeout=_LS_REMOTE_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise DataUnreachable(f"git ls-remote {url} timed out") from exc
    if res.returncode != 0:
        raise DataUnreachable(f"git ls-remote {url}: {(res.stderr or res.stdout or '')[:300]}")
    frames = []
    for line in (res.stdout or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].startswith(_FRAME_REF_PREFIX):
            frame = parts[1][len(_FRAME_REF_PREFIX) :]
            if _FRAME_ID.fullmatch(frame):
                frames.append(frame)
    return sorted(set(frames))


def _fetch_frame(node: FenceNode, frame: str) -> str:
    """Configure the ``frame-<id>`` remote on the writer's store and fetch it; returns the
    remote-tracking revision of the frame's ``main``."""
    publish = importlib.import_module("beadhive.write_policy_publish")
    try:
        name = publish.ensure_remote(node.engine, frame)
    except publish.PublishError as exc:
        raise DataUnreachable(str(exc)) from exc
    fetcher = copy.copy(node.engine)
    fetcher.remote = name
    fetcher.fetch()
    return f"{name}/{node.branch}"


def _columns(row: Mapping) -> list[str]:
    names: dict[str, None] = {}
    for key in row:
        if key in _PREVIEW_META:
            continue
        for side in ("base_", "our_", "their_"):
            if key.startswith(side):
                names.setdefault(key[len(side) :], None)
    return list(names)


def _bead_column(table: str, columns: Sequence[str]) -> str:
    if table in ("issues", "wisps"):
        return "id"
    for column in ("issue_id", "parent_id"):
        if column in columns:
            return column
    return ""


def _primary_key(node: FenceNode, table: str) -> list[str]:
    rows = node.query(
        "SELECT column_name AS c FROM information_schema.key_column_usage WHERE "
        f"table_schema = database() AND table_name = {quote(table)} AND "
        "constraint_name = 'PRIMARY' ORDER BY ordinal_position"
    )
    return [str(r.get("c") or r.get("COLUMN_NAME") or "") for r in rows]


def _side_value(row: Mapping, column: str) -> Any:
    for side in ("our_", "their_", "base_"):
        value = row.get(f"{side}{column}")
        if value is not None:
            return value
    return None


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, dict)):
        return json.loads(json.dumps(value, default=str))
    return str(value)


def _conflict_row(node: FenceNode, table: str, row: Mapping, pk_cache: dict) -> ConflictRow:
    columns = _columns(row)
    bead_column = _bead_column(table, columns)
    bead = str(_side_value(row, bead_column) or "") if bead_column else ""
    if bead:
        key = bead
    else:
        if table not in pk_cache:
            pk_cache[table] = _primary_key(node, table) or columns[:1]
        key = "/".join(str(_side_value(row, c)) for c in pk_cache[table])
    differ = [
        c
        for c in columns
        if row.get(f"our_{c}") != row.get(f"their_{c}") and c not in _NOISE_COLUMNS
    ] or [c for c in columns if row.get(f"our_{c}") != row.get(f"their_{c}")]
    collision = (
        bead_column == "id"
        and row.get("our_diff_type") == "added"
        and row.get("their_diff_type") == "added"
    )
    if collision and "title" in columns and "title" not in differ:
        differ.insert(0, "title")  # name both beads, even when only other fields differ
    return ConflictRow(
        table=table,
        bead=bead,
        key=key,
        main={c: _jsonable(row.get(f"our_{c}")) for c in differ},
        frame={c: _jsonable(row.get(f"their_{c}")) for c in differ},
        main_change=str(row.get("our_diff_type") or ""),
        frame_change=str(row.get("their_diff_type") or ""),
        collision=collision,
    )


def _is_clone_local(row: ConflictRow) -> bool:
    return (row.table, row.key) in CLONE_LOCAL


def preview(node: FenceNode, rev: str) -> list[ConflictRow]:
    """Every row merging `rev` into ``main`` would conflict on (schema conflicts as one row
    per table). Read-only."""
    summary = node.query(
        "SELECT `table` AS t, num_data_conflicts AS d, num_schema_conflicts AS s FROM "
        f"DOLT_PREVIEW_MERGE_CONFLICTS_SUMMARY({quote(node.branch)}, {quote(rev)})"
    )
    rows: list[ConflictRow] = []
    pk_cache: dict[str, list[str]] = {}
    for item in summary:
        table = str(item.get("t") or "")
        if int(item.get("s") or 0):
            rows.append(ConflictRow(table, "", "<schema>", {}, {}, schema=True))
        if not int(item.get("d") or 0):
            continue
        if not _TABLE.fullmatch(table):
            rows.append(ConflictRow(table, "", "<unreadable>", {}, {}))
            continue
        for row in node.query(
            "SELECT * FROM DOLT_PREVIEW_MERGE_CONFLICTS("
            f"{quote(node.branch)}, {quote(rev)}, {quote(table)})"
        ):
            rows.append(_conflict_row(node, table, row, pk_cache))
    return rows


def double_claims(rows: Iterable[ConflictRow], frame: str) -> tuple[DoubleClaim, ...]:
    """The conflicting ``issues`` rows where ``main`` and the frame each hold a different,
    non-empty assignee: two frames claimed the bead."""
    out = []
    for row in rows:
        if row.table != "issues" or row.collision or "assignee" not in row.main:
            continue
        ours, theirs = str(row.main.get("assignee") or ""), str(row.frame.get("assignee") or "")
        if ours and theirs and ours != theirs:
            out.append(DoubleClaim(row.bead, ours, theirs, frame))
    return tuple(out)


# =============================================================================================
# The merge
# =============================================================================================


def merge_message(frame: str, epoch: int, resolutions: Mapping[str, str] | None = None) -> str:
    """The merge commit's subject; ``bh: merge frame/`` is the fence audit's sanctioned prefix."""
    message = (
        f"bh: merge frame/{frame} from {_FRAME_REF_PREFIX}{frame}, re-stamp its marks at "
        f"epoch {int(epoch)}"
    )
    if resolutions:
        sides = ", ".join(f"{s}={side}" for s, side in sorted(resolutions.items()))
        message += f"; resolved {sides}"
    return message


def _key_filter(row: ConflictRow, pk: Sequence[str]) -> str:
    """A ``dolt_conflicts_<table>`` WHERE clause selecting a non-bead row by its primary key."""
    values = row.key.split("/")
    if not pk or len(values) != len(pk):
        raise FrameMergeRefused(f"cannot address {row.subject}: its key does not match {pk}")
    return " AND ".join(
        f"COALESCE(our_{c}, their_{c}, base_{c}) = {quote(v)}"
        for c, v in zip(pk, values, strict=True)
    )


def _bead_column_for(node: FenceNode, table: str) -> str:
    rows = node.query(
        "SELECT column_name AS c FROM information_schema.columns WHERE "
        f"table_schema = database() AND table_name = {quote(table)}"
    )
    return _bead_column(table, [str(r.get("c") or r.get("COLUMN_NAME") or "") for r in rows])


def _resolution_statements(
    node: FenceNode, rows: Sequence[ConflictRow], resolutions: Mapping[str, str]
) -> list[str]:
    """Statements that resolve every conflict in `rows` by the operator's sides, in the open
    merge's session. ``main``: drop the subject's conflict rows (ours stays). ``frame``: after
    every ``main`` subject of the table is dropped, ``DOLT_CONFLICTS_RESOLVE('--theirs')`` takes
    the rest — no DML on a guarded table (its trigger would close the merge)."""
    statements: list[str] = []
    by_table: dict[str, list[ConflictRow]] = {}
    for row in rows:
        by_table.setdefault(row.table, []).append(row)
    for table, table_rows in sorted(by_table.items()):
        if not _TABLE.fullmatch(table):
            raise FrameMergeRefused(f"cannot resolve a conflict on table {table!r}")
        pk = _primary_key(node, table)
        bead_column = _bead_column_for(node, table)
        theirs = False
        for row in table_rows:
            if row.collision:
                raise FrameMergeRefused(_collision_refusal(row, ""))
            side = MAIN if _is_clone_local(row) else resolutions.get(row.subject)
            if side not in SIDES:
                raise FrameMergeRefused(f"{row.subject} has no side taken")
            if side == FRAME:
                theirs = True
                continue
            if row.bead and bead_column:
                where = f"COALESCE(our_{bead_column}, their_{bead_column}, base_{bead_column}) = "
                where += quote(row.bead)
            else:
                where = _key_filter(row, pk)
            statements.append(f"DELETE FROM dolt_conflicts_{table} WHERE {where}")
        if theirs:
            statements.append(f"CALL DOLT_CONFLICTS_RESOLVE('--theirs', {quote(table)})")
    return statements


def _clone_local_statements() -> list[str]:
    """Keep ``main``'s clone-local rows even when the frame changed them without a conflict.
    ``metadata`` is unguarded (``fence_schema.UNGUARDED_BD_TABLES``), so this DML fires no
    trigger and leaves the merge open."""
    out = []
    for table, key in CLONE_LOCAL:
        out.append(
            f"UPDATE {table} SET value = (SELECT value FROM {table} AS OF 'HEAD' WHERE "
            f"`key` = {quote(key)}) WHERE `key` = {quote(key)} AND @bh_merging > 0 AND EXISTS "
            f"(SELECT 1 FROM {table} AS OF 'HEAD' WHERE `key` = {quote(key)})"
        )
    return out


def _postflight(node: FenceNode, prev: str, head: str, message: str, epoch: int) -> list[str]:
    problems = []
    now = _hashof(node, node.branch)
    h = quote(now)
    rows = node.query(
        f"SELECT parents AS p, message AS m FROM dolt_log('--parents', {h}) WHERE commit_hash = {h}"
    )
    row = rows[0] if rows else {}
    parents = tuple(sorted(p.strip() for p in str(row.get("p") or "").split(",") if p.strip()))
    if parents != tuple(sorted((prev, head))):
        problems.append(f"HEAD {now[:12]} is not the merge of {prev[:12]} and {head[:12]}")
    if str(row.get("m") or "") != message:
        problems.append(f"HEAD subject is {str(row.get('m') or '')[:80]!r}, not the merge's")
    if node.query("SELECT `table` FROM dolt_constraint_violations"):
        problems.append("constraint violations remain in the working set")
    if node.query("SELECT `table` FROM dolt_conflicts"):
        problems.append("conflicts remain in the working set")
    stale = node.query(f"SELECT COUNT(*) AS n FROM bh_write_mark WHERE epoch <> {int(epoch)}")
    if int(_scalar(stale) or 0):
        problems.append(f"{_scalar(stale)} marks are not at the live epoch {epoch}")
    return problems


def _restore(node: FenceNode, prev: str) -> str:
    try:
        node.engine.reset_hard(prev)
        after = _hashof(node, node.branch)
    except (SqlFailed, DataUnreachable) as exc:
        return f"MAIN NOT RESTORED to {prev} ({exc}) — do not push; reset it by hand"
    if after != prev:
        return f"MAIN NOT RESTORED: it is {after}, not {prev} — do not push; reset it by hand"
    return f"local main was restored to {prev[:12]}"


def _mark_count(node: FenceNode, rev: str) -> int:
    rows = node.query(f"SELECT COUNT(*) AS n FROM bh_write_mark AS OF {quote(rev)}")
    return int(_scalar(rows) or 0)


def _require_writer(node: FenceNode, writer_frame: str) -> int:
    node.require_writer_ready()
    local = node.writer()
    if local is None or not writer_frame or local.frame != writer_frame:
        held = f"{local.frame}@{local.epoch}" if local else "nobody (not cut over)"
        raise FrameMergeRefused(
            f"only the hive's writer merges frame refs: local bh_writer names {held}, this "
            f"frame is {writer_frame or '?'}"
        )
    live = int(_scalar(node.query("SELECT epoch FROM bh_epoch_live WHERE id = 1")) or 0)
    if live != local.epoch:
        raise FrameMergeRefused(
            f"live epoch {live} does not match bh_writer epoch {local.epoch}; re-run the adopt"
        )
    return live


def merge_frame(
    node: FenceNode,
    frame: str,
    *,
    hive: str,
    writer_frame: str,
    resolutions: Mapping[str, str] | None = None,
    now: Callable[[], float] = time.time,
) -> FrameMergeOutcome:
    """Merge ``refs/dolt/frame/<frame>`` into the writer's local ``main``; never raises for a
    conflict or a failed merge — both are returned (and recorded by the caller).

    With no `resolutions` any conflict holds the ref unmerged. With `resolutions` (subject ->
    ``main`` | ``frame``) covering every conflicting subject, the merge completes taking those
    sides. Raises :class:`FrameMergeRefused` when this node is not the writer."""
    _frame_id(frame)
    resolutions = dict(resolutions or {})
    at = now()
    base = FrameMergeOutcome(hive, frame, Status.FAILED, at, resolutions=resolutions)
    live = _require_writer(node, writer_frame)
    try:
        rev = _fetch_frame(node, frame)
        head = _hashof(node, rev)
        node.engine.commit("bh: commit working set before frame merge")
        prev = _hashof(node, node.branch)
    except (SqlFailed, DataUnreachable) as exc:
        return replace(base, error=f"cannot read {_FRAME_REF_PREFIX}{frame}: {exc}")
    base = replace(base, frame_head=head, main_before=prev)
    if _contains(node, head, node.branch):
        return replace(base, status=Status.UP_TO_DATE, resolutions={})
    try:
        rows = preview(node, rev)
    except SqlFailed as exc:
        return replace(base, error=f"cannot preview the merge: {exc}")
    kept = tuple(f"{row.table}.{row.key}" for row in rows if _is_clone_local(row))
    conflicts = tuple(row for row in rows if not _is_clone_local(row))
    claims = double_claims(conflicts, frame)
    subjects = {row.subject for row in conflicts}
    collided = {row.subject for row in conflicts if row.collision}
    resolutions = {
        s: side for s, side in resolutions.items() if s in subjects and s not in collided
    }
    held = replace(
        base,
        status=Status.CONFLICT,
        conflicts=conflicts,
        double_claims=claims,
        resolutions=resolutions,
    )
    if any(row.schema for row in conflicts):
        return replace(held, error="a schema conflict is resolved by hand only")
    if collided:
        return replace(held, error="; ".join(r.describe_collision() for r in held.collisions))
    if subjects - set(resolutions):
        return held
    message = merge_message(frame, live, resolutions)
    try:
        resolve_statements = _resolution_statements(node, rows, resolutions)
        marks_before = _mark_count(node, prev)
    except (FrameMergeRefused, SqlFailed) as exc:
        return replace(held, error=str(exc))
    # bd's server batch does not stop at a failing statement, so everything after the merge is
    # guarded on the merge actually being open: a failed DOLT_MERGE commits nothing (a NULL
    # message aborts DOLT_COMMIT), and DOLT_COMMIT refuses while any conflict remains.
    statements = [
        "SET @@autocommit = 0",
        "SET @@dolt_force_transaction_commit = 1",
        f"CALL DOLT_MERGE('--no-ff', '--no-commit', {quote(rev)})",
        "SET @bh_merging = (SELECT COUNT(*) FROM dolt_merge_status WHERE is_merging)",
        *_clone_local_statements(),
        *resolve_statements,
        f"UPDATE bh_write_mark SET epoch = {live} WHERE epoch <> {live} AND @bh_merging > 0",
        "DELETE FROM dolt_constraint_violations_bh_write_mark WHERE @bh_merging > 0",
        f"CALL DOLT_COMMIT('-Am', IF(@bh_merging > 0, {quote(message)}, NULL))",
    ]
    error = ""
    try:
        node.engine.execute_session(statements)
    except SqlFailed as exc:
        error = str(exc)
    try:
        problems = _postflight(node, prev, head, message, live)
    except (SqlFailed, DataUnreachable) as exc:
        problems = [f"postflight unreadable: {exc}"]
    if problems:
        problems.append(_restore(node, prev))
        detail = "; ".join(problems) + (f" (session: {error})" if error else "")
        failed = held if conflicts else replace(base, kept_main=kept)
        return replace(failed, status=Status.FAILED, error=f"the merge did not land: {detail}")
    commit = _hashof(node, node.branch)
    restamped = max(_mark_count(node, commit) - marks_before, 0)
    log.get_logger(__name__).info(
        "frame_ref_merged", hive=hive, frame=frame, commit=commit, resolved=len(resolutions)
    )
    return replace(
        base,
        status=Status.MERGED,
        commit=commit,
        resolutions=resolutions,
        kept_main=kept,
        restamped=restamped,
    )


def _collision_refusal(row: ConflictRow, frame: str) -> str:
    where = f"frame {frame}" if frame else "the frame"
    return (
        f"{row.describe_collision()}. No side is taken for an id collision. On {where}, "
        f"re-create its bead {row.frame.get('title')!r} under a new id, remove {row.bead} from "
        "that frame's main and publish again; the next merge then sees no collision."
    )


# =============================================================================================
# The hive: every frame ref, isolated
# =============================================================================================


@dataclass(frozen=True)
class WriterContext:
    """The writer's node for one hive."""

    hive: str
    node: FenceNode
    writer_frame: str


def writer_context(hive_dir: Path, *, cfg=None) -> WriterContext:
    """The writer's :class:`FenceNode` for the hive at `hive_dir`. Raises
    :class:`FrameMergeRefused` when this host is not the hive's writer or the hive is not cut
    over (frame refs exist only on a cut-over hive)."""
    fence_data = importlib.import_module("beadhive.fence_data")
    write_policy = importlib.import_module("beadhive.write_policy")
    guard = importlib.import_module("beadhive.guard")
    node = fence_data.node_for(Path(hive_dir))
    if node is None:
        raise FrameMergeRefused(
            f"{hive_dir}: the hive is not cut over to the in-data fence; frame refs are merged "
            "only on a cut-over hive"
        )
    try:
        state = guard.writer_state("", cfg=cfg, hive_dir=Path(hive_dir))
    except guard.WriterUnreadable as exc:
        raise FrameMergeRefused(f"{hive_dir}: the writer fence is unreadable: {exc}") from exc
    if state is None:
        raise FrameMergeRefused(f"{hive_dir}: no writer is recorded for this hive")
    prefix, this_frame, writer = state
    where = write_policy.off_primary(cfg=cfg, hive_dir=Path(hive_dir))
    if where is not None or not this_frame or writer.frame != this_frame:
        raise FrameMergeRefused(
            f"{prefix}: only the hive's writer ({writer.frame or '?'}) merges frame refs; this "
            f"frame is {this_frame or '?'}"
        )
    return WriterContext(str(prefix), node, this_frame)


def merge_hive(
    ctx: WriterContext,
    *,
    frames: Sequence[str] | None = None,
    now: Callable[[], float] = time.time,
    path: Path | None = None,
    lister: Callable[[FenceNode], list[str]] | None = None,
) -> list[FrameMergeOutcome]:
    """Merge every published frame ref (or `frames`) of the hive, each on its own, and record
    each outcome. A conflicting or failing ref never stops the next one; recorded operator sides
    are re-applied so a merge resolved earlier completes. Raises :class:`DataUnreachable` only
    when the frame refs cannot be listed."""
    names = list(frames) if frames is not None else (lister or list_frames)(ctx.node)
    outcomes = []
    for frame in names:
        if frame == ctx.writer_frame:
            continue
        prior = recorded(ctx.hive, frame, path=path)
        sides = dict(prior.resolutions) if prior is not None else {}
        try:
            outcome = merge_frame(
                ctx.node,
                frame,
                hive=ctx.hive,
                writer_frame=ctx.writer_frame,
                resolutions=sides,
                now=now,
            )
        except Exception as exc:  # noqa: BLE001 - one ref's failure never stops the others
            outcome = FrameMergeOutcome(ctx.hive, frame, Status.FAILED, now(), error=str(exc))
        record(outcome, path=path)
        outcomes.append(outcome)
    return outcomes


def after_pull(
    hive_dir: Path,
    *,
    cfg=None,
    now: Callable[[], float] = time.time,
    path: Path | None = None,
) -> list[FrameMergeOutcome]:
    """The ``hive.sync`` loop's seam (``bh-midl0``): after auto-pull on the writer, merge every
    frame ref. Never raises and never blocks: on a host that is not the writer, a hive that is
    not cut over, or an unlistable remote it returns ``[]`` (logged)."""
    try:
        ctx = writer_context(Path(hive_dir), cfg=cfg)
    except Exception as exc:  # noqa: BLE001 - not the writer here: nothing to merge
        log.get_logger(__name__).debug("frame_merge_skipped", hive_dir=str(hive_dir), why=str(exc))
        return []
    try:
        return merge_hive(ctx, now=now, path=path)
    except Exception as exc:  # noqa: BLE001 - the sync loop never fails on a frame merge
        log.get_logger(__name__).warning("frame_merge_unlisted", hive=ctx.hive, error=str(exc))
        return []


def resolve(
    ctx: WriterContext,
    frame: str,
    subject: str,
    take: str | None,
    *,
    now: Callable[[], float] = time.time,
    path: Path | None = None,
) -> FrameMergeOutcome:
    """Take ``main``'s side or the frame's side for one recorded conflicting bead (or
    ``<table>:<key>`` row) of ``refs/dolt/frame/<frame>``; once every recorded subject has a
    side, complete the merge. Refuses (:class:`FrameMergeRefused`, nothing written) without an
    explicit side, or for a subject with no recorded conflict."""
    _frame_id(frame)
    if take not in SIDES:
        raise FrameMergeRefused(
            f"resolving {subject or '<bead>'} needs an explicit side: --take main keeps main's "
            f"rows, --take frame takes {_FRAME_REF_PREFIX}{frame}'s. Nothing is ever resolved "
            "by default."
        )
    prior = recorded(ctx.hive, frame, path=path)
    if prior is None or prior.status != Status.CONFLICT or subject not in prior.subjects:
        known = ", ".join(prior.subjects) if prior is not None and prior.subjects else "none"
        raise FrameMergeRefused(
            f"{ctx.hive}: no recorded conflict on {subject} for {_FRAME_REF_PREFIX}{frame} "
            f"(recorded: {known}); run `bh hive frames merge {ctx.hive}` first"
        )
    collision = next((r for r in prior.collisions if r.subject == subject), None)
    if collision is not None:
        raise FrameMergeRefused(_collision_refusal(collision, frame))
    sides = {**prior.resolutions, subject: take}
    pending = replace(prior, resolutions=sides)
    if pending.unresolved:
        record(pending, path=path)
        return pending
    outcome = merge_frame(
        ctx.node, frame, hive=ctx.hive, writer_frame=ctx.writer_frame, resolutions=sides, now=now
    )
    record(outcome, path=path)
    return outcome


# =============================================================================================
# bh doctor / bh hq why
# =============================================================================================


def doctor_data(*, path: Path | None = None) -> dict:
    """The recorded outcome per hive and frame; ``warn`` while any ref is held."""
    hives = {}
    held = 0
    for hive, frames in sorted(load(path=path).items()):
        items = [frames[f].as_dict() for f in sorted(frames)]
        held += sum(1 for f in frames.values() if f.held)
        hives[hive] = items
    return {"level": "warn" if held else "ok", "held": held, "hives": hives}


def doctor_lines(data: Mapping, *, bh: str = "bh") -> list[str]:
    lines = []
    for hive, items in sorted((data.get("hives") or {}).items()):
        for item in items:
            outcome = FrameMergeOutcome.from_dict(item)
            glyph = "⚠" if outcome.held else "✓"
            lines.append(f"  {glyph} {hive}: {outcome.describe()}")
            for row in outcome.conflicts:
                tag = " [id collision: no side may be taken]" if row.collision else ""
                lines.append(
                    f"      {row.table} {row.subject}: main {_values(row.main)} | frame "
                    f"{_values(row.frame)}{tag}"
                )
            if outcome.status == Status.CONFLICT:
                lines.append(
                    f"      resolve: {bh} hive frames resolve {hive} --frame {outcome.frame} "
                    "--bead <bead> --take main|frame"
                )
    return lines


def _values(values: Mapping) -> str:
    text = ", ".join(f"{k}={v!r}" for k, v in values.items())
    return f"{{{text[:160]}}}"


def why_summary(hive: str, *, path: Path | None = None) -> tuple[str, list[dict]]:
    """``(text, held outcomes)`` for ``bh hq why``: the frame refs of `hive` held by a conflict
    or a failed merge, per frame. ``("", [])`` when none is held."""
    held = [o for _, o in sorted(load(path=path).get(hive, {}).items()) if o.held]
    if not held:
        return "", []
    text = "; ".join(o.describe() for o in held)
    return text, [o.as_dict() for o in held]
