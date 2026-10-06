"""State/work pairing: per-(bead, frame) backup refs on the hive's push remote (M14, bh-cqvj6).

Binding condition 18 of ``docs/design/hive-writer-partitioning-adr.md``: bead lifecycle state is
never published unless the matching worktree commits are also on a backup on the remote. The
decision record ``docs/spikes/bh-55vvh-state-work-pairing.md`` (M14) fixes the design this module
implements:

* **D1 — where.** ``refs/bh/backup/<bead>/<frame>`` on ``pairing.remote`` (empty = the hive's
  ``work.push_remote``, normally ``origin``; never ``upstream``). One single-writer ref per
  (bead, frame); every write is a compare-and-swap (``--force-with-lease=<ref>:<last pushed>``,
  empty for create). A frame that loses the CAS stops and reports — it never retries blind.
  Default clones and fetches never download ``refs/bh/*``.
* **D2 — when.** Rule P: a verb that writes work-asserting state pushes the backup FIRST, verifies
  the per-ref status, and only then writes state; a failed push writes nothing. Ordering at write
  time covers every publisher (managed push, bd auto-push, a primary publishing a forwarder's
  write) without touching any of them. :func:`pair` is that step.
* **D3 — recoverable.** One fresh fetch of ``refs/bh/backup/<bead>/*``; the claim-frame's ref
  exists and its tip carries work not already in the remote integration base. A failed fetch is
  ``unknown`` and never licenses a rewind. :func:`recoverability` is the query M3 reads.
* **D7 — retention.** A ref is reaped only when covered (its tip is in the remote integration
  base), superseded/unlanded past a configurable grace, or orphaned past ``orphan_days`` (default
  never). :func:`reap` applies it with CAS deletes.

The secret scan (D7) runs before every push over the commits new to the remote.

**The last pushed sha.** The CAS expectation is kept in a repo-local mirror ref,
``refs/bh/backup-pushed/<bead>/<frame>``, in the hive clone's shared git dir: it survives worktree
re-provisioning and covers batch members and containers, which have no per-bead claim record. The
worktree's ``ClaimRecord.backup_sha`` (M14 D1) is kept in step with it for the checkpoint timer
and diagnostics.

Everything here is a no-op unless the hive's ``bh.pairing.enabled`` policy is on.
"""

from __future__ import annotations

import datetime as _dt
import re
import shlex
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field

from . import work_pairing_policy
from .run import run

GIT_TIMEOUT = 60.0

BACKUP_NS = "refs/bh/backup"
PUSHED_NS = "refs/bh/backup-pushed"
FETCH_NS = "refs/bh/remote-backup"
BASE_NS = "refs/bh/remote-base"
UPSTREAM_REMOTE = "upstream"

# Outcomes of push_backup (M14 D11 item 2).
PUSHED = "pushed"
REFUSED = "refused"
UNREACHABLE = "unreachable"
LEAKED = "leaked"

# Outcomes of recoverability (M14 D3).
RECOVERABLE = "recoverable"
UNBACKED = "unbacked"
UNKNOWN = "unknown"
SUSPECT = "suspect"

CLAIM_FRAME_DIMENSION = "claim-frame"
RECOVERY_DIMENSION = "recovery"
RESUMABLE = "resumable"

_SEGMENT_BAD = re.compile(r"[^A-Za-z0-9._-]")


class PairingRefused(RuntimeError):
    """Rule P refused a work-asserting state write: the backup did not land. Nothing written."""

    def __init__(self, outcome: Outcome, verb: str):
        self.outcome = outcome
        self.verb = verb
        super().__init__(
            f"backup push {outcome.status} for {outcome.bead} → {outcome.remote} {outcome.ref}"
            f"{f' ({outcome.detail})' if outcome.detail else ''} — nothing {verb}"
        )


@dataclass(frozen=True)
class Outcome:
    """One backup push: ``Pushed | Refused | Unreachable | Leaked`` (M14 D11)."""

    status: str
    bead: str
    ref: str
    sha: str
    remote: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == PUSHED


@dataclass(frozen=True)
class RefStatus:
    frame: str
    ref: str
    sha: str
    status: str
    detail: str = ""


@dataclass(frozen=True)
class Recoverability:
    """The D3 answer for one bead: the verdict for its claim-frame, plus every frame's ref."""

    bead: str
    remote: str
    claim_frame: str
    status: str
    base: str = ""
    refs: tuple[RefStatus, ...] = ()
    detail: str = ""

    def to_json(self) -> dict:
        data = asdict(self)
        data["refs"] = [asdict(r) for r in self.refs]
        return data


# ---- naming ------------------------------------------------------------------------------------


def segment(raw: str) -> str:
    """``raw`` as one safe ref-path component: characters outside ``[A-Za-z0-9._-]`` map to
    ``-`` (D1), and the git ref-format traps (leading ``.``, ``..``, a ``.lock`` suffix) are
    defused so every frame id yields a valid ref."""
    value = _SEGMENT_BAD.sub("-", str(raw or "").strip())
    value = value.replace("..", "-")
    if value.startswith("."):
        value = "-" + value[1:]
    if value.endswith(".lock"):
        value = value[: -len(".lock")] + "-lock"
    if not value:
        raise ValueError("an empty frame/bead id cannot name a backup ref")
    return value


def backup_ref(bead: str, frame: str) -> str:
    return f"{BACKUP_NS}/{segment(bead)}/{segment(frame)}"


def pushed_ref(bead: str, frame: str) -> str:
    return f"{PUSHED_NS}/{segment(bead)}/{segment(frame)}"


def local_frame_id() -> str:
    """This machine's placement frame id: the host's enrolled ``host.frame_id`` when it has one,
    else the stable ``host_id`` from ``host.yaml``, else the hostname — always ref-safe."""
    from . import host

    try:
        frame = host.enrolled_frame_id()
        if frame:
            return segment(frame)
    except Exception:
        pass
    try:
        return segment(host.host_id())
    except Exception:
        import socket

        return segment(socket.gethostname())


def resolve_remote(policy, cfg, entry) -> str:
    """``pairing.remote`` or, when empty, the hive's ``work.push_remote`` (D1)."""
    if policy.remote:
        return policy.remote
    from .config_consumer_ports import work_settings as config

    return config.push_remote(cfg, entry)


# ---- git plumbing --------------------------------------------------------------------------------


def _git(args: list[str], cwd) -> subprocess.CompletedProcess:
    try:
        return run(["git", *args], cwd=str(cwd), check=False, capture=True, timeout=GIT_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(exc.cmd, 124, "", f"timed out after {GIT_TIMEOUT}s")


def _line(res) -> str:
    lines = [
        line.strip()
        for line in ((res.stderr or "") + "\n" + (res.stdout or "")).splitlines()
        if line.strip()
    ]
    if not lines:
        return f"exit {res.returncode}"
    verdicts = [line for line in lines if line.startswith("!") or "rejected" in line]
    return verdicts[0] if verdicts else lines[-1]


def _local_sha(cwd, ref: str) -> str:
    res = _git(["rev-parse", "--verify", "--quiet", ref], cwd)
    return (res.stdout or "").strip() if res.returncode == 0 else ""


def is_ancestor(cwd, ancestor: str, descendant: str) -> bool:
    return _git(["merge-base", "--is-ancestor", ancestor, descendant], cwd).returncode == 0


def last_pushed(cwd, bead: str, frame: str) -> str:
    """The CAS expectation: the sha this frame last landed on its backup ref ('' = none)."""
    return _local_sha(cwd, pushed_ref(bead, frame))


# ---- the secret scan (D7) ------------------------------------------------------------------------


def scan_range(cwd, sha: str, expected: str, remote: str, integration: str) -> str:
    """The commits new to the remote: ``<last backup>..<sha>``, else ``<remote base>..<sha>``,
    else the whole history of ``sha`` (a first push to a remote that has no integration branch
    locally tracked)."""
    if expected and _local_sha(cwd, expected):
        return f"{expected}..{sha}"
    base = _local_sha(cwd, f"refs/remotes/{remote}/{integration}") if integration else ""
    if base:
        merge_base = _git(["merge-base", base, sha], cwd)
        mb = (merge_base.stdout or "").strip()
        if merge_base.returncode == 0 and mb:
            return f"{mb}..{sha}"
    return sha


def secret_scan(policy, cwd, rng: str) -> tuple[bool, str]:
    """Run ``pairing.secret_scan.command`` over ``rng``. ``(True, "")`` when clean or disabled.

    A finding refuses the push with the scanner's own location line; a missing scanner refuses
    too unless the operator has turned the scan off (D7)."""
    if not policy.secret_scan_enabled:
        return True, ""
    try:
        argv = [part.replace("{range}", rng) for part in shlex.split(policy.secret_scan_command)]
    except ValueError as exc:
        return False, f"unparseable pairing.secret_scan.command: {exc}"
    if not argv:
        return False, "empty pairing.secret_scan.command"
    try:
        res = run(argv, cwd=str(cwd), check=False, capture=True, timeout=300.0)
    except subprocess.TimeoutExpired:
        return False, "secret scan timed out"
    if res.returncode == 127:
        return False, (
            f"secret scanner {argv[0]!r} not found — install it, or set "
            "bh.pairing.secret_scan.enabled=false to publish unscanned backups"
        )
    if res.returncode != 0:
        return False, f"secret scan refused the push: {_line(res)}"
    return True, ""


# ---- push (D1 + D2) ------------------------------------------------------------------------------


def _parse_porcelain(stdout: str, ref: str) -> tuple[str, str] | None:
    """``(flag, summary)`` for ``ref`` from ``git push --porcelain`` output, if reported."""
    for line in (stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[1].endswith(":" + ref):
            return parts[0][:1], parts[2].strip()
        if len(parts) >= 2 and parts[1] == ":" + ref:
            return parts[0][:1], (parts[2].strip() if len(parts) > 2 else "")
    return None


def push_backup(
    cwd,
    remote: str,
    bead: str,
    frame: str,
    sha: str,
    *,
    policy,
    integration: str = "main",
    expected: str | None = None,
) -> Outcome:
    """Scan, then CAS ``sha`` onto ``refs/bh/backup/<bead>/<frame>`` on ``remote``.

    ``expected`` defaults to this frame's last landed sha (:func:`last_pushed`); an absent value
    asserts the ref does not exist yet. Never retries: a lost CAS comes back ``refused`` with
    git's own line. The result is read from git's per-ref porcelain status, falling back to
    ``ls-remote`` when that is ambiguous."""
    ref = backup_ref(bead, frame)
    if not sha:
        return Outcome(REFUSED, bead, ref, sha, remote, "no commit to back up")
    if not remote or remote == UPSTREAM_REMOTE:
        return Outcome(REFUSED, bead, ref, sha, remote, "backups never push to 'upstream'")
    expected = last_pushed(cwd, bead, frame) if expected is None else expected
    ok, why = secret_scan(policy, cwd, scan_range(cwd, sha, expected, remote, integration))
    if not ok:
        return Outcome(LEAKED, bead, ref, sha, remote, why)
    res = _git(
        ["push", "--porcelain", f"--force-with-lease={ref}:{expected}", remote, f"{sha}:{ref}"],
        cwd,
    )
    status = _parse_porcelain(res.stdout, ref)
    if status is not None:
        flag, summary = status
        if flag == "!":
            return Outcome(REFUSED, bead, ref, sha, remote, summary or _line(res))
        if res.returncode == 0 or flag in ("*", "+", " ", "="):
            _git(["update-ref", pushed_ref(bead, frame), sha], cwd)
            return Outcome(PUSHED, bead, ref, sha, remote, summary)
    if res.returncode == 0:
        # Ambiguous status: verify on the remote itself.
        probe = _git(["ls-remote", remote, ref], cwd)
        line = (probe.stdout or "").strip()
        if probe.returncode == 0 and line.split()[:1] == [sha]:
            _git(["update-ref", pushed_ref(bead, frame), sha], cwd)
            return Outcome(PUSHED, bead, ref, sha, remote, "verified by ls-remote")
        return Outcome(UNREACHABLE, bead, ref, sha, remote, "push status unverifiable")
    return Outcome(UNREACHABLE, bead, ref, sha, remote, _line(res))


def delete_backup(cwd, remote: str, ref: str, sha: str) -> bool:
    """CAS-delete ``ref`` on ``remote`` only while it still points at ``sha``."""
    res = _git(["push", "--porcelain", f"--force-with-lease={ref}:{sha}", remote, f":{ref}"], cwd)
    if res.returncode != 0:
        return False
    parts = ref.split("/")
    if len(parts) >= 5:
        _git(["update-ref", "-d", pushed_ref(parts[3], parts[4])], cwd)
    return True


# ---- rule P -------------------------------------------------------------------------------------


def note_claim_record(worktree, bead: str, frame: str, sha: str) -> None:
    """Keep ``ClaimRecord.backup_sha`` in step with a landed push (best-effort)."""
    if worktree is None:
        return
    try:
        from . import claim_authority

        claim_authority.record_backup(worktree, bead=bead, frame_id=frame, backup_sha=sha)
    except Exception:
        pass


def pair(
    *,
    cfg,
    entry,
    main,
    beads: Iterable[str],
    sha: str,
    verb: str,
    cwd=None,
    worktree=None,
    policy=None,
    echo: Callable[[str], None] | None = None,
) -> list[Outcome]:
    """Rule P (M14 D2): land ``sha`` on every bead's backup ref BEFORE the caller writes state.

    Returns ``[]`` untouched when the hive's pairing switch is off (0.22.x behaviour). Raises
    :class:`PairingRefused` on the first push that does not land — the caller must then write no
    state. Re-running is idempotent: a CAS at the same sha is a no-op."""
    policy = policy if policy is not None else work_pairing_policy.read(main)
    if not policy.enabled:
        return []
    from .config_consumer_ports import work_settings as config

    remote = resolve_remote(policy, cfg, entry)
    integration = config.integration_branch(cfg, entry)
    frame = local_frame_id()
    cwd = cwd or main
    outcomes = []
    for bead in beads:
        outcome = push_backup(cwd, remote, bead, frame, sha, policy=policy, integration=integration)
        if not outcome.ok:
            raise PairingRefused(outcome, verb)
        note_claim_record(worktree, bead, frame, sha)
        outcomes.append(outcome)
        if echo:
            echo(f"  backed up {bead} @ {sha[:12]} → {remote} {outcome.ref}")
    return outcomes


def checkpoint(
    *,
    cfg,
    entry,
    main,
    bead: str,
    sha: str,
    cwd=None,
    worktree=None,
    policy=None,
) -> Outcome | None:
    """A best-effort checkpoint push (D2a): never raises, None when pairing is off. A failed
    checkpoint only warns — it is not a state write, so rule P does not apply."""
    try:
        policy = policy if policy is not None else work_pairing_policy.read(main)
        if not policy.enabled:
            return None
        from .config_consumer_ports import work_settings as config

        remote = resolve_remote(policy, cfg, entry)
        frame = local_frame_id()
        outcome = push_backup(
            cwd or main,
            remote,
            bead,
            frame,
            sha,
            policy=policy,
            integration=config.integration_branch(cfg, entry),
        )
        if outcome.ok:
            note_claim_record(worktree, bead, frame, sha)
        return outcome
    except Exception as exc:  # a checkpoint must never break the verb it rides on
        return Outcome(UNREACHABLE, bead, "", sha, "", f"checkpoint error: {exc}")


# ---- recoverability (D3) -------------------------------------------------------------------------


def fetch_backups(cwd, remote: str, beads: Iterable[str], integration: str) -> tuple[bool, str]:
    """One fresh, pruning fetch of every bead's backup refs plus the remote integration base."""
    specs = [f"+{BACKUP_NS}/{segment(b)}/*:{FETCH_NS}/{segment(b)}/*" for b in beads]
    if integration:
        specs.append(f"+refs/heads/{integration}:{BASE_NS}/{segment(remote)}/{integration}")
    res = _git(["fetch", "--prune", "--no-tags", "--quiet", remote, *specs], cwd)
    if res.returncode != 0:
        # A missing integration branch on the remote must not hide the backups: retry without it.
        res = _git(["fetch", "--prune", "--no-tags", "--quiet", remote, *specs[:-1]], cwd)
        if res.returncode != 0 or not integration:
            return False, _line(res)
    return True, ""


def fetched_refs(cwd, bead: str) -> dict[str, str]:
    """``{frame: sha}`` for the locally fetched backup refs of ``bead``."""
    prefix = f"{FETCH_NS}/{segment(bead)}/"
    res = _git(["for-each-ref", "--format=%(objectname) %(refname)", prefix], cwd)
    out = {}
    for line in (res.stdout or "").splitlines():
        sha, _, name = line.strip().partition(" ")
        if name.startswith(prefix):
            out[name[len(prefix) :]] = sha
    return out


def _unsigned(cwd, base_sha: str, tip: str) -> list[str]:
    rng = f"{base_sha}..{tip}" if base_sha else tip
    res = _git(["log", "--format=%H %G?", rng], cwd)
    if res.returncode != 0:
        return ["<unverifiable>"]
    return [
        line.split()[0] for line in (res.stdout or "").splitlines() if line.split()[1:] != ["G"]
    ]


def classify(cwd, tip: str, base_sha: str, signature_policy: str) -> tuple[str, str]:
    """D3 for one fetched tip: ``unbacked`` when already in base, ``suspect`` under ``strict``
    with an unsigned commit, else ``recoverable``."""
    if base_sha and is_ancestor(cwd, tip, base_sha):
        return UNBACKED, "tip already in the integration base"
    if signature_policy == "strict":
        bad = _unsigned(cwd, base_sha, tip)
        if bad:
            return SUSPECT, f"{len(bad)} commit(s) not signed by an allowed signer"
    return RECOVERABLE, ""


def recoverability(
    cwd,
    remote: str,
    bead: str,
    claim_frame: str,
    *,
    integration: str = "main",
    signature_policy: str = "off",
) -> Recoverability:
    """The D3 query for one bead: is its claim-frame's work recoverable from the remote?

    ``unknown`` when the fetch fails (never rewind on it); ``unbacked`` when the claim-frame has
    no ref or its tip is already in base; ``suspect`` under strict signatures; else
    ``recoverable``. Every other frame's ref is classified too (orphan work, D6)."""
    ok, why = fetch_backups(cwd, remote, [bead], integration)
    if not ok:
        return Recoverability(bead, remote, claim_frame, UNKNOWN, detail=why)
    base_sha = _local_sha(cwd, f"{BASE_NS}/{segment(remote)}/{integration}") if integration else ""
    refs = []
    for frame, tip in sorted(fetched_refs(cwd, bead).items()):
        status, detail = classify(cwd, tip, base_sha, signature_policy)
        refs.append(RefStatus(frame, backup_ref(bead, frame), tip, status, detail))
    mine = next((r for r in refs if claim_frame and r.frame == segment(claim_frame)), None)
    if mine is None:
        status = UNBACKED
        detail = "no claim-frame recorded" if not claim_frame else "no backup ref for claim-frame"
    else:
        status, detail = mine.status, mine.detail
    return Recoverability(bead, remote, claim_frame, status, base_sha, tuple(refs), detail)


# ---- retention (D7) ------------------------------------------------------------------------------


@dataclass
class ReapReport:
    deleted: list[str] = field(default_factory=list)
    kept: list[tuple[str, str]] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    error: str = ""


def _labels(data) -> list[str]:
    return [str(x) for x in ((data or {}).get("labels") or [])]


def state_label(data, dimension: str) -> str:
    """The value of a bd state dimension from a bead's labels (``<dim>:<value>``)."""
    prefix = f"{dimension}:"
    for label in _labels(data):
        if label.startswith(prefix):
            return label[len(prefix) :]
    return ""


def _parse_time(raw) -> _dt.datetime | None:
    if not raw:
        return None
    try:
        value = _dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=_dt.UTC)


def _landed(data) -> bool:
    reason = str((data or {}).get("close_reason") or "").lower()
    return reason.startswith("merged") or "landed" in reason


def retention_verdict(
    data, frame: str, *, covered: bool, policy, now: _dt.datetime
) -> tuple[bool, str]:
    """``(delete?, why)`` for one backup ref of a bead whose current record is ``data``."""
    if covered:
        return True, "covered by the integration base"
    if data is None:
        return False, "bead unreadable — kept"
    status = str(data.get("status") or "")
    if status == "closed":
        closed = _parse_time(data.get("closed_at")) or _parse_time(data.get("updated_at"))
        if _landed(data):
            if state_label(data, CLAIM_FRAME_DIMENSION) == frame:
                return False, "landed holder's ref awaits coverage by the remote base"
            grace = policy.retention_days("superseded")
            label = "superseded"
        else:
            grace = policy.retention_days("unlanded")
            label = "unlanded"
        if grace is None or closed is None:
            return False, f"{label}: no deletion grace applies"
        if now - closed >= _dt.timedelta(days=grace):
            return True, f"{label} for more than {grace}d"
        return False, f"{label}: within the {grace}d grace"
    held = status == "in_progress" or bool(data.get("assignee"))
    if held or state_label(data, RECOVERY_DIMENSION):
        return False, "live claim or marked resumable"
    grace = policy.retention_days("orphan")
    if grace is None:
        return False, "orphan work — never auto-deleted"
    updated = _parse_time(data.get("updated_at"))
    if updated is not None and now - updated >= _dt.timedelta(days=grace):
        return True, f"orphan for more than {grace}d"
    return False, f"orphan: within the {grace}d grace"


def list_remote(cwd, remote: str) -> dict[str, str]:
    """``{ref: sha}`` for every backup ref on ``remote``; raises RuntimeError when unreachable."""
    res = _git(["ls-remote", remote, f"{BACKUP_NS}/*"], cwd)
    if res.returncode != 0:
        raise RuntimeError(_line(res))
    out = {}
    for line in (res.stdout or "").splitlines():
        sha, _, ref = line.strip().partition("\t")
        if ref.startswith(BACKUP_NS + "/"):
            out[ref] = sha
    return out


def reap(
    cwd,
    remote: str,
    *,
    policy,
    integration: str = "main",
    show: Callable[[str], dict | None],
    beads: Iterable[str] | None = None,
    now: _dt.datetime | None = None,
    dry_run: bool = False,
) -> ReapReport:
    """Apply D7 retention to the backup refs on ``remote`` (optionally only ``beads``')."""
    report = ReapReport()
    now = now or _dt.datetime.now(_dt.UTC)
    try:
        remote_refs = list_remote(cwd, remote)
    except RuntimeError as exc:
        report.error = str(exc)
        return report
    wanted = {segment(b) for b in beads} if beads is not None else None
    by_bead: dict[str, dict[str, str]] = {}
    for ref, sha in remote_refs.items():
        parts = ref.split("/")
        if len(parts) != 5:
            continue
        bead, frame = parts[3], parts[4]
        if wanted is not None and bead not in wanted:
            continue
        by_bead.setdefault(bead, {})[frame] = sha
    if not by_bead:
        return report
    ok, why = fetch_backups(cwd, remote, list(by_bead), integration)
    if not ok:
        report.error = why
        return report
    base_sha = _local_sha(cwd, f"{BASE_NS}/{segment(remote)}/{integration}") if integration else ""
    for bead, frames in sorted(by_bead.items()):
        try:
            data = show(bead)
        except Exception:
            data = None
        for frame, sha in sorted(frames.items()):
            ref = f"{BACKUP_NS}/{bead}/{frame}"
            covered = bool(base_sha) and is_ancestor(cwd, sha, base_sha)
            delete, why = retention_verdict(data, frame, covered=covered, policy=policy, now=now)
            if not delete:
                report.kept.append((ref, why))
            elif dry_run or delete_backup(cwd, remote, ref, sha):
                report.deleted.append(ref)
            else:
                report.failed.append(ref)
    return report


def reap_after_land(*, cfg, entry, main, beads: Iterable[str], echo, policy=None) -> None:
    """Best-effort retention sweep after a land/close (D7, M14 D11 item 7). Never raises."""
    try:
        policy = policy if policy is not None else work_pairing_policy.read(main)
        if not policy.enabled:
            return
        from . import bd
        from .config_consumer_ports import work_settings as config

        report = reap(
            main,
            resolve_remote(policy, cfg, entry),
            policy=policy,
            integration=config.integration_branch(cfg, entry),
            show=lambda b: bd.show(b, main),
            beads=beads,
        )
        if report.deleted:
            echo(f"  reaped {len(report.deleted)} backup ref(s) per pairing retention")
        if report.failed:
            echo("⚠ backup ref reap refused for: " + ", ".join(report.failed))
    except Exception as exc:
        echo(f"⚠ backup retention sweep skipped: {exc}")
