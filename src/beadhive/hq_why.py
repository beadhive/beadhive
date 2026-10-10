"""``bh hq why <hive>`` — which layer refuses a write here, and the one command that fixes it.

Trust-profiles ADR section 2 (bh-4pki5). A write verb is decided by a stack of layers — the
config snapshot, the capability's eligibility predicates, placement, the writer fence — and
before this an operator reconstructed a refusal from predicate names and runbooks. ``why``
walks the same layers the guard (:func:`beadhive.guard.guard_capability`) does, for this host
and one hive, and reports the FIRST layer that refuses, with the exact command to run next.

The walk is open for extension and closed for modification. A layer is a :class:`Layer` — a
name, an ``order`` and a ``check`` — and the walk visits every registered layer by ``order``.
Layers come from :func:`register_layer` and from **provider modules**: every root module named
``hq_why_layer_<name>`` is imported on first use and its ``LAYERS`` tuple registered. A later
layer (frame-ref merge conflicts, trust-profile overrides) ships as its own provider module and
picks an ``order`` between the built-in ones (:mod:`beadhive.hq_why_layer_core`); nothing here
changes.

A check returns one :class:`Verdict`:

* ``refused`` — this layer refuses the write; the walk stops and reports it.
* ``ok`` — this layer allows; the walk continues.
* ``skipped`` — this layer is not consulted for this hive (a legacy single-host hive has no
  placement, a cut-over hive is decided by its writer fence alone); the walk continues.
* ``decided`` — this layer allows and no later layer is consulted (a forwarded checkout is
  decided by the primary); the walk stops with an allow.

A check that raises is reported as that layer refusing: the guard fails closed on the same
error, so ``why`` must not report an allow it cannot prove.

This module reads nothing at import time and imports no HQ or config code, so ``bh hq why
--help`` never needs HQ (the bh-o01m6 lesson).
"""

from __future__ import annotations

import pkgutil
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

OK = "ok"
REFUSED = "refused"
SKIPPED = "skipped"
DECIDED = "decided"
STATUSES = (OK, REFUSED, SKIPPED, DECIDED)

#: Root modules with this prefix are layer providers (module docstring).
PROVIDER_PREFIX = "hq_why_layer_"


@dataclass(frozen=True)
class Verdict:
    """One layer's answer for this host, hive and capability."""

    layer: str
    status: str
    #: A stable machine code: ``config_unavailable``, ``capability_refused``, ``not_placed``,
    #: ``fence_foreign``, ``ok`` … — what ``--json`` consumers branch on.
    code: str
    summary: str
    #: The exact command to run next; empty only on a layer that passed or was skipped.
    command: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"unknown verdict status {self.status!r} (known: {STATUSES})")

    def as_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "status": self.status,
            "code": self.code,
            "summary": self.summary,
            "command": self.command,
            "detail": dict(self.detail),
        }


@dataclass(frozen=True)
class Layer:
    """One registered layer of the walk."""

    name: str
    #: Walk position; lower runs first. Built-ins use multiples of 10 so a provider can slot
    #: between them (:mod:`beadhive.hq_why_layer_core`).
    order: int
    check: Callable[[WhyContext], Verdict]
    description: str = ""


class WhyContext:
    """What one walk is asked about, plus a memo layers share.

    Layers read this host's identity, config and hive resolution through the context so each
    is resolved at most once per walk and a test can hand a walk a prepared context. ``memo``
    carries what one layer learned for a later one (the config layer's resolved entry, the
    eligibility decision the placement layer needs)."""

    def __init__(self, hive: str, capability: str, *, cfg: Any = None) -> None:
        self.hive = hive
        self.capability = capability
        self.cfg = cfg
        self.memo: dict[str, Any] = {}

    @property
    def label(self) -> str:
        """The hive as an operator names it: its prefix once resolved, else the argument."""
        entry = self.memo.get("entry") or {}
        return str(entry.get("prefix") or self.hive or "<hive>")

    def remember(self, key: str, compute: Callable[[], Any]) -> Any:
        if key not in self.memo:
            self.memo[key] = compute()
        return self.memo[key]


@dataclass(frozen=True)
class WhyReport:
    """The walk's result: the deciding verdict and every verdict reached on the way."""

    hive: str
    capability: str
    host: str
    verdict: Verdict
    walked: tuple[Verdict, ...]

    @property
    def allowed(self) -> bool:
        return self.verdict.status != REFUSED

    def as_dict(self) -> dict[str, Any]:
        return {
            "hive": self.hive,
            "capability": self.capability,
            "host": self.host,
            "allowed": self.allowed,
            "layer": self.verdict.layer,
            "code": self.verdict.code,
            "summary": self.verdict.summary,
            "command": self.verdict.command,
            "detail": dict(self.verdict.detail),
            "layers": [verdict.as_dict() for verdict in self.walked],
        }


_LAYERS: dict[str, Layer] = {}
_DISCOVERED = False


class DuplicateLayer(ValueError):
    """Two providers registered one layer name."""


def register_layer(layer: Layer, *, replace: bool = False) -> Layer:
    """Add `layer` to the walk. A name registers once unless `replace` (a test double)."""
    if layer.name in _LAYERS and not replace and _LAYERS[layer.name] is not layer:
        raise DuplicateLayer(f"hq why layer {layer.name!r} is already registered")
    _LAYERS[layer.name] = layer
    return layer


def unregister_layer(name: str) -> None:
    _LAYERS.pop(name, None)


def discover() -> None:
    """Import every ``hq_why_layer_*`` provider once and register its ``LAYERS``."""
    global _DISCOVERED
    if _DISCOVERED:
        return
    _DISCOVERED = True
    import importlib

    from . import __path__ as package_path

    for module in sorted(pkgutil.iter_modules(package_path), key=lambda m: m.name):
        if not module.name.startswith(PROVIDER_PREFIX):
            continue
        provider = importlib.import_module(f"{__package__}.{module.name}")
        for layer in getattr(provider, "LAYERS", ()):
            register_layer(layer)


def layers() -> tuple[Layer, ...]:
    """The registered layers in walk order."""
    discover()
    return tuple(sorted(_LAYERS.values(), key=lambda layer: (layer.order, layer.name)))


def _failed(layer: Layer, exc: Exception) -> Verdict:
    from . import config

    return Verdict(
        layer.name,
        REFUSED,
        "layer_error",
        f"the {layer.name} layer could not be evaluated ({type(exc).__name__}: {exc}); the "
        "guard fails closed on the same error",
        f"{config.BINARY_ALIAS} doctor",
        {"error_type": type(exc).__name__, "error": str(exc)},
    )


def walk(ctx: WhyContext) -> WhyReport:
    """Visit the layers in order and return the first refusing (or deciding) verdict."""
    walked: list[Verdict] = []
    deciding = None
    for layer in layers():
        try:
            verdict = layer.check(ctx)
        except Exception as exc:  # noqa: BLE001 — fail closed, named, like the guard
            verdict = _failed(layer, exc)
        walked.append(verdict)
        if verdict.status in (REFUSED, DECIDED):
            deciding = verdict
            break
    host = str(ctx.memo.get("host_id") or "")
    return WhyReport(ctx.label, ctx.capability, host, deciding or _allow(ctx), tuple(walked))


def _allow(ctx: WhyContext) -> Verdict:
    from . import config

    return Verdict(
        "",
        OK,
        "ok",
        f"no layer refuses {ctx.capability} on {ctx.label} from this host; re-run the write",
        f"{config.BINARY_ALIAS} host eligible --hive {ctx.label}",
    )


def render(report: WhyReport) -> str:
    """The human report: the deciding layer, its fix, then the layers walked."""
    verdict = report.verdict
    mark = "✗" if verdict.status == REFUSED else "✓"
    where = f" [{verdict.layer}]" if verdict.layer else ""
    lines = [
        f"{mark} {report.hive} · {report.capability}{where}: {verdict.summary}",
    ]
    if verdict.command:
        lines.append(f"  next: {verdict.command}")
    lines.append("")
    for step in report.walked:
        lines.append(f"  {step.status:<8} {step.layer:<12} {step.summary}")
    return "\n".join(lines)
