"""The generic status and doctor renderer of the ``tracker.adapter`` category.

One root function over the NEUTRAL port, so the output is identical for any future tracker
plugin (tracker-adapter-plugin-adr.md section 4, "Status and doctor"). Everything is computed
from the category composition (manifest metadata, folded enablement), credential PRESENCE, and,
only when a caller probed, the port's own ``whoami``/``probe_capabilities`` answer:

* ``status`` is offline: no network, no ``gh`` run, no package import;
* ``doctor`` is ``status`` plus :class:`~beadhive_plugins.contracts.PluginDiagnostic` rows
  under the four ``tracker-*`` codes; it fails (exit 1) only on an error while the plugin is
  enabled, so an off-by-default plugin never nags and never fails a gate.

**What is never rendered**: a token or any part of one, a header, a raw upstream body or a grant
secret. Every field is built from enumerations, booleans, provenance strings, public ids and
plugin metadata; every free-text value (a probe ``reason``, a display handle, an error) passes
through :func:`~beadhive_plugins.credentials.scrub` with the declared credential values as
exact secrets plus the token-shape pattern. Credential values are read only to test presence and
to compare two legs (the state-only "both set and differ" advisory), and are never stored.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable, Mapping, Sequence
from enum import StrEnum
from typing import Any

from beadhive_plugins.credentials import scrub

from ..kernel.plugins import DiagnosticCode, DiagnosticSeverity, PluginDiagnostic
from ..modules.tracker.contracts import WRITE_OPERATIONS, CapabilityReport, Operation
from .tracker import (
    BUILTIN_TRACKER_PROVIDERS,
    TRACKER_ADAPTER,
    TrackerAdapterProvider,
    TrackerBindingError,
    TrackerComposition,
    bound_adapter_id,
)

#: The ``--json`` envelope's schema id.
STATUS_SCHEMA = "tracker-adapter-status/1"

#: The note every write row carries. A constant, never conditional on configuration, so no
#: output can suggest a bypass.
WRITE_GATE = "operator-approved single-use grant per use"

_ENV_KIND = "environment-variable"


class TrackerState(StrEnum):
    NOT_INSTALLED = "not-installed"
    DISABLED = "disabled"
    INVALID_CONFIG = "invalid-config"
    CONFLICT = "conflict"
    UNCONFIGURED = "unconfigured"  # enabled and selected, but no store binds this adapter
    READY = "ready"


def _bound_stores(plugin_id: str, hives: Sequence[Mapping[str, Any]]) -> int:
    count = 0
    for entry in hives:
        try:
            if bound_adapter_id(entry, "public") == plugin_id:
                count += 1
        except (TrackerBindingError, TypeError):
            continue
    return count


def _state(plugin_id: str, composition: TrackerComposition, bound: int) -> TrackerState:
    if composition.manifest(plugin_id) is None:
        return TrackerState.NOT_INSTALLED
    settings = composition.settings[plugin_id]
    if composition.policy_error is not None or (settings.own_enabled and not settings.valid):
        return TrackerState.INVALID_CONFIG
    if not composition.plugin_enabled(plugin_id):
        return TrackerState.DISABLED
    if composition.conflict or composition.owner != plugin_id:
        return TrackerState.CONFLICT
    return TrackerState.READY if bound else TrackerState.UNCONFIGURED


def tracker_status(
    plugin_id: str,
    *,
    composition: TrackerComposition,
    hives: Sequence[Mapping[str, Any]] = (),
    environ: Mapping[str, str] | None = None,
    which: Callable[[str], object] | None = None,
    report: CapabilityReport | None = None,
    error: str | None = None,
    providers: Sequence[TrackerAdapterProvider] = BUILTIN_TRACKER_PROVIDERS,
) -> dict[str, Any]:
    """The status document for one tracker plugin. Offline unless the caller passes ``report``
    (``doctor --probe``: the port's ``probe_capabilities()`` answer).

    ``hives`` are the ``managed_repos`` entries, to tell ``unconfigured`` (no store binds this
    adapter) from ``ready``. ``environ`` defaults to the process environment and ``which`` to
    :func:`shutil.which`; neither runs anything.
    """
    environ = os.environ if environ is None else environ
    which = shutil.which if which is None else which
    discovered = composition.manifest(plugin_id)
    manifest = discovered.manifest if discovered is not None else None
    settings = composition.settings.get(plugin_id)

    names = sorted(
        {
            source.name
            for credential in (manifest.credentials if manifest else ())
            for source in credential.sources
            if source.kind == _ENV_KIND
        }
    )
    present = {name: (environ.get(name) or "").strip() for name in names}
    # The set credential values: exact secrets for scrub and the "both set and differ" test.
    # Used here only, never stored in the document.
    values = {value for value in present.values() if value}
    secrets = tuple(values)

    def clean(text: str | None) -> str | None:
        return None if text is None else scrub(text, secrets)

    bound = _bound_stores(plugin_id, hives)
    state = _state(plugin_id, composition, bound)
    enabled_ops = composition.enabled_operations(plugin_id)
    operations = []
    for op in Operation:
        probed = report.states.get(op) if report is not None else None
        operations.append(
            {
                "operation": op.value,
                "kind": "write" if op in WRITE_OPERATIONS else "read",
                "enabled": op in enabled_ops,
                "probe": probed.probe.value if probed else "not-probed",
                "usable": (probed.usable and op in enabled_ops) if probed else None,
                "reason": clean(probed.reason) if probed else "",
                "gate": WRITE_GATE if op in WRITE_OPERATIONS else None,
            }
        )
    if report is None:
        identity: dict[str, Any] | None = None
    elif report.actor is None:
        identity = {"anonymous": True}
    else:
        identity = {
            "id": clean(report.actor.key.id),
            "kind": report.actor.key.kind.value,
            "display": clean(report.actor.display),
        }
    row = next((p for p in providers if p.plugin_id == plugin_id), None)
    return {
        "schema": STATUS_SCHEMA,
        "plugin": plugin_id,
        "state": state.value,
        "capability": TRACKER_ADAPTER.render(),
        "owner": composition.owner,
        "enabled": bool(settings and settings.own_enabled)
        and composition.policy.enabled.get(plugin_id, True) is not False,
        "install": row.install_hint if row else None,
        "bound_stores": bound,
        "credential": {
            "legs": [{"source": f"env:{name}", "present": bool(present[name])} for name in names],
            "executables": [
                {"name": item.name, "found": which(item.name) is not None}
                for item in (manifest.executables if manifest else ())
            ],
            # State only: two declared legs are both set and their values differ.
            "disagree": len(values) > 1,
            "resolved": clean(report.credential_source) if report is not None else None,
        },
        "identity": identity,
        "operations": operations,
        "error": clean(error),
    }


def render_json(doc: Mapping[str, Any]) -> str:
    return json.dumps(doc, indent=2, sort_keys=True)


def render_text(doc: Mapping[str, Any]) -> str:
    plugin = doc["plugin"]
    lines = [f"{plugin}  {doc['state']}"]
    owner = doc["owner"] or "none selected"
    lines.append(f"  capability   {doc['capability']}  (owner: {owner})")
    lines.append(f"  stores       {doc['bound_stores']} bound")
    cred = doc["credential"]
    legs = [f"{leg['source']} {'set' if leg['present'] else 'unset'}" for leg in cred["legs"]]
    tools = [f"{x['name']} {'found' if x['found'] else 'not found'}" for x in cred["executables"]]
    lines.append(
        "  credential   "
        + ("; ".join(part for part in (", ".join(legs), *tools) if part) or "none")
    )
    if cred["disagree"]:
        names = " and ".join(
            leg["source"].removeprefix("env:") for leg in cred["legs"] if leg["present"]
        )
        lines.append(f"               warning: {names} are both set and differ")
    if cred.get("resolved"):
        lines.append(f"               resolved via {cred['resolved']}")
    ident = doc["identity"]
    if ident is None:
        lines.append(f"  identity     not probed (bh plugin {plugin} doctor --probe)")
    elif ident.get("anonymous"):
        lines.append("  identity     anonymous (no credential)")
    else:
        lines.append(
            f"  identity     id={ident['id']} kind={ident['kind']} display={ident['display']}"
        )
    for index, op in enumerate(doc["operations"]):
        head = "  operations   " if index == 0 else "               "
        usable = {True: " usable", False: " not usable", None: ""}[op["usable"]]
        flag = "enabled" if op["enabled"] else "disabled"
        cell = f"{op['operation']:<15} {flag:<9} probe={op['probe']:<10}{usable}".rstrip()
        note = f"  [{op['gate']}]" if op["gate"] else ""
        lines.append(f"{head}{cell}{note}")
    if doc["error"]:
        lines.append(f"  error        {doc['error']}")
    return "\n".join(lines)


def _diagnostic(doc: Mapping[str, Any], code, severity, detail: str) -> PluginDiagnostic:
    return PluginDiagnostic(
        code, severity, detail, plugin_id=doc["plugin"], capability=TRACKER_ADAPTER
    )


def tracker_doctor(doc: Mapping[str, Any]) -> tuple[PluginDiagnostic, ...]:
    """The doctor rows for a status document. Built from the document alone, which holds no
    credential value, so a row cannot leak one."""
    plugin, state, on = doc["plugin"], TrackerState(doc["state"]), doc["enabled"]
    info, warning, error = (
        DiagnosticSeverity.INFO,
        DiagnosticSeverity.WARNING,
        DiagnosticSeverity.ERROR,
    )
    rows: list[PluginDiagnostic] = []
    if state is TrackerState.NOT_INSTALLED:
        hint = f" — install `{doc['install']}`" if doc["install"] else ""
        rows.append(
            _diagnostic(
                doc,
                DiagnosticCode.TRACKER_PACKAGE,
                error if on else info,
                f"{plugin}: no discovered plugin has this id{hint}",
            )
        )
        return tuple(rows)
    if state is TrackerState.DISABLED:
        rows.append(
            _diagnostic(
                doc,
                DiagnosticCode.DISABLED_PLUGIN,
                info,
                f"{plugin}: disabled — plugins.{plugin}.enabled is not true on this host "
                "(a binding never enables a plugin)",
            )
        )
        return tuple(rows)
    if state is TrackerState.INVALID_CONFIG:
        rows.append(
            _diagnostic(
                doc,
                DiagnosticCode.TRACKER_CONFIG,
                error,
                f"{plugin}: plugins.{plugin} or plugin_kernel is invalid and is retained "
                "inactive — run `bh config validate`",
            )
        )
        return tuple(rows)
    if state is TrackerState.CONFLICT:
        owner = doc["owner"]
        detail = (
            f"{plugin}: {owner!r} is the selected owner of {doc['capability']}"
            if owner
            else f"{plugin}: two enabled plugins provide {doc['capability']} — select one "
            "with plugin_kernel.capability_owners"
        )
        rows.append(_diagnostic(doc, DiagnosticCode.DUPLICATE_CAPABILITY, error, detail))
        return tuple(rows)
    if state is TrackerState.UNCONFIGURED:
        rows.append(
            _diagnostic(
                doc,
                DiagnosticCode.TRACKER_CONFIG,
                warning,
                f"{plugin}: enabled, but no store binds this adapter (inert) — declare "
                f"`stores.public.adapter: {plugin}` on a hive to opt in",
            )
        )
    ops = {row["operation"]: row for row in doc["operations"]}
    reads_on = any(r["enabled"] for r in ops.values() if r["kind"] == "read")
    writes_on = [r["operation"] for r in ops.values() if r["kind"] == "write" and r["enabled"]]
    cred = doc["credential"]
    reachable = any(leg["present"] for leg in cred["legs"]) or any(
        x["found"] for x in cred["executables"]
    )
    if not reachable and writes_on:
        rows.append(
            _diagnostic(
                doc,
                DiagnosticCode.TRACKER_CREDENTIAL,
                error,
                f"{plugin}: a write is enabled but no credential source is available",
            )
        )
    elif not reachable and reads_on:
        rows.append(
            _diagnostic(
                doc,
                DiagnosticCode.TRACKER_CREDENTIAL,
                warning,
                f"{plugin}: reads are enabled with no credential source (anonymous, rate-limited)",
            )
        )
    if cred["disagree"]:
        names = " and ".join(
            leg["source"].removeprefix("env:") for leg in cred["legs"] if leg["present"]
        )
        rows.append(
            _diagnostic(
                doc,
                DiagnosticCode.TRACKER_CREDENTIAL,
                warning,
                f"{plugin}: {names} are both set and differ; the first in precedence is used",
            )
        )
    for name, row in ops.items():
        if row["enabled"] and row["probe"] == "no":
            reason = f" ({row['reason']})" if row["reason"] else ""
            rows.append(
                _diagnostic(
                    doc,
                    DiagnosticCode.TRACKER_CAPABILITY,
                    warning,
                    f"{plugin}: {name} is enabled but probes no{reason}",
                )
            )
    for name in writes_on:
        rows.append(
            _diagnostic(
                doc,
                DiagnosticCode.TRACKER_CAPABILITY,
                info,
                f"{plugin}: {name} is enabled; each write still needs an operator-approved "
                "single-use grant",
            )
        )
    return tuple(rows)


def doctor_exit_code(doc: Mapping[str, Any], rows: Sequence[PluginDiagnostic]) -> int:
    """Non-zero only on an error while the plugin is enabled."""
    failing = any(row.severity is DiagnosticSeverity.ERROR for row in rows)
    return 1 if failing and doc["enabled"] else 0


def render_doctor_text(doc: Mapping[str, Any], rows: Sequence[PluginDiagnostic]) -> str:
    lines = [render_text(doc)]
    lines.extend(f"  {row.severity.value:<8} {row.code.value}: {row.detail}" for row in rows)
    return "\n".join(lines)


def render_doctor_json(doc: Mapping[str, Any], rows: Sequence[PluginDiagnostic]) -> str:
    payload = {
        **doc,
        "diagnostics": [
            {"code": row.code.value, "severity": row.severity.value, "detail": row.detail}
            for row in rows
        ],
    }
    return render_json(payload)


__all__ = [
    "STATUS_SCHEMA",
    "WRITE_GATE",
    "TrackerState",
    "doctor_exit_code",
    "render_doctor_json",
    "render_doctor_text",
    "render_json",
    "render_text",
    "tracker_doctor",
    "tracker_status",
]
