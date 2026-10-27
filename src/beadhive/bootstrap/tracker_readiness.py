"""The offline tier-2 readiness row of the ``tracker.adapter`` category (T2).

``bh hive ready`` and ``bh doctor`` show one INFORMATIONAL row per store that binds a tracker
adapter (tracker-adapter-plugin-adr.md section 4, "Validation rules and where they run"): rules
R6, R9 and R12 as information or a warning, and a ready row naming the bound repository and the
enabled operations. It is never a required check, so a frame's conformance cannot change because
of it (a new required check drops frames out of conformance), and it never fails a gate:

* a hive that binds no adapter yields NO row and costs nothing: this module's top level imports
  only the standard library, and the composition (discovery, plugin settings) is built lazily,
  once, only when some store binds an adapter;
* offline and read-only: no network, no ``gh`` run, no package import, no credential read;
* a plugin that is off (disabled, or not installed while off) is information, never a warning:
  an off-by-default plugin never nags. A binding problem is reported only once the plugin is
  on.

Every field is built from ids, enumerations and the hive triplet; the only free text is a
refusal message the composition root already builds from those, so no row can carry a token.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

#: The hive-ready check label (the generic per-store row).
READY_LABEL = "tracker adapter"


@dataclass(frozen=True)
class TrackerStoreRow:
    hive: str
    scope: str
    #: The bound plugin id, or ``None`` when the binding is malformed (the value is not echoed).
    adapter: str | None
    #: The tracker project locator derived from the hive identity (``owner/name``), if it resolves.
    project: str | None
    #: A ``TrackerState`` value, or ``invalid-binding`` / ``identity``.
    state: str
    #: A ``DiagnosticSeverity`` value: ``info``, ``warning`` or ``error``.
    severity: str
    #: A ``DiagnosticCode`` value.
    code: str
    #: The ADR rule id (``R6``, ``R9``, ``R10``, ``R11``, ``R12``), when the row is one.
    rule: str | None
    detail: str
    #: The plugin-level enabled operation names, in port order. Empty unless ready.
    operations: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "hive": self.hive,
            "scope": self.scope,
            "adapter": self.adapter,
            "project": self.project,
            "state": self.state,
            "severity": self.severity,
            "code": self.code,
            "rule": self.rule,
            "detail": self.detail,
            "operations": list(self.operations),
        }

    def check_state(self) -> str:
        """The ``hive ready`` glyph state. Informational rows are ``ok`` (ready), ``na`` (the
        plugin is disabled, never probed) or ``off`` (not installed while off); a problem is
        ``warn``. Never ``missing``: this row can never fail readiness."""
        if self.severity != "info":
            return "warn"
        return {"ready": "ok", "disabled": "na"}.get(self.state, "off")


def _bound_scopes(entry: Mapping[str, Any]) -> list[str]:
    """The scope of every store of ``entry`` that declares an ``adapter`` key. Pure
    dictionary reads, so the common unbound hive pays nothing."""
    stores = entry.get("stores") if isinstance(entry, Mapping) else None
    if not isinstance(stores, Mapping):
        return []
    return [
        str(scope)
        for scope, store in stores.items()
        if isinstance(store, Mapping) and store.get("adapter") is not None
    ]


def has_binding(hives: Sequence[Mapping[str, Any]]) -> bool:
    return any(_bound_scopes(entry) for entry in hives if isinstance(entry, Mapping))


def tracker_store_rows(
    hives: Sequence[Mapping[str, Any]],
    *,
    plugins: Mapping[str, object] | None,
    plugin_kernel: Mapping[str, object] | None,
    sources: Sequence[Any] | None = None,
) -> tuple[TrackerStoreRow, ...]:
    """One row per bound store across ``hives`` (``managed_repos`` entries); ``()`` when no
    store binds an adapter."""
    candidates = [
        (entry, scope)
        for entry in hives
        if isinstance(entry, Mapping)
        for scope in _bound_scopes(entry)
    ]
    if not candidates:
        return ()

    from .tracker import compose_tracker_category

    composition = compose_tracker_category(
        plugins=plugins, plugin_kernel=plugin_kernel, sources=sources
    )
    return tuple(_classify(entry, scope, composition) for entry, scope in candidates)


def _row(entry, scope, adapter, state, severity, code, rule, detail, *, project=None, ops=()):
    from .tracker import hive_label

    return TrackerStoreRow(
        hive_label(entry),
        scope,
        adapter,
        project,
        state,
        severity.value,
        code.value,
        rule,
        detail,
        tuple(ops),
    )


def _classify(entry: Mapping[str, Any], scope: str, composition: Any) -> TrackerStoreRow:
    """The row for one bound store, in the ADR's order: binding, installed, valid, conflict or
    owner, disabled, then identity. An off plugin is information whatever else is wrong."""
    from ..kernel.plugins import DiagnosticCode as Code
    from ..kernel.plugins import DiagnosticSeverity as Severity
    from ..modules.tracker.contracts import WRITE_OPERATIONS, Operation
    from .tracker import (
        BUILTIN_TRACKER_PROVIDERS,
        TrackerBindingError,
        bound_adapter_id,
        derive_project,
    )
    from .tracker_status import TrackerState, _state

    try:
        adapter = bound_adapter_id(entry, scope)
    except TrackerBindingError as exc:
        detail = f"{exc.message} — run `bh config validate`"
        return _row(entry, scope, None, "invalid-binding", Severity.WARNING, exc.code, "R2", detail)
    assert adapter is not None  # _bound_scopes only yields scopes that declare one
    state = _state(adapter, composition, 1)

    def row(severity, code, rule, detail, **extra):
        return _row(entry, scope, adapter, state.value, severity, code, rule, detail, **extra)

    if state is TrackerState.NOT_INSTALLED:
        settings = composition.settings.get(adapter)
        switched_on = (
            bool(settings and settings.own_enabled)
            and composition.policy.enabled.get(adapter, True) is not False
        )
        hint = next(
            (p.install_hint for p in BUILTIN_TRACKER_PROVIDERS if p.plugin_id == adapter), None
        )
        how = f"install `{hint}`" if hint else "no discovered plugin has this id"
        note = "" if switched_on else " (the plugin is off, nothing is needed)"
        return row(
            Severity.ERROR if switched_on else Severity.INFO,
            Code.TRACKER_PACKAGE,
            "R9",
            f"{adapter}: not installed — {how}{note}",
        )
    if state is TrackerState.INVALID_CONFIG:
        return row(
            Severity.ERROR,
            Code.TRACKER_CONFIG,
            None,
            f"{adapter}: plugins.{adapter} or plugin_kernel is invalid and is retained "
            "inactive — run `bh config validate`",
        )
    if state is TrackerState.DISABLED:
        return row(
            Severity.INFO,
            Code.DISABLED_PLUGIN,
            "R6",
            f"{adapter}: disabled — plugins.{adapter}.enabled is not true on this host "
            "(a binding never enables a plugin)",
        )
    if state is TrackerState.CONFLICT:
        owner = composition.owner
        if owner is None:
            detail = (
                f"{adapter}: two enabled plugins provide tracker.adapter@1 — select one "
                "with plugin_kernel.capability_owners"
            )
            return row(Severity.ERROR, Code.DUPLICATE_CAPABILITY, "R11", detail)
        detail = f"{adapter}: {owner!r} is the selected owner of tracker.adapter@1"
        return row(Severity.ERROR, Code.TRACKER_CONFIG, "R10", detail)
    try:
        project = derive_project(entry, adapter)
    except TrackerBindingError as exc:
        return _row(
            entry, scope, adapter, "identity", Severity.WARNING, exc.code, "R12", exc.message
        )
    enabled = composition.enabled_operations(adapter)
    ops = [op.value for op in Operation if op in enabled]
    grant = "; each write needs an operator-approved grant" if enabled & WRITE_OPERATIONS else ""
    return row(
        Severity.INFO,
        Code.TRACKER_CONFIG,
        None,
        f"{adapter}: ready — {project.locator}; operations: "
        f"{', '.join(ops) if ops else 'none enabled'}{grant}",
        project=project.locator,
        ops=ops,
    )


__all__ = ["READY_LABEL", "TrackerStoreRow", "has_binding", "tracker_store_rows"]
