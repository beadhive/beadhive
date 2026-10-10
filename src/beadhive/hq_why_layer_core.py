"""The built-in layers of ``bh hq why`` (trust-profiles ADR section 2, bh-4pki5).

They mirror :func:`beadhive.guard._guard_write`, in the ADR's order:

====== =========== ===================================================================
order  layer       decides
====== =========== ===================================================================
10     config      the config snapshot loads and names the hive
15     forward     a forwarded checkout's marker (the primary decides the write)
20     capability  the frame-eligibility predicates the capability consults (bh-b3ebg)
30     placement   this host holds the hive lease (the ``fenced`` write policy)
40     fence       the in-data writer fence (``bh_writer``) on a cut-over hive
====== =========== ===================================================================

On a cut-over hive the guard consults the writer fence alone, so ``capability`` and
``placement`` report ``skipped`` there and ``fence`` decides; on every other hive ``fence`` is
skipped. Orders are spaced so another provider (``hq_why_layer_<name>``) can slot a layer in
between without editing this module — frame-ref merge conflicts (bh-sfp9o.5) after ``fence``,
trust-profile overrides (bh-bc2rj) before ``capability``.

The capability layer reuses the eligibility decision and its per-capability narrowing
(:meth:`beadhive.frame_eligibility.EligibilityDecision.for_capability`) rather than
re-deriving any predicate.
"""

from __future__ import annotations

from typing import Any

from .hq_why import DECIDED, OK, REFUSED, SKIPPED, Layer, Verdict, WhyContext


def _bh() -> str:
    from . import config

    return config.BINARY_ALIAS


#: The exact command that fixes each frame-eligibility predicate, with ``{frame}``, ``{host}``
#: and ``{hive}`` filled in. Lifecycle verbs are the ``plan`` action: review it, then ``apply``
#: (docs/FRAME-FLEET-MEMBERSHIP.md).
PREDICATE_FIXES: dict[str, str] = {
    "authority_available": "{bh} hq authority rebind",
    "admitted_active": "{bh} host admit plan {frame}",
    "reviewed_admission_or_emergency": "{bh} host admit plan {frame}",
    "not_cordoned": "{bh} host resume plan {frame}",
    "current_frame_incarnation": "{bh} host admit plan {frame}",
    "beadyard_binding": "{bh} hq authority bind-beadyard",
    "authenticated_fresh_heartbeat": "{bh} host daemon status",
    "release_matches": "{bh} host release-upgrade plan {frame}",
    "conformance_pass": "{bh} host release-upgrade check {frame}",
    "capabilities_match_admission": "{bh} host admit plan {frame}",
    "hive_requirements": "{bh} host eligible {host} --hive {hive}",
    "executor_role": "{bh} host show {host}",
    "available_capacity": "{bh} host dispatch status {hive}",
    "dispatch_enabled": "{bh} host dispatch enable {hive}",
}


def predicate_fix(predicate: str, *, frame: str, host: str, hive: str) -> str:
    template = PREDICATE_FIXES.get(predicate, "{bh} host eligible {host} --hive {hive}")
    return template.format(
        bh=_bh(), frame=frame or "<frame>", host=host or "<host>", hive=hive or "<hive>"
    )


# ---- shared reads (memoized on the context) -----------------------------------------------


def _host_id(ctx: WhyContext) -> str:
    def compute() -> str:
        from . import host

        try:
            return host.host_id()
        except FileNotFoundError:
            return ""

    return ctx.remember("host_id", compute)


def _frame_id(ctx: WhyContext) -> str:
    def compute() -> str:
        from . import host

        return host.enrolled_frame_id() or _host_id(ctx)

    return ctx.remember("frame_id", compute)


def _writer(ctx: WhyContext) -> tuple[str, Any]:
    """``("cut_over", state)``, ``("legacy", None)`` or ``("unreadable", exc)``."""

    def compute() -> tuple[str, Any]:
        from . import guard

        try:
            state = guard.writer_state(ctx.hive, cfg=ctx.cfg, hive_dir=ctx.memo.get("hive_dir"))
        except guard.WriterUnreadable as exc:
            return "unreadable", exc
        return ("legacy", None) if state is None else ("cut_over", state)

    return ctx.remember("writer", compute)


def _decision(ctx: WhyContext):
    """The frame's eligibility decision for the hive (``None``: an unenrolled legacy host)."""

    def compute():
        from . import frame_eligibility

        return frame_eligibility.decision_for(
            _host_id(ctx), ctx.memo.get("entry") or {}, cfg=ctx.cfg
        )

    return ctx.remember("decision", compute)


def _fenced_by_data(ctx: WhyContext, layer: str) -> Verdict | None:
    kind, _ = _writer(ctx)
    if kind == "legacy":
        return None
    return Verdict(
        layer,
        SKIPPED,
        "cut_over",
        "not consulted: the hive is cut over, so its in-data writer fence decides alone",
    )


# ---- 10 config ----------------------------------------------------------------------------


def check_config(ctx: WhyContext) -> Verdict:
    from . import config, registry

    _host_id(ctx)  # the report names this host even when the walk stops here
    if ctx.cfg is None:
        try:
            ctx.cfg = config.load()
        except Exception as exc:  # noqa: BLE001 — any load failure is this layer's refusal
            return Verdict(
                "config",
                REFUSED,
                "config_unavailable",
                f"config unavailable: the config snapshot did not load "
                f"({type(exc).__name__}: {exc})",
                f"{_bh()} doctor",
                {"error_type": type(exc).__name__, "error": str(exc)},
            )
    unknown = Verdict(
        "config",
        REFUSED,
        "config_unavailable",
        f"config unavailable: {ctx.hive or 'this directory'} is not a registered hive in the "
        "config snapshot",
        f"{_bh()} hive list",
    )
    try:
        hive_dir = registry.hive_dir_for(ctx.cfg, ctx.hive)
        entry = registry.entry_for_dir(ctx.cfg, hive_dir) or {}
    except Exception as exc:  # noqa: BLE001 — an unresolvable hive is a config refusal
        return Verdict(
            unknown.layer,
            REFUSED,
            unknown.code,
            unknown.summary,
            unknown.command,
            {"error_type": type(exc).__name__, "error": str(exc)},
        )
    if not entry.get("prefix"):
        return unknown
    ctx.memo["hive_dir"] = hive_dir
    ctx.memo["entry"] = entry
    return Verdict("config", OK, "ok", f"config snapshot loaded; hive {entry['prefix']}")


# ---- 15 forward ---------------------------------------------------------------------------


def check_forward(ctx: WhyContext) -> Verdict:
    from . import guard

    marker = guard.forward_marker(ctx.hive, cfg=ctx.cfg, hive_dir=ctx.memo.get("hive_dir"))
    if marker is None:
        return Verdict("forward", SKIPPED, "not_forwarded", "not a forwarded checkout")
    detail = {"state": marker.state, "frame": marker.frame, "reason": marker.reason}
    if marker.state == "forwarding":
        return Verdict(
            "forward",
            DECIDED,
            "forwarded",
            f"this checkout forwards its writes to the primary {marker.frame or '?'}, whose "
            "guard decides them",
            f"{_bh()} hive forward status {marker.prefix}",
            detail,
        )
    return Verdict(
        "forward",
        REFUSED,
        "forward_refused",
        f"forwarding to the primary is refused ({marker.reason or 'no reason recorded'})",
        f"{_bh()} hive forward point {marker.prefix}",
        detail,
    )


# ---- 20 capability ------------------------------------------------------------------------


def check_capability(ctx: WhyContext) -> Verdict:
    from . import capabilities

    if (skipped := _fenced_by_data(ctx, "capability")) is not None:
        return skipped
    decision = _decision(ctx)
    if decision is None:
        return Verdict(
            "capability",
            OK,
            "legacy",
            "not an enrolled frame: the legacy lease policy applies, no predicates to consult",
        )
    narrowed = decision.for_capability(ctx.capability)
    report = {
        name: {
            "allowed": decision.for_capability(name).allowed,
            "reason": decision.for_capability(name).reason,
        }
        for name in capabilities.REGISTRY
    }
    detail = {"predicates": dict(narrowed.predicates), "capabilities": report}
    if narrowed.allowed:
        return Verdict(
            "capability",
            OK,
            "ok",
            f"every predicate {ctx.capability} consults holds",
            detail=detail,
        )
    failing = [name for name, value in narrowed.predicates if not value]
    predicate = failing[0]
    detail["refused_by"] = failing
    return Verdict(
        "capability",
        REFUSED,
        "capability_refused",
        f"capability {ctx.capability} refused by predicate {predicate}"
        + (f" (also: {', '.join(failing[1:])})" if failing[1:] else ""),
        predicate_fix(predicate, frame=_frame_id(ctx), host=_host_id(ctx), hive=ctx.label),
        detail,
    )


# ---- 30 placement -------------------------------------------------------------------------


def _self_placement(ctx: WhyContext):
    """This host's self-placement decision for the hive (bh-vl0r3), or ``None`` when it is
    unreadable or the host runs 0.24.0's adopt (no learned profile, no placement override)."""

    def compute():
        from . import config, self_placement

        try:
            decision = self_placement.evaluate(
                ctx.label, host_id=_host_id(ctx), hq_dir=config.hq_dir()
            )
        except Exception:  # noqa: BLE001 - a diagnostic never fails on this read
            return None
        return decision if decision.mode != self_placement.LEGACY else None

    return ctx.remember("self_placement", compute)


def _not_placed(ctx: WhyContext, lease, cause: str, source: str) -> Verdict:
    free = lease is None or lease.is_tombstone or lease.is_expired()
    holder = "" if lease is None or lease.is_tombstone else lease.host_id
    placing = _self_placement(ctx) if _host_id(ctx) else None
    if placing is not None and placing.mode == "self" and placing.allowed:
        command = f"{_bh()} host lease adopt {ctx.label}"
        summary = (
            f"not placed: {cause}; the {placing.profile} trust profile lets this frame place "
            "itself (no live writer session): adopt it here"
        )
    elif placing is not None and not placing.allowed:
        command = placing.director_command or f"{_bh()} host lease adopt {ctx.label}"
        if placing.action == "live_writer":
            failover = f", failover fires {placing.failover_at}" if placing.failover_at else ""
            summary = (
                f"not placed: {cause}; a live writer session holds it under the "
                f"{placing.profile} trust profile — tenure ends {placing.tenure_ends_at}{failover}"
                + ("; or have the director place this frame" if placing.director_command else "")
            )
        else:
            summary = (
                f"not placed: {cause}; self-placement is refused under the {placing.profile} "
                "trust profile: the director places this frame"
            )
    elif free:
        command = f"{_bh()} host adopt {ctx.label}"
        summary = f"not placed: {cause}; adopt it here"
    elif _decision(ctx) is not None:
        command = f"{_bh()} hq placement show {ctx.label}"
        summary = f"not placed: {cause}; run the write there, or have placement moved here"
    else:
        command = f"{_bh()} host list --lease-hive {ctx.label}"
        summary = f"not placed: {cause}; run the write there, or adopt here once it lapses"
    return Verdict(
        "placement",
        REFUSED,
        "not_placed",
        summary,
        command,
        {
            "holder": holder,
            "cause": cause,
            "expires_at": getattr(lease, "expires_at", ""),
            "epoch": getattr(lease, "epoch", 0),
            "source": source,
            "this_host": _host_id(ctx),
            **({"self_placement": placing.as_dict()} if placing is not None else {}),
        },
    )


def _cause(lease) -> str:
    if lease is None:
        return "nobody holds the hive lease"
    if lease.is_tombstone:
        return f"the hive lease was released ({lease.describe()})"
    if lease.is_expired():
        return f"the hive lease EXPIRED ({lease.describe()})"
    return f"held by {lease.describe()}"


def check_placement(ctx: WhyContext) -> Verdict:
    from . import frame_eligibility, guard

    if (skipped := _fenced_by_data(ctx, "placement")) is not None:
        return skipped
    this_host = _host_id(ctx)
    hive_dir = ctx.memo.get("hive_dir")
    if _decision(ctx) is not None:
        try:
            _prefix, _identity, lease = frame_eligibility.authoritative_primary(
                ctx.hive, cfg=ctx.cfg, hive_dir=hive_dir
            )
        except frame_eligibility.EligibilityError as exc:
            return Verdict(
                "placement",
                REFUSED,
                "not_placed",
                f"not placed: the authoritative hive lease could not be read ({exc})",
                f"{_bh()} hq placement show {ctx.label}",
                {"holder": "", "cause": str(exc), "this_host": this_host},
            )
        if lease is not None and lease.held_by(this_host):
            return Verdict("placement", OK, "ok", "this host holds the hive lease")
        return _not_placed(ctx, lease, _cause(lease), guard.SOURCE_HQ)
    state = guard.primary_state(ctx.hive, cfg=ctx.cfg, hive_dir=hive_dir)
    if state is None:
        return Verdict(
            "placement",
            SKIPPED,
            "unplaced_default",
            "no host lease was ever adopted: single-host default, nothing to hold",
        )
    prefix, this_host, lease = state
    if lease.held_by(this_host):
        return Verdict("placement", OK, "ok", "this host holds the hive lease")
    lease, source = guard._refresh_expired(prefix, lease)
    if lease is None or lease.held_by(this_host):
        return Verdict("placement", OK, "ok", "this host holds the hive lease (cache refreshed)")
    return _not_placed(ctx, lease, _cause(lease), source)


# ---- 40 fence -----------------------------------------------------------------------------


def check_fence(ctx: WhyContext) -> Verdict:
    kind, value = _writer(ctx)
    if kind == "legacy":
        return Verdict(
            "fence", SKIPPED, "not_cut_over", "not cut over: the hive lease decides writes"
        )
    if kind == "unreadable":
        return Verdict(
            "fence",
            REFUSED,
            "fence_unreadable",
            f"the in-data writer fence could not be read ({value}); writes fail closed",
            f"{_bh()} doctor",
            {"error": str(value)},
        )
    prefix, this_frame, writer = value
    detail = {"writer": writer.frame, "epoch": writer.epoch, "this_frame": this_frame}
    if this_frame and writer.frame == this_frame:
        return Verdict("fence", OK, "ok", f"bh_writer names this frame at epoch {writer.epoch}")
    return Verdict(
        "fence",
        REFUSED,
        "fence_foreign",
        f"the writer fence names another frame: {writer.frame or '?'} at epoch {writer.epoch} "
        f"(this frame: {this_frame or '?'})",
        f"{_bh()} host adopt {prefix}",
        detail,
    )


LAYERS: tuple[Layer, ...] = (
    Layer("config", 10, check_config, "the config snapshot loads and names the hive"),
    Layer("forward", 15, check_forward, "a forwarded checkout's marker"),
    Layer("capability", 20, check_capability, "the capability's eligibility predicates"),
    Layer("placement", 30, check_placement, "this host holds the hive lease"),
    Layer("fence", 40, check_fence, "the in-data writer fence on a cut-over hive"),
)
