"""The managed write path's divert and the writer's orphan merge (bh-4z3oz, P-M6).

ADR ``docs/design/hive-writer-partitioning-adr.md`` §2 "Managed write path" (``bh-vje85`` E3,
R3; prototype ``tests/harness/composed_fence.py``). Everything here is dormant until a hive's
data is cut over: on a legacy hive :func:`managed_preflight` answers "proceed" without a read.

* **Commit before push** (:func:`managed_preflight`): in server mode bd's write commit leaves the
  guard's marks unstaged (``bh-vje85`` E7), so ``Engine.push_state`` runs ``bd dolt commit``
  first. On a cut-over hive in server mode a FAILED commit now refuses the push instead of
  pushing a ``main`` whose marks stayed behind.
* **Divert when superseded** (:func:`divert_if_superseded`): fetch (never merge) and compare the
  ``bh_writer.epoch`` this frame's committed ``main`` holds with the remote head's. When the
  remote is ahead the frame stops writing ``main``: it commits its working set, pushes its
  unpublished commits to a fresh ``frame/<id>/orphan-<epoch>-<n>`` branch, verifies the branch
  landed, and only then resets to the remote head through :meth:`FenceNode.sync_to_remote`.
  In server mode that reset is :meth:`beadhive.fence_data.BdServerEngine.reset_to_remote`,
  which kills every forwarder session first (``quiesce_before_reset``, M13 E5), so an in-flight
  forwarded write is refused rather than acknowledged and then dropped by the reset. Nothing is
  lost: the reset never runs before the orphan is on the remote.
* **Orphan merge** (:func:`merge_orphan`): the writer merges one named orphan deliberately, in
  ONE SQL session: ``dolt_force_transaction_commit`` lets the retired-epoch marks past the
  foreign key for exactly that session, the marks are re-stamped to the live epoch, the
  violation rows are cleared and the merge is committed as ``bh: merge frame/…`` — the subject
  :mod:`beadhive.fence_audit`'s history check sanctions. It never runs a plain ``vc merge`` and
  never resolves a conflict: any conflict (on ``bh_*`` or elsewhere) refuses before the merge.

The worktree-commit pairing (M14, ``bh-cqvj6``) is unchanged: rule P backs up a bead's work
before its state write, so bead state that ends up on an orphan still has its work on the
per-(bead, frame) backup ref.

Typer-free; the CLI is :mod:`beadhive.hive_fence_cli` (``bh hive fence orphans|orphan-merge``).
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import fence_audit, log
from .fence_data import FenceError, FenceNode, SqlFailed
from .fence_schema import quote
from .writer_adopt import DataUnreachable, WriterRow

__all__ = [
    "MERGE_PREFIX",
    "ORPHAN_PATTERN",
    "DivertFailed",
    "DivertOutcome",
    "MergeResult",
    "Orphan",
    "OrphanError",
    "OrphanMergeFailed",
    "OrphanMergeRefused",
    "committed_writer",
    "divert_if_superseded",
    "is_non_fast_forward",
    "list_orphans",
    "managed_preflight",
    "merge_orphan",
    "orphan_branch",
    "parse_orphan",
]

#: The sanctioned merge subject (shared with :mod:`beadhive.fence_audit`).
MERGE_PREFIX = fence_audit.MERGE_PREFIX
#: ``frame/<frame>/orphan-<epoch>-<n>``.
ORPHAN_PATTERN = re.compile(
    r"frame/(?P<frame>[A-Za-z0-9._-]+)/orphan-(?P<epoch>[0-9]+)-(?P<n>[0-9a-z]+)"
)
_FRAME = re.compile(r"[A-Za-z0-9._-]+")
_HASH = re.compile(r"[0-9a-v]{32}")


class OrphanError(FenceError):
    """The divert or the orphan merge could not be completed."""


class DivertFailed(OrphanError):
    """The superseded frame could not publish its orphan; it did NOT reset (nothing lost)."""


class OrphanMergeRefused(OrphanError):
    """The orphan merge was refused before anything was written."""


class OrphanMergeFailed(OrphanError):
    """The one-session orphan merge did not land as a clean, re-stamped merge commit."""


# =============================================================================================
# Names and reads
# =============================================================================================


def orphan_branch(frame: str, epoch: int, n: int | str) -> str:
    if not _FRAME.fullmatch(frame or ""):
        raise OrphanError(f"frame id {frame!r} cannot name an orphan branch")
    return f"frame/{frame}/orphan-{int(epoch)}-{n}"


def parse_orphan(branch: str) -> tuple[str, int, str] | None:
    """``(frame, epoch, n)`` for an orphan branch name, else ``None``."""
    m = ORPHAN_PATTERN.fullmatch(branch or "")
    return (m["frame"], int(m["epoch"]), m["n"]) if m else None


@dataclass(frozen=True)
class Orphan:
    """One published orphan branch on the hive's remote."""

    branch: str
    frame: str
    epoch: int
    n: str
    commit: str

    def as_dict(self) -> dict:
        return {
            "branch": self.branch,
            "frame": self.frame,
            "epoch": self.epoch,
            "n": self.n,
            "commit": self.commit,
        }


def _scalar(rows: list[dict]) -> object:
    return next(iter(rows[0].values())) if rows and rows[0] else None


def _truthy(value: object) -> bool:
    return str(value).strip().lower() in {"1", "true"}


def _remote_ref(node: FenceNode, branch: str) -> str:
    return f"{node.remote}/{branch}"


def _hashof(node: FenceNode, rev: str) -> str:
    text = str(_scalar(node.query(f"SELECT hashof({quote(rev)}) AS h")) or "")
    if not _HASH.fullmatch(text):
        raise SqlFailed(f"hashof({rev}) is not a commit hash: {text!r}")
    return text


def committed_writer(node: FenceNode, rev: str = "main") -> WriterRow | None:
    """``bh_writer`` as of a COMMIT (``rev``). A refused server-mode pull leaves the new writer
    merged into the working set only (``bh-vje85`` E6), so the held epoch is read from history.
    ``None`` when that commit carries no ``bh_writer``."""
    try:
        rows = node.query(f"SELECT frame, epoch FROM bh_writer AS OF {quote(rev)} WHERE id = 1")
    except SqlFailed as exc:
        if "not found" in str(exc).lower():
            return None
        raise
    if not rows:
        return None
    return WriterRow(str(rows[0]["frame"]), int(rows[0]["epoch"]))


def _published(node: FenceNode) -> bool:
    """Whether committed ``main`` is already contained in the remote head."""
    remote = quote(_remote_ref(node, node.branch))
    rows = node.query(
        f"SELECT dolt_merge_base({quote(node.branch)}, {remote}) = hashof({quote(node.branch)}) "
        "AS published"
    )
    return _truthy(_scalar(rows)) if rows else True


def _contains(node: FenceNode, commit: str, rev: str) -> bool:
    c = quote(commit)
    rows = node.query(f"SELECT dolt_merge_base({c}, {quote(rev)}) = {c} AS m")
    return _truthy(_scalar(rows))


def _remote_branches(node: FenceNode) -> dict[str, str]:
    """``{branch: hash}`` of the remote-tracking branches (after the caller's fetch)."""
    prefix = f"remotes/{node.remote}/"
    out = {}
    for row in node.query("SELECT name, hash FROM dolt_remote_branches"):
        name = str(row.get("name") or "")
        if name.startswith(prefix):
            out[name[len(prefix) :]] = str(row.get("hash") or "")
    return out


def list_orphans(
    node: FenceNode, *, fetch: bool = True, include_merged: bool = False
) -> list[Orphan]:
    """Published orphan branches NOT yet merged into the remote head (``include_merged``: all of
    them). Pure read: a fetch moves only remote-tracking refs."""
    if fetch:
        node.fetch()
    head = _remote_ref(node, node.branch)
    out = []
    for name, commit in sorted(_remote_branches(node).items()):
        parsed = parse_orphan(name)
        if parsed is None:
            continue
        if not include_merged and _contains(node, commit, head):
            continue
        frame, epoch, n = parsed
        out.append(Orphan(name, frame, epoch, n, commit))
    return out


def _next_n(node: FenceNode, frame: str, epoch: int) -> int:
    taken = [
        int(p[2])
        for name in _remote_branches(node)
        if (p := parse_orphan(name)) and p[0] == frame and p[1] == epoch and p[2].isdigit()
    ]
    return max(taken, default=0) + 1


# =============================================================================================
# The divert
# =============================================================================================


@dataclass(frozen=True)
class DivertOutcome:
    #: The remote head names a higher ``bh_writer.epoch`` than this frame's committed ``main``.
    superseded: bool
    held: WriterRow | None = None
    remote: WriterRow | None = None
    #: The orphan branch the unpublished commits went to (``None``: nothing was unpublished).
    branch: str | None = None
    commit: str | None = None
    #: Whether local ``main`` was reset to the remote head.
    reset: bool = False

    def describe(self, prefix: str) -> str:
        if not self.superseded:
            return f"{prefix}: current (remote writer epoch is not ahead)"
        held = f"epoch {self.held.epoch}" if self.held else "an unknown epoch"
        remote = f"{self.remote.frame}@{self.remote.epoch}" if self.remote else "?"
        kept = (
            f"its unpublished commits are on {self.branch} ({(self.commit or '')[:12]})"
            if self.branch
            else "nothing was unpublished"
        )
        return (
            f"{prefix}: superseded — this frame held {held} but the remote writer is {remote}. "
            f"main was NOT pushed; {kept}; local main was reset to the remote head. The writer "
            f"merges the orphan with `bh hive fence orphan-merge {prefix} <branch>`."
        )


def divert_if_superseded(node: FenceNode, *, frame: str) -> DivertOutcome:
    """The managed rejoin (ADR §2): fetch without merging; when the remote head's
    ``bh_writer.epoch`` is above the one committed ``main`` holds, publish the unpublished
    commits to ``frame/<frame>/orphan-<held epoch>-<n>`` and reset to the remote head.

    Never pushes ``main`` and never merges. The reset runs only after the orphan is verified
    on the remote (:class:`DivertFailed` otherwise, with nothing reset), and goes through
    :meth:`FenceNode.sync_to_remote`, whose server-mode engine quiesces forwarders first."""
    node.fetch()
    held = committed_writer(node, node.branch)
    remote = committed_writer(node, _remote_ref(node, node.branch))
    if remote is None or held is None or remote.epoch <= held.epoch:
        return DivertOutcome(superseded=False, held=held, remote=remote)
    logger = log.get_logger(__name__)
    node.engine.commit("bh: commit working set before orphan diversion")
    branch = commit = None
    if not _published(node):
        branch = orphan_branch(frame, held.epoch, _next_n(node, frame, held.epoch))
        commit = _hashof(node, node.branch)
        try:
            node.engine.push_branch(branch)
            node.fetch()
            landed = _remote_branches(node).get(branch, "")
        except (DataUnreachable, SqlFailed) as exc:
            raise DivertFailed(
                f"superseded (remote writer {remote.frame}@{remote.epoch}), but the orphan "
                f"{branch} could not be published: {exc}. Local main was NOT reset; nothing "
                "was lost. Retry the push once the remote is reachable."
            ) from exc
        if landed != commit:
            raise DivertFailed(
                f"orphan {branch} is {landed or 'missing'} on the remote, not {commit}; local "
                "main was NOT reset (nothing lost)"
            )
        logger.warning(
            "fence_orphan_diverted",
            branch=branch,
            commit=commit,
            held_epoch=held.epoch,
            remote_writer=remote.frame,
            remote_epoch=remote.epoch,
        )
    node.sync_to_remote()
    return DivertOutcome(
        superseded=True, held=held, remote=remote, branch=branch, commit=commit, reset=True
    )


def _failed_commit(result: object) -> str:
    """Non-empty when ``bd dolt commit`` failed (``Nothing to commit.`` exits 0)."""
    if result is None or not getattr(result, "returncode", 0):
        return ""
    text = f"{getattr(result, 'stdout', '') or ''}{getattr(result, 'stderr', '') or ''}"
    if "nothing to commit" in text.lower():
        return ""
    return text.strip()[:400] or f"exit {result.returncode}"


def is_non_fast_forward(result: object) -> bool:
    """Whether a failed push reads as a lost race (non-fast-forward), not an unreachable remote."""
    from .fence_data import _NON_FF_MARKERS

    text = f"{getattr(result, 'stdout', '') or ''}{getattr(result, 'stderr', '') or ''}".lower()
    return bool(getattr(result, "returncode", 0)) and any(m in text for m in _NON_FF_MARKERS)


def managed_preflight(
    cwd: Path, *, commit_result: subprocess.CompletedProcess | None = None, cfg=None
) -> str | None:
    """The cut-over half of ``Engine.push_state``, run after its ``bd dolt commit``.

    ``None`` means proceed (a legacy hive, or a cut-over hive whose remote writer is not ahead).
    A string is the refusal: the commit-before-push failed in server mode, the remote epoch
    could not be checked (fail closed), or the frame was superseded and diverted — ``main`` is
    never pushed in any of these."""
    from . import fence_data, fence_data_port, guard  # lazy: only cut-over hives get past here

    try:
        state = guard.writer_state(cfg=cfg, hive_dir=Path(cwd))
    except guard.WriterUnreadable as exc:
        return f"managed push refused: {exc}"
    if state is None:
        return None
    prefix, this_frame, _writer = state
    node = fence_data_port.fence_data_for(prefix, Path(cwd))
    if not isinstance(node, FenceNode):
        return None
    if isinstance(node.engine, fence_data.BdServerEngine):
        failed = _failed_commit(commit_result)
        if failed:
            return (
                f"{prefix}: commit-before-push failed, so the guard's marks would not travel "
                f"with the write; managed push refused before data transfer.\n  bd: {failed}"
            )
    try:
        outcome = divert_if_superseded(node, frame=this_frame)
    except DivertFailed as exc:
        return f"{prefix}: {exc}"
    except (FenceError, DataUnreachable, RuntimeError) as exc:
        return (
            f"{prefix}: could not check the remote writer epoch before pushing main (fail "
            f"closed): {exc}"
        )
    if outcome.superseded:
        return outcome.describe(prefix)
    return None


# =============================================================================================
# The writer's orphan merge
# =============================================================================================


@dataclass(frozen=True)
class MergeResult:
    branch: str
    orphan_commit: str
    epoch: int
    #: ``True`` when the orphan was already contained in local ``main`` (nothing written).
    already: bool = False
    commit: str | None = None
    #: Marks the merge brought in, re-stamped to ``epoch``.
    restamped: int = 0
    parents: tuple[str, ...] = field(default=())

    def as_dict(self) -> dict:
        return {
            "branch": self.branch,
            "orphan_commit": self.orphan_commit,
            "epoch": self.epoch,
            "already": self.already,
            "commit": self.commit,
            "restamped": self.restamped,
            "parents": list(self.parents),
        }


def merge_message(branch: str, epoch: int) -> str:
    return f"bh: merge {branch}, re-stamp its marks at epoch {int(epoch)}"


def _mark_ids(node: FenceNode, rev: str) -> set[str]:
    return {str(r["id"]) for r in node.query(f"SELECT id FROM bh_write_mark AS OF {quote(rev)}")}


def _conflicts_preview(node: FenceNode, rev: str) -> list[str]:
    rows = node.query(
        "SELECT `table` AS t, num_data_conflicts AS d, num_schema_conflicts AS s FROM "
        f"DOLT_PREVIEW_MERGE_CONFLICTS_SUMMARY({quote(node.branch)}, {quote(rev)})"
    )
    return sorted(str(r["t"]) for r in rows if int(r.get("d") or 0) or int(r.get("s") or 0))


def _postflight(node: FenceNode, prev: str, orphan: str, message: str, epoch: int) -> list[str]:
    problems = []
    head = _hashof(node, node.branch)
    h = quote(head)
    rows = node.query(
        f"SELECT parents AS p, message AS m FROM dolt_log('--parents', {h}) WHERE commit_hash = {h}"
    )
    row = rows[0] if rows else {}
    parents = tuple(sorted(p.strip() for p in str(row.get("p") or "").split(",") if p.strip()))
    subject = str(row.get("m") or "")
    if parents != tuple(sorted((prev, orphan))):
        problems.append(f"HEAD {head[:12]} is not the merge of {prev[:12]} and {orphan[:12]}")
    if subject != message:
        problems.append(f"HEAD subject is {subject[:80]!r}, not {message!r}")
    if node.query("SELECT `table` FROM dolt_constraint_violations"):
        problems.append("constraint violations remain in the working set")
    if node.query("SELECT `table` FROM dolt_conflicts"):
        problems.append("conflicts remain in the working set")
    stale = node.query(f"SELECT COUNT(*) AS n FROM bh_write_mark WHERE epoch <> {int(epoch)}")
    if int(_scalar(stale) or 0):
        problems.append(f"{_scalar(stale)} marks are not at the live epoch {epoch}")
    return problems


def _restore(node: FenceNode, prev: str) -> list[str]:
    """After a failed merge: abort an open merge and hard-reset local ``main`` back to ``prev``
    (the pre-merge head) through the engine's sanctioned reset, so nothing the failed batch
    committed (a non-merge re-stamp commit) can ride the next managed push. Returns what it
    did; a ``main`` it could not restore is reported, loudly, as such."""
    try:
        head = _hashof(node, node.branch)
    except SqlFailed:
        head = ""
    try:
        node.engine.reset_hard(prev)
        after = _hashof(node, node.branch)
    except (SqlFailed, DataUnreachable) as exc:
        return [f"MAIN NOT RESTORED to {prev} ({exc}) — do not push; reset it by hand"]
    if after != prev:
        return [f"MAIN NOT RESTORED: it is {after}, not {prev} — do not push; reset it by hand"]
    log.get_logger(__name__).warning(
        "fence_orphan_merge_restored", prev=prev, discarded=head if head != prev else ""
    )
    moved = f" (discarded {head[:12]})" if head and head != prev else ""
    return [f"local main was restored to {prev[:12]}{moved}"]


def merge_orphan(node: FenceNode, branch: str, *, frame: str) -> MergeResult:
    """Merge the published orphan ``branch`` into local ``main`` on the writer, re-stamping its
    marks at the live epoch, in one SQL session. Not pushed: publishing is the caller's
    managed push.

    Refuses (:class:`OrphanMergeRefused`, nothing written) unless: ``branch`` is an orphan name,
    this node carries all 44 triggers, its local ``bh_writer`` names ``frame``, the remote writer
    is not ahead, the orphan exists on the remote, and the merge would raise no conflict at all
    — conflicts are never resolved here, on ``bh_*`` or anywhere else. Raises
    :class:`OrphanMergeFailed` when the merge did not land as the expected merge commit (an
    open merge is aborted)."""
    if parse_orphan(branch) is None:
        raise OrphanMergeRefused(
            f"{branch!r} is not an orphan branch (frame/<id>/orphan-<epoch>-<n>); only orphans "
            "merge through this verb — never a plain vc merge"
        )
    node.require_writer_ready()
    local = node.writer()
    if local is None or not frame or local.frame != frame:
        held = f"{local.frame}@{local.epoch}" if local else "nobody (not cut over)"
        raise OrphanMergeRefused(
            f"only the writer merges an orphan: local bh_writer names {held}, this frame is "
            f"{frame or '?'}"
        )
    node.fetch()
    remote = committed_writer(node, _remote_ref(node, node.branch))
    if remote is not None and remote.epoch > local.epoch:
        raise OrphanMergeRefused(
            f"this frame is superseded (remote writer {remote.frame}@{remote.epoch} > "
            f"{local.epoch}); it must divert, not merge"
        )
    node.engine.commit("bh: commit working set before orphan merge")
    rev = _remote_ref(node, branch)
    orphan = _remote_branches(node).get(branch, "")
    if not orphan:
        raise OrphanMergeRefused(f"no orphan {branch} on {node.remote} (after a fetch)")
    live = int(_scalar(node.query("SELECT epoch FROM bh_epoch_live WHERE id = 1")) or 0)
    if live != local.epoch:
        raise OrphanMergeRefused(
            f"live epoch {live} does not match bh_writer epoch {local.epoch}; re-run the adopt"
        )
    if _contains(node, orphan, node.branch):
        return MergeResult(branch, orphan, live, already=True)
    conflicts = _conflicts_preview(node, rev)
    if conflicts:
        fenced = [t for t in conflicts if t.startswith("bh_")]
        never = f" (and one on {', '.join(fenced)} is never resolved at all)" if fenced else ""
        raise OrphanMergeRefused(
            f"merging {branch} would conflict on {', '.join(conflicts)}; this verb never "
            f"resolves a conflict{never}. Nothing was written."
        )
    prev = _hashof(node, node.branch)
    before = _mark_ids(node, prev)
    message = merge_message(branch, live)
    # bd's batch does not stop at a failing statement, so every statement after the merge is
    # guarded in SQL on the merge actually being open: a failed DOLT_MERGE re-stamps nothing,
    # deletes nothing and commits nothing (a NULL message aborts DOLT_COMMIT).
    statements = [
        "SET @@dolt_force_transaction_commit = 1",
        f"CALL DOLT_MERGE('--no-ff', '--no-commit', {quote(rev)})",
        "SET @bh_merging = (SELECT COUNT(*) FROM dolt_merge_status WHERE is_merging)",
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
        problems = _postflight(node, prev, orphan, message, live)
    except (SqlFailed, DataUnreachable) as exc:
        problems = [f"postflight unreadable: {exc}"]
    if problems:
        problems += _restore(node, prev)
        raise OrphanMergeFailed(
            f"orphan merge of {branch} did not land cleanly: {'; '.join(problems)}"
            + (f" (session: {error})" if error else "")
        )
    head = _hashof(node, node.branch)
    restamped = len(_mark_ids(node, head) - before)
    log.get_logger(__name__).info(
        "fence_orphan_merged", branch=branch, commit=head, epoch=live, restamped=restamped
    )
    return MergeResult(
        branch,
        orphan,
        live,
        commit=head,
        restamped=restamped,
        parents=(prev, orphan),
    )
