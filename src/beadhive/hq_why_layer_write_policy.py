"""``bh hq why`` provider: the off-primary write policy (trust-profiles ADR section 4, bh-sxhnq).

Runs after the config layer and before the forward marker (order 12), because the policy
decides whether the marker is consulted at all:

* ``forward`` — the forward marker decides (the next layer); skipped here.
* ``open`` — never forwards. On a host that is not the hive's writer an ``author`` or ``claim``
  write is **decided** here: it lands on this replica's own data and is published only to
  ``refs/dolt/frame/<id>``, limited to the allowed operations (child create and top-level
  create stay refused). ``publish`` and ``execute`` stay with the writer: the later layers
  decide them.
* ``fenced`` set by an override (``hive.write_policy``) — **refused** on a host that is not
  the writer, naming the override and how to lift it. ``fenced`` because no forwarder is
  configured passes on: the writer fence and placement layers refuse as before.
"""

from __future__ import annotations

from .hq_why import DECIDED, OK, REFUSED, SKIPPED, Layer, Verdict, WhyContext


def _bh() -> str:
    from . import write_policy

    return write_policy._bh()


def check_write_policy(ctx: WhyContext) -> Verdict:
    from . import guard, write_policy

    entry = ctx.memo.get("entry") or {}
    policy = write_policy.for_entry(entry, cfg=ctx.cfg, hive_dir=ctx.memo.get("hive_dir"))
    detail = policy.as_dict()
    if policy.forwards:
        return Verdict(
            "write_policy", SKIPPED, "forward", f"{policy.describe()}: the forward marker decides"
        )
    try:
        where = write_policy.off_primary(
            cfg=ctx.cfg, hive_dir=ctx.memo.get("hive_dir"), hive=ctx.hive
        )
    except guard.WriterUnreadable:
        return Verdict(
            "write_policy", SKIPPED, "fence_unreadable", "the writer fence decides (unreadable)"
        )
    if where is None:
        return Verdict("write_policy", OK, "writer", f"{policy.describe()}; this host writes")
    detail = {**detail, "writer": where.writer, "this_frame": where.this_frame}
    if policy.open:
        if ctx.capability not in guard.OPEN_CAPABILITIES:
            return Verdict(
                "write_policy",
                OK,
                "open_writer_only",
                f"{policy.describe()}: {ctx.capability} stays with the writer {where.writer}",
                detail=detail,
            )
        return Verdict(
            "write_policy",
            DECIDED,
            "open_replica_write",
            f"{policy.describe()}: this write lands on this replica's own data and is published "
            f"only to {write_policy.FRAME_REF.format(frame=where.this_frame or '<frame>')}; "
            f"the writer {where.writer or '?'} merges it. Allowed: "
            f"{', '.join(write_policy.OPEN_OPERATIONS)}; child create is refused "
            f"({write_policy.CHILD_ID_ISSUE}) and top-level create waits on "
            f"{write_policy.CREATE_DECISION}",
            f"{_bh()} doctor",
            detail,
        )
    if policy.resolved == write_policy.FENCED:
        return Verdict(
            "write_policy",
            REFUSED,
            "write_policy_fenced",
            f"{policy.describe()}: only the writer frame {where.writer or '?'} writes this hive",
            f"{_bh()} config unset hive.write_policy",
            detail,
        )
    return Verdict(
        "write_policy",
        OK,
        "no_forwarder",
        f"{policy.describe()}: point a forwarder with `{_bh()} hive forward point "
        f"{policy.hive}`, or run the write on the writer {where.writer or '?'}",
        detail=detail,
    )


LAYERS: tuple[Layer, ...] = (
    Layer("write_policy", 12, check_write_policy, "the hive's off-primary write policy"),
)
