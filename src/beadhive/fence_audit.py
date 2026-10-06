"""``fence_audit`` — after-the-fact detection on a cut-over hive (bh-uz46l, P-M1).

ADR ``docs/design/hive-writer-partitioning-adr.md`` §2 "Detection", condition 4, and the M13
amendment (``bh-uhx2r``, operator 2026-10-06). It judges the hive's REMOTE ``main`` (never
``refs/dolt/data``: a refused push still moves that ref, ``bh-eybn7`` E6) plus HQ placement, and
reports, each with evidence:

* ``stale_marks`` — ``bh_write_mark`` rows whose epoch is not the live one: a write stamped by a
  retired epoch was committed past the foreign key (``dolt commit --force``, a forced merge);
* ``epoch_regressed`` — ``bh_writer.epoch`` below an epoch its own history already reached (a
  ``--strategy`` merge resolved ``bh_writer`` to a stale side);
* ``placement_ahead`` — HQ placement names a higher epoch than the data (adopt incomplete, or a
  force push wiped the bump);
* ``late_writes`` — the I3-style HISTORY check. It walks ``main``'s commits since the live
  epoch's adopt (the commits reachable from the head but not from that adopt) and reports every
  non-merge commit stamped with an epoch lower than the live one. Such a commit was written
  before the bump yet entered ``main`` after it: the ``bh-uhx2r`` E2 case, where a forwarder
  with excess grants force-merges the bump into a stale ``main``, RE-STAMPS its marks and
  deletes the violation rows, leaving the three current-state checks clean. Commits that entered
  through a deliberate ``bh: merge frame/…`` orphan merge are sanctioned (the writer re-stamps
  them on purpose, ``bh-vje85`` E3).

The history check is a second line of defence. The table-scoped forwarder grants (M12,
``bh-g7dlo``) are the only defence against a ``bh_local_ident`` rewrite (``bh-uhx2r`` E3): a
non-writer writing AT the live epoch is undetectable from data.

Pure read: it fetches (moving only remote-tracking refs) and reads ``AS OF`` the fetched head;
it never writes the working set, ``main`` or any table. Authorship-independent: bd commits as
``beads``/``root`` (``bh-jbb6r`` E4), so nothing here reads a commit's author or committer.
Fence reads bind values the audit already read (Python-side variables), never subqueries
(``bh-vje85`` E3).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from .fence_schema import quote

__all__ = [
    "DEFAULT_EVIDENCE_LIMIT",
    "DEFAULT_REF",
    "MERGE_PREFIX",
    "AuditReader",
    "Commit",
    "FenceAudit",
    "LateWrite",
    "StaleMark",
    "fence_audit",
    "find_late_writes",
    "history_window",
]

#: The audited ref: remote ``main`` as of the audit's own fetch.
DEFAULT_REF = "origin/main"
#: Subject of the writer's deliberate orphan / branch merge (``bh-vje85`` E3, ``bh-sieai`` R2).
MERGE_PREFIX = "bh: merge frame/"
#: Rows of evidence kept per finding (the counts are always exact).
DEFAULT_EVIDENCE_LIMIT = 20
_IN_CHUNK = 200
_HASH = re.compile(r"[0-9a-v]{32}")


class AuditReader(Protocol):
    """A read-only SQL view of one node's copy of the hive (:class:`beadhive.fence_data.FenceNode`
    implements it)."""

    def query(self, sql: str) -> list[dict]:
        """Rows of one read-only statement."""

    def fetch(self) -> None:
        """Fetch the hive remote (moves only remote-tracking refs)."""


class _Placement(Protocol):
    frame: str
    epoch: int


@dataclass(frozen=True)
class Commit:
    """One commit of the walked window."""

    hash: str
    parents: tuple[str, ...] = ()
    message: str = ""

    @property
    def is_merge(self) -> bool:
        return len(self.parents) > 1


@dataclass(frozen=True)
class StaleMark:
    id: str
    epoch: int
    tbl: str = ""


@dataclass(frozen=True)
class LateWrite:
    """A commit stamped below an adopt it is not an ancestor of (I3)."""

    commit: str
    #: ``bh_writer.epoch`` the commit itself carries (``None``: it predates the cutover).
    epoch: int | None
    adopt_commit: str
    adopt_epoch: int
    message: str = ""
    #: The merge commit that brought it into ``main``, when there is one in the window.
    merged_by: str = ""

    def describe(self) -> str:
        stamped = "unfenced" if self.epoch is None else f"epoch {self.epoch}"
        via = f", merged by {self.merged_by[:12]}" if self.merged_by else ""
        return (
            f"{self.commit[:12]} ({stamped}) entered main after the epoch-{self.adopt_epoch} "
            f"adopt {self.adopt_commit[:12]} without being its ancestor{via}: "
            f"{self.message.splitlines()[0][:60] if self.message else ''}"
        )


@dataclass(frozen=True)
class FenceAudit:
    """One audit of ``ref`` (``head``) against HQ placement."""

    ref: str
    head: str
    cut_over: bool
    writer_frame: str = ""
    writer_epoch: int = 0
    live_epoch: int = 0
    stale_mark_count: int = 0
    stale_marks: tuple[StaleMark, ...] = ()
    history_max_epoch: int = 0
    history_max_commit: str = ""
    placement_frame: str | None = None
    placement_epoch: int | None = None
    adopt_commits: tuple[str, ...] = ()
    history_window: int = 0
    late_writes: tuple[LateWrite, ...] = ()
    notes: tuple[str, ...] = field(default=())

    @property
    def epoch_regressed(self) -> bool:
        return self.cut_over and self.history_max_epoch > self.writer_epoch

    @property
    def placement_ahead(self) -> bool:
        return (
            self.cut_over
            and self.placement_epoch is not None
            and self.placement_epoch > self.writer_epoch
        )

    @property
    def history_unanchored(self) -> bool:
        """No non-merge adopt commit introduces the live epoch: the history walk had no anchor."""
        return self.cut_over and not self.adopt_commits

    @property
    def ok(self) -> bool:
        return not self.findings()

    def findings(self) -> list[str]:
        """One line per finding, with evidence; empty when the fence is clean."""
        if not self.cut_over:
            return []
        out: list[str] = []
        if self.stale_mark_count:
            shown = ", ".join(f"{m.id}@{m.epoch}({m.tbl})" for m in self.stale_marks)
            out.append(
                f"stale_marks: {self.stale_mark_count} bh_write_mark row(s) not at the live "
                f"epoch {self.live_epoch}: {shown}"
            )
        if self.epoch_regressed:
            out.append(
                f"epoch_regressed: bh_writer is at {self.writer_epoch} but history reached "
                f"{self.history_max_epoch} (commit {self.history_max_commit[:12]})"
            )
        if self.placement_ahead:
            out.append(
                f"placement_ahead: placement names {self.placement_frame}@"
                f"{self.placement_epoch} but bh_writer names {self.writer_frame}@"
                f"{self.writer_epoch} (adopt incomplete)"
            )
        if self.history_unanchored:
            out.append(
                f"history_unanchored: no adopt commit introduces the live epoch "
                f"{self.live_epoch}; the late-write history check could not run"
            )
        for late in self.late_writes:
            out.append(f"late_write: {late.describe()}")
        return out

    def as_dict(self) -> dict:
        return {
            "ref": self.ref,
            "head": self.head,
            "cut_over": self.cut_over,
            "writer": {"frame": self.writer_frame, "epoch": self.writer_epoch},
            "live_epoch": self.live_epoch,
            "stale_marks": self.stale_mark_count,
            "stale_mark_evidence": [m.__dict__ for m in self.stale_marks],
            "history_max_epoch": self.history_max_epoch,
            "history_max_commit": self.history_max_commit,
            "epoch_regressed": self.epoch_regressed,
            "placement": (
                None
                if self.placement_epoch is None
                else {"frame": self.placement_frame, "epoch": self.placement_epoch}
            ),
            "placement_ahead": self.placement_ahead,
            "adopt_commits": list(self.adopt_commits),
            "history_window": self.history_window,
            "late_writes": [w.__dict__ for w in self.late_writes],
            "ok": self.ok,
            "findings": self.findings(),
        }


# =============================================================================================
# The history check (pure)
# =============================================================================================


def _ancestors_within(start: str, window: Mapping[str, Commit]) -> set[str]:
    """``start`` and its ancestors that lie inside ``window``. Exact for set arithmetic on the
    window: anything reachable from a commit outside it is an ancestor of the anchor, so it is
    outside the window too."""
    seen: set[str] = set()
    stack = [start]
    while stack:
        c = stack.pop()
        if c in seen or c not in window:
            continue
        seen.add(c)
        stack.extend(window[c].parents)
    return seen


def find_late_writes(
    window: Mapping[str, Commit],
    epochs: Mapping[str, int | None],
    anchors: Mapping[str, int],
) -> list[LateWrite]:
    """I3 on a window of ``main``: every non-merge, non-bump commit in ``window`` stamped at an
    epoch below some adopt (an ``anchor`` below the window, or a bump inside it) that it is not
    an ancestor of — unless it entered through a ``bh: merge frame/…`` merge.

    ``window`` is the commits reachable from the audited head but not from any anchor;
    ``epochs`` maps every window commit (and its parents) to its ``bh_writer.epoch`` (``None``
    when the commit has no ``bh_writer``); ``anchors`` maps each adopt commit that starts the
    window to its epoch. A commit is a bump when it is not a merge and carries a higher epoch
    than its first parent."""

    def epoch(c: str) -> int | None:
        return epochs.get(c)

    def is_bump(c: Commit) -> bool:
        if c.is_merge or epoch(c.hash) is None:
            return False
        first = c.parents[0] if c.parents else None
        before = epoch(first) if first else None
        return before is None or before < (epoch(c.hash) or 0)

    merged_by: dict[str, str] = {}
    sanctioned: set[str] = set()
    for c in window.values():
        if not c.is_merge:
            continue
        below_first = _ancestors_within(c.parents[0], window)
        side: set[str] = set()
        for other in c.parents[1:]:
            side |= _ancestors_within(other, window) - below_first
        for s in side:
            merged_by.setdefault(s, c.hash)
        if c.message.startswith(MERGE_PREFIX):
            sanctioned |= side

    bumps: dict[str, int] = dict(anchors)
    below: dict[str, set[str]] = {a: set() for a in anchors}  # nothing in the window is below
    for c in window.values():
        if is_bump(c):
            bumps[c.hash] = int(epoch(c.hash) or 0)
            below[c.hash] = _ancestors_within(c.hash, window)

    late: list[LateWrite] = []
    for c in sorted(window.values(), key=lambda x: x.hash):
        if c.is_merge or c.hash in bumps or c.hash in sanctioned:
            continue
        stamped = epoch(c.hash)
        for bump, bump_epoch in sorted(bumps.items(), key=lambda kv: (kv[1], kv[0])):
            if bump_epoch > (stamped or 0) and c.hash not in below[bump]:
                late.append(
                    LateWrite(
                        commit=c.hash,
                        epoch=stamped,
                        adopt_commit=bump,
                        adopt_epoch=bump_epoch,
                        message=c.message,
                        merged_by=merged_by.get(c.hash, ""),
                    )
                )
                break
    return late


# =============================================================================================
# The audit
# =============================================================================================


def _hash(value: object, what: str) -> str:
    text = str(value or "")
    if not _HASH.fullmatch(text):
        raise ValueError(f"fence_audit: {what} is not a Dolt commit hash: {text!r}")
    return text


def _parents(raw: object) -> tuple[str, ...]:
    return tuple(p.strip() for p in str(raw or "").split(",") if p.strip())


def _chunks(items: Sequence[str], size: int = _IN_CHUNK) -> Iterable[Sequence[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _scalar(rows: list[dict]) -> object:
    return next(iter(rows[0].values())) if rows and rows[0] else None


def history_window(
    fenced: Mapping[str, tuple[int, tuple[str, ...]]], head: str, base: int
) -> tuple[dict[str, Commit], dict[str, int], dict[str, str]]:
    """Split ``ref``'s fenced history (``{commit: (epoch, parents)}``) for the I3 walk.

    Returns ``(window, anchors, unfenced)``:

    * ``anchors`` — the epoch-``base`` adopts: non-merge commits at ``base`` whose first parent
      is below it (or carries no ``bh_writer``: the cutover itself);
    * ``window`` — the fenced commits reachable from ``head`` that are NOT ancestors of an
      anchor ("main's commits since the adopt");
    * ``unfenced`` — ``{parent: merge}`` for every window merge whose non-first parent carries
      no ``bh_writer``: pre-cutover history merged in after the adopt.

    Ancestry is computed inside ``fenced`` only; an unfenced commit can be an ancestor of an
    anchor only below the cutover, which no window commit reaches except through a merge."""

    def first_parent_epoch(parents: tuple[str, ...]) -> int | None:
        return fenced[parents[0]][0] if parents and parents[0] in fenced else None

    anchors = {
        c: base
        for c, (epoch, parents) in fenced.items()
        if epoch == base
        and len(parents) <= 1
        and (first_parent_epoch(parents) is None or (first_parent_epoch(parents) or 0) < base)
    }
    graph = {c: Commit(c, parents) for c, (_, parents) in fenced.items()}
    below: set[str] = set()
    for anchor in anchors:
        below |= _ancestors_within(anchor, graph)
    window = {
        c: graph[c] for c in _ancestors_within(head, graph) if c not in below and c not in anchors
    }
    settled = {p for c in below | set(anchors) for p in graph[c].parents}  # cutover's parents
    unfenced = {
        parent: c.hash
        for c in window.values()
        if c.is_merge
        for parent in c.parents[1:]
        if parent not in fenced and parent not in settled
    }
    return window, anchors, unfenced


def _messages(reader: AuditReader, head: str, commits: Sequence[str]) -> dict[str, str]:
    """Commit subjects for a few commits (evidence and the orphan-merge sanction only)."""
    out: dict[str, str] = {}
    for chunk in _chunks(sorted(set(commits))):
        listed = ", ".join(quote(_hash(c, "commit")) for c in chunk)
        for row in reader.query(
            f"SELECT commit_hash, message FROM dolt_log({quote(head)}) "
            f"WHERE commit_hash IN ({listed})"
        ):
            out[str(row["commit_hash"])] = str(row.get("message") or "")
    return out


def fence_audit(
    reader: AuditReader,
    *,
    placement: _Placement | None = None,
    ref: str = DEFAULT_REF,
    fetch: bool = True,
    since_epoch: int | None = None,
    evidence_limit: int = DEFAULT_EVIDENCE_LIMIT,
) -> FenceAudit:
    """Audit ``ref`` (remote ``main`` by default, after one fetch) against HQ ``placement``.

    ``placement`` is the HQ record (anything with ``frame`` and ``epoch``, e.g.
    :class:`beadhive.writer_adopt.PlacementView`); ``None`` when HQ was not read, which leaves
    ``placement_ahead`` false. ``since_epoch`` widens the history walk to start at that epoch's
    adopt instead of the live epoch's. Returns ``cut_over=False`` (no findings) when ``ref``
    carries no ``bh_writer``."""
    if fetch:
        reader.fetch()
    head = _hash(_scalar(reader.query(f"SELECT hashof({quote(ref)}) AS h")), ref)
    database = str(_scalar(reader.query("SELECT database() AS d")) or "")
    if not database or "`" in database:
        raise ValueError(f"fence_audit: unusable database name {database!r}")
    rev = f"`{database}/{head}`"
    at = f"AS OF {quote(head)}"

    tables = {str(_scalar([row])) for row in reader.query(f"SHOW TABLES {at}")}
    p_frame = None if placement is None else str(placement.frame)
    p_epoch = None if placement is None else int(placement.epoch)
    if not {"bh_writer", "bh_epoch_live", "bh_write_mark"} <= tables:
        return FenceAudit(
            ref=ref, head=head, cut_over=False, placement_frame=p_frame, placement_epoch=p_epoch
        )

    writer = reader.query(f"SELECT frame, epoch FROM bh_writer {at} WHERE id = 1")
    writer_frame = str(writer[0]["frame"]) if writer else ""
    writer_epoch = int(writer[0]["epoch"]) if writer else 0
    live_rows = reader.query(f"SELECT epoch FROM bh_epoch_live {at} WHERE id = 1")
    live = int(live_rows[0]["epoch"]) if live_rows else 0

    # ---- stale_marks: a bound value, never a subquery against bh_epoch_live --------------
    stale_count = int(
        _scalar(reader.query(f"SELECT COUNT(*) AS n FROM bh_write_mark {at} WHERE epoch <> {live}"))
        or 0
    )
    stale = tuple(
        StaleMark(str(r["id"]), int(r["epoch"]), str(r.get("tbl") or ""))
        for r in reader.query(
            f"SELECT id, epoch, tbl FROM bh_write_mark {at} WHERE epoch <> {live} "
            f"ORDER BY epoch, id LIMIT {int(evidence_limit)}"
        )
    )

    # ---- one read of every fenced commit reachable from ref: epoch + parents --------------
    # dolt_history_bh_writer (in ref's own revision database) has one row per commit that
    # carries bh_writer; joined with ref's log it gives the fenced DAG in ONE query. NOT
    # dolt_diff_bh_writer: on a merged history it drops commits (measured on Dolt 2.3.5 —
    # an adopt sitting beside a merge vanished from it).
    fenced: dict[str, tuple[int, tuple[str, ...]]] = {}
    for row in reader.query(
        f"SELECT h.commit_hash AS c, h.epoch AS e, l.parents AS p "
        f"FROM {rev}.dolt_history_bh_writer h "
        f"JOIN dolt_log('--parents', {quote(head)}) l ON l.commit_hash = h.commit_hash "
        "WHERE h.id = 1"
    ):
        fenced[_hash(row["c"], "history commit")] = (int(row["e"]), _parents(row.get("p")))

    # ---- epoch_regressed: the highest epoch bh_writer ever carried in ref's history -------
    top = max(fenced.items(), key=lambda kv: (kv[1][0], kv[0]), default=None)
    history_max = top[1][0] if top else writer_epoch
    history_commit = top[0] if top else ""

    # ---- late writes: the I3 walk since the live (or since_epoch's) adopt ----------------
    base = live if since_epoch is None else int(since_epoch)
    window, anchors, unfenced = history_window(fenced, head, base)
    messages = _messages(reader, head, [c for c in window if len(window[c].parents) > 1])
    window = {c: Commit(c, info.parents, messages.get(c, "")) for c, info in window.items()}
    epochs: dict[str, int | None] = {c: e for c, (e, _) in fenced.items()}
    found = find_late_writes(window, epochs, anchors)
    for parent, merge in sorted(unfenced.items()):
        adopt = min(anchors, default="")
        found.append(LateWrite(parent, None, adopt, base, merged_by=merge))
    detail = _messages(reader, head, [w.commit for w in found[: int(evidence_limit)]])
    late = tuple(
        LateWrite(
            w.commit, w.epoch, w.adopt_commit, w.adopt_epoch, detail.get(w.commit, ""), w.merged_by
        )
        for w in found
    )

    return FenceAudit(
        ref=ref,
        head=head,
        cut_over=True,
        writer_frame=writer_frame,
        writer_epoch=writer_epoch,
        live_epoch=live,
        stale_mark_count=stale_count,
        stale_marks=stale,
        history_max_epoch=max(history_max, writer_epoch),
        history_max_commit=history_commit,
        placement_frame=p_frame,
        placement_epoch=p_epoch,
        adopt_commits=tuple(sorted(anchors)),
        history_window=len(window),
        late_writes=late,
    )
