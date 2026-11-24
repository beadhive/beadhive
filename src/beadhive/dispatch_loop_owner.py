"""The ONE stand-down predicate for "who runs the dispatcher loop" (bh-njarn).

ADR Decision 7 (`docs/design/gas-city-runtime-seam-adr.md`): sharing a beads store with Gas City
is not unsafe in itself; two loops each assuming they own the same concern is. The fix is to
DECLARE the owner. A hive declares it with ``work.dispatch.loop: bh | gc`` (default ``bh``); a
molecule member declares it with the ``dispatch-owner:gc`` label. Both forms are read here, by
this one predicate, so the dispatch pass, lease acquisition, dispatch service install/start,
poll/claim and reclaim can never disagree about who stood down.

Standing down is NARROW: only the loop-owning concerns (picking ready work, the dispatcher
lease, reclaim of in-flight claims, the supervised service). Lifecycle verbs (claim, submit,
review, merge, gate resolve) never consult this module.
"""

from __future__ import annotations

from collections.abc import Iterable

LOOP_BH = "bh"
LOOP_GC = "gc"
LOOPS: tuple[str, ...] = (LOOP_BH, LOOP_GC)

#: The molecule-scoped form of the same declaration (ADR Decision 6, Mode 2).
OWNER_LABEL_GC = "dispatch-owner:gc"

#: What `bh host dispatch status` says for a delegated hive.
DELEGATED_DETAIL = "delegated (loop: gc) — bh not managing dispatch, lease, or reclaim"

ADR_REF = "docs/design/gas-city-runtime-seam-adr.md (Decision 7)"


def declared_loop(cfg: dict | None, entry: dict | None) -> str:
    """The hive's declared loop owner: per-hive `work.dispatch.loop` > global > ``bh``. A
    missing, blank or unknown value reads as ``bh`` — unset behavior must never change."""
    from .config_work_settings import dispatch_loop

    value = str(dispatch_loop(cfg or {}, entry or {}) or "").strip().lower()
    return value if value in LOOPS else LOOP_BH


def hive_delegated(cfg: dict | None, entry: dict | None) -> bool:
    """Hive-scoped stand-down: ``work.dispatch.loop: gc``."""
    return declared_loop(cfg, entry) == LOOP_GC


def member_delegated(labels: Iterable[str] | None) -> bool:
    """Molecule-scoped stand-down: the member carries ``dispatch-owner:gc``."""
    return OWNER_LABEL_GC in (labels or ())


def stands_down(cfg: dict | None, entry: dict | None, labels: Iterable[str] | None = None) -> bool:
    """THE predicate: the hive's loop is gc OR the member is labeled ``dispatch-owner:gc``."""
    return hive_delegated(cfg, entry) or member_delegated(labels)


def refusal(verb: str, hive: str = "") -> str:
    """The message a loop-owning verb refuses with: it names gc as the declared owner."""
    where = f" for {hive}" if hive else ""
    return (
        f"{verb} refused{where}: the dispatcher loop is owned by gc (work.dispatch.loop: gc) — "
        f"bh does not manage dispatch, the lease, or reclaim here. Set work.dispatch.loop: bh "
        f"to take the loop back, or run `bh host dispatch disable` to stop this unit. "
        f"See {ADR_REF}."
    )


class LoopDelegatedError(RuntimeError):
    """A loop-owning verb was invoked for a hive whose loop is owned by gc."""
