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
    never sets or checks it."""

    bead: str
    seat: str
    worktree: str
    issued_at: str
    expires_at: str = ""
    attestation: str = "none"


@runtime_checkable
class ClaimAuthority(Protocol):
    """The claim-authority seam: mint a record at claim time, verify (or default) a seat against
    it at any later action. `read` is the natural counterpart to `issue` — how a later verb gets
    the record back — and is deliberately part of this protocol so a future tier can shape its own
    storage (signed token file, remote credential service, ...) behind the same three calls."""

    def issue(self, bead: str, seat: str, worktree) -> ClaimRecord: ...
    def read(self, worktree) -> ClaimRecord | None: ...
    def verify(self, record: ClaimRecord | None, action: str, seat: str) -> bool: ...


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


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

    def issue(self, bead: str, seat: str, worktree) -> ClaimRecord:
        record = ClaimRecord(
            bead=bead, seat=seat, worktree=str(worktree), issued_at=_now_iso(), attestation="none"
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
    git config, verify defaults an empty seat to the recorded holder and rejects a mismatch, and
    the registry resolves by name."""
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

    unknown_raised = False
    try:
        get_authority("signed-token")
    except ValueError as exc:
        unknown_raised = "local" in str(exc)
    assert unknown_raised, "unknown authority must raise ValueError listing available authorities"

    print("claim_authority self-check OK:", record)


if __name__ == "__main__":
    _self_check()
