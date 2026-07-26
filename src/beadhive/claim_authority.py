"""Pluggable claim-authority seam — WHO issues and verifies a bead claim, decoupled from HOW the
acting identity gets resolved at each individual `bh work` verb.

Root cause this closes (bh-ejlq): `bh work claim` and `bh work submit` used to each independently
re-derive the acting seat via `identity.resolve_actor(...)` from ambient env/git. `claim` writes
the resolved actor into the bead's `assignee`; `submit` re-derived its own actor and compared the
two with strict string equality (`work_logic._guard_holds_claim`). When the two resolutions
diverged — an explicit `--as` at claim but not at submit, or `$BH_DEV` not surviving between
separate shells/tool-calls — the guard tripped even though the SAME seat legitimately held the
claim throughout.

The fix: `claim` now also `issue()`s a `ClaimRecord` naming the seat it resolved, persisted into
worktree-local state. `submit` (when no explicit `--as` is given) defaults its actor to that
recorded holder instead of re-deriving from env/git — so a no-`--as` submit right after a
successful claim just works. An explicit `--as` still goes through the existing
`identity.resolve_actor` / `_guard_holds_claim` path unchanged, so an explicit mismatch (or a
genuinely unclaimed bead) is still refused exactly as before.

Mirrors the `ConflictEstimator` / estimator-registry pattern in `conflict_estimator.py`: a narrow
`Protocol` plus a name-keyed registry, selected by a config key (`work.identity.authority`,
default `"local"`). The registry is open for a future entry — see the module-level plugin-seam
note in `conflict_estimator.py` for the same shape here — but nothing auto-discovers a plugin; it
must call `register_authority` itself.

**TIER 0 — `LocalTrustAuthority` — LOCAL-TRUST ONLY.** The only implementation shipped today.
`attestation` is always `"none"`: `issue()` just writes the resolved seat to a small JSON file
inside the worktree's OWN git-dir (`git rev-parse --absolute-git-dir` — for a linked worktree this
is its private per-worktree directory under the main repo's `.git/worktrees/<name>`, never shared
with a sibling worktree and orthogonal to the tracked `user.*`/`gpg.*` identity config
`identity.stamp` manages) and `verify()` reads it back at face value. This provides **ZERO spoof
resistance** — any process with filesystem access to the worktree's git-dir can mint or forge a
claim record, exactly as any process that can write bd state could already forge the `assignee`
field this replaces as submit's actor source. It exists to close the papercut above and to
establish the seam. The anti-spoof tiers (signed claim tokens, seat credentials, workload
attestation) are tracked in spike bh-zspz and are expected to be config-selectable drop-ins under
this same `ClaimAuthority` protocol — no rework of claim/submit required when they land.

**TIER 0.5 — the multi-host FENCING TOKEN (bh-ytbb.10).** The first of those extensions to
land, and the reason `issue()`/`read()` carry two more fields than the seat. A record now also
names the `host_id` that minted it and the `epoch` (the host-lease/fence ADOPT generation,
`docs/design/multi-host-model-adr.md` Amendment 1) it was minted under. That turns the record
from a pure memo into a **fencing token**: `guard.guard_claim_epoch` compares the recorded
`epoch` against the generation in force at submit, so a worker that ran for hours *through* a
lost host lease is caught at the WRITE boundary rather than mid-work. It is not spoof
resistance — a local attacker can still forge the file — it is *staleness* resistance, which is
the failure this molecule actually has.

Deliberately **orthogonal to seat verification**: :meth:`LocalTrustAuthority.verify` is
untouched and still answers only "does this record back this seat?". The epoch is a separate
decision on a separate axis (is this record's generation still current?), owned by `guard.py`
where the other write refusals live, so neither check can mask the other.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from .run import run

# The default named authority the registry resolves — mirrors config's `work.identity.authority`
# default. Only this Tier 0 floor ships today.
DEFAULT_AUTHORITY = "local"

# Filename `LocalTrustAuthority` reads/writes inside a worktree's OWN git-dir.
_RECORD_FILENAME = "bh-claim.json"


@dataclass(frozen=True)
class ClaimRecord:
    """Who holds a claim, and how. `attestation` names the trust mechanism behind the record
    (`"none"` for Tier 0's local trust; a future signed-token tier would carry something like
    `"ssh-signed"` alongside a signature). `expires_at` is reserved for a future tier — Tier 0
    never sets or checks it.

    `host_id` + `epoch` are the multi-host **fencing token** (bh-ytbb.10): WHICH machine minted
    the claim, and under which host-lease/fence ADOPT generation. `host_id` is diagnostic only
    — it names the minting host in a refusal and makes orphaned claims detectable at all (ADR
    "Orphan recovery": *today it records only the seat, so orphans are not even detectable*) —
    and, exactly as in `host_fence.EpochFence`, is never itself the thing a decision turns on.
    `epoch` is. Both default to the unfenced values so a record written before this landed, or
    on a single-host factory that never adopted anything, still reads back cleanly."""

    bead: str
    seat: str
    worktree: str
    issued_at: str
    expires_at: str = ""
    attestation: str = "none"
    host_id: str = ""
    epoch: int = 0

    def is_fenced(self) -> bool:
        """Whether this record carries a usable fencing token at all. `epoch` 0 means *no
        generation was in force when this was minted* — an un-adopted single-host factory, or a
        record predating bh-ytbb.10 — and there is nothing to compare it against."""
        return self.epoch > 0

    def is_stale(self, live_epoch: int) -> bool:
        """Whether the generation in force has moved PAST the one this record was minted under
        — i.e. an adopt happened between claim and now, so this token no longer authorizes a
        bead write.

        Strictly `>`: the same epoch is the healthy case, and a live epoch *behind* the record
        is not staleness but a torn/rolled-back read of the lease, which is
        :func:`beadhive.guard.guard_primary`'s problem (it refuses on holder/expiry) and not
        something to convert into a confusing refusal here.

        Fails **open** for an unfenced record (`epoch` 0). That is deliberate and bounded: the
        upgrade window and the never-adopted factory must not start refusing submits for a
        token they were never issued, and `guard_primary` — which does not depend on this field
        at all — is still gating the same verb underneath."""
        return self.is_fenced() and live_epoch > self.epoch


@runtime_checkable
class ClaimAuthority(Protocol):
    """The claim-authority seam: mint a record at claim time, verify (or default) a seat against
    it at any later action. `read` is the natural counterpart to `issue` — how a later verb gets
    the record back — and is deliberately part of this protocol so a future tier can shape its own
    storage (signed token file, remote credential service, ...) behind the same three calls.

    The fencing token (bh-ytbb.10) rides `issue` as KEYWORD-ONLY arguments with unfenced
    defaults, so every existing three-positional-argument call site — and any authority
    implemented against the pre-bh-ytbb.10 shape — keeps working untouched. An authority is
    free to ignore them; `LocalTrustAuthority` persists them."""

    def issue(
        self, bead: str, seat: str, worktree, *, host_id: str = "", epoch: int = 0
    ) -> ClaimRecord: ...
    def read(self, worktree) -> ClaimRecord | None: ...
    def verify(self, record: ClaimRecord | None, action: str, seat: str) -> bool: ...


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _as_epoch(value) -> int:
    """A persisted `epoch` back to an int, or 0 when absent/garbage. 0 reads as *unfenced*
    (see :meth:`ClaimRecord.is_fenced`) — the same direction a pre-bh-ytbb.10 record takes, so
    a corrupt field degrades to "no token" rather than to a spuriously current one."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _record_path(worktree) -> Path | None:
    """The `bh-claim.json` path inside `worktree`'s OWN git-dir, or None when `worktree` isn't
    (yet) a git working tree — issue()/read() then no-op rather than raise, so a claim-authority
    hiccup never blocks the lifecycle verb driving it."""
    res = run(
        ["git", "-C", str(worktree), "rev-parse", "--absolute-git-dir"], check=False, capture=True
    )
    if res.returncode != 0:
        return None
    git_dir = (res.stdout or "").strip()
    return Path(git_dir) / _RECORD_FILENAME if git_dir else None


class LocalTrustAuthority:
    """Tier 0 — LOCAL TRUST ONLY (see module docstring). `issue()` persists the resolved seat into
    a private per-worktree state file; `verify()` trusts the persisted record at face value — no
    signature, no external check."""

    def issue(
        self, bead: str, seat: str, worktree, *, host_id: str = "", epoch: int = 0
    ) -> ClaimRecord:
        record = ClaimRecord(
            bead=bead,
            seat=seat,
            worktree=str(worktree),
            issued_at=_now_iso(),
            attestation="none",
            host_id=host_id,
            epoch=epoch,
        )
        path = _record_path(worktree)
        if path is not None:
            path.write_text(json.dumps(asdict(record)))
        return record

    def read(self, worktree) -> ClaimRecord | None:
        path = _record_path(worktree)
        if path is None or not path.is_file():
            return None
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict) or not data.get("bead") or not data.get("seat"):
            return None
        return ClaimRecord(
            bead=str(data["bead"]),
            seat=str(data["seat"]),
            worktree=str(data.get("worktree") or worktree),
            issued_at=str(data.get("issued_at") or ""),
            expires_at=str(data.get("expires_at") or ""),
            attestation=str(data.get("attestation") or "none"),
            host_id=str(data.get("host_id") or ""),
            epoch=_as_epoch(data.get("epoch")),
        )

    def verify(self, record: ClaimRecord | None, action: str, seat: str) -> bool:
        """True iff `record` backs `seat` acting on this claim. An empty `seat` (no explicit
        override) always verifies against a present record — that's the "default to the recorded
        holder" case submit uses; a non-empty `seat` must match the record's seat exactly. `action`
        is accepted for protocol symmetry with future tiers that may vary verification by action;
        Tier 0 verifies identically for every action."""
        del action
        if record is None:
            return False
        return not seat or seat == record.seat


# The authority registry: named implementations, resolved by `work.identity.authority`. Only the
# `local` Tier 0 floor ships today; the registry is intentionally left OPEN for a future entry (see
# the module docstring's plugin-seam note) — but no loader/discovery populates it, so a plugin must
# `register_authority` itself.
_AUTHORITIES: dict[str, ClaimAuthority] = {DEFAULT_AUTHORITY: LocalTrustAuthority()}


def register_authority(name: str, authority: ClaimAuthority) -> None:
    """Register `authority` under `name` so `work.identity.authority: <name>` resolves it.

    The plugin seam: a future signed-token / credential-service / attestation authority calls this
    to make itself selectable by name. This is NOT plugin discovery — nothing auto-calls it; wiring
    one in is a deliberate, explicit act (out-of-scope loader left for later, same as
    `conflict_estimator.register_estimator`)."""
    _AUTHORITIES[name] = authority


def available_authorities() -> list[str]:
    """The names the registry can resolve, sorted — surfaced in the unknown-authority error."""
    return sorted(_AUTHORITIES)


def get_authority(name: str = DEFAULT_AUTHORITY) -> ClaimAuthority:
    """Resolve the authority registered under `name` (default/config `work.identity.authority`).

    Raises `ValueError` — listing the available names — when `name` is not registered (mirrors
    `conflict_estimator.get_estimator`'s unknown-name behavior)."""
    try:
        return _AUTHORITIES[name]
    except KeyError:
        raise ValueError(
            f"unknown claim authority {name!r}; available: {', '.join(available_authorities())}"
        ) from None


def _self_check() -> None:
    """`python -m beadhive.claim_authority` self-check: issue/read round-trips through worktree
    git config, verify defaults an empty seat to the recorded holder and rejects a mismatch, the
    fencing token round-trips and goes stale on a newer epoch, and the registry resolves by
    name."""
    import tempfile

    def _init_repo(path) -> None:
        run(["git", "init", "-q", str(path)], check=True)

    authority = get_authority()  # local
    with tempfile.TemporaryDirectory() as tmp:
        _init_repo(tmp)
        assert authority.read(tmp) is None, "no record before issue()"

        record = authority.issue("bh-ejlq", "dev/alice", tmp)
        assert record.seat == "dev/alice"
        assert record.attestation == "none"

        back = authority.read(tmp)
        assert back is not None and back.seat == "dev/alice", back

        assert authority.verify(back, "submit", "") is True  # defaults to the recorded holder
        assert authority.verify(back, "submit", "dev/alice") is True  # matching explicit seat
        assert authority.verify(back, "submit", "dev/mallory") is False  # mismatch refused
        assert authority.verify(None, "submit", "") is False  # no record ⇒ never verified
        assert not back.is_fenced()  # issued without a token ⇒ nothing to compare

        fenced = authority.issue("bh-ytbb.10", "dev/alice", tmp, host_id="host-a", epoch=7)
        token = authority.read(tmp)
        assert token is not None and (token.host_id, token.epoch) == ("host-a", 7), token
        assert token.is_stale(8) is True  # an adopt happened mid-work ⇒ stale token
        assert token.is_stale(7) is False  # same generation ⇒ still current
        assert fenced.is_stale(6) is False  # a BEHIND epoch is not staleness
        assert authority.verify(token, "submit", "dev/alice") is True  # seat check untouched

    unknown_raised = False
    try:
        get_authority("signed-token")
    except ValueError as exc:
        unknown_raised = "local" in str(exc)
    assert unknown_raised, "unknown authority must raise ValueError listing available authorities"

    print("claim_authority self-check OK:", record)


if __name__ == "__main__":
    _self_check()
