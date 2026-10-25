"""Prototype of the generic status and doctor rendering (root-owned, port-driven).

Everything here is computed from the NEUTRAL port (``describe``, ``whoami``,
``probe_capabilities``), the composed config and credential PRESENCE, so a second tracker plugin
gets the same output for free. Every free-text value passes through ``scrub``; no field is ever
built from a token value, only from provenance strings and booleans.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from compose_proto import TrackerComposition
from credentials_proto import scrub
from tracker_contracts import CapabilityReport, Operation

SCHEMA = "tracker-adapter-status/1"
WRITE_OPS = (Operation.COMMENT_CREATE, Operation.LABELS_CHANGE)


def credential_facts(
    declared: Sequence[tuple[str, str]], environ: Mapping[str, str], gh_on_path: bool
) -> dict:
    """Presence only. ``declared`` is the manifest's (kind, name) list; the ``gh`` leg is
    reported as available/not found and is NOT consulted (running it is a doctor-only act)."""
    legs = [
        {"source": f"env:{name}", "present": bool((environ.get(name) or "").strip())}
        for kind, name in declared
        if kind == "environment-variable"
    ]
    return {"legs": legs, "gh": "found" if gh_on_path else "not found"}


def build_status(
    composition: TrackerComposition,
    *,
    plugin_id: str,
    settings: Mapping,
    credential: dict,
    report: CapabilityReport | None = None,
    secrets: Sequence[str] = (),
    error: str | None = None,
) -> dict:
    ops = []
    for op in Operation:
        state = report.states.get(op) if report else None
        enabled = (
            state.enabled if state else (settings.get("enabled") is True and _flag(settings, op))
        )
        ops.append(
            {
                "operation": op.value,
                "kind": "write" if op in WRITE_OPS else "read",
                "enabled": enabled,
                "probe": state.probe.value if state else "not-probed",
                "usable": state.usable if state else None,
                "reason": scrub(state.reason, secrets) if state else "",
                "gate": "operator-approved single-use grant per use" if op in WRITE_OPS else None,
            }
        )
    actor = report.actor if report else None
    return {
        "schema": SCHEMA,
        "plugin": plugin_id,
        "state": composition.state,
        "capability": "tracker.adapter@1",
        "owner": composition.owner,
        "repo": settings.get("repo"),
        "credential": {
            **credential,
            "resolved": scrub(report.credential_source or "", secrets) if report else None,
        },
        "identity": (
            None
            if report is None
            else {
                "id": actor.key.id,
                "kind": actor.key.kind,
                "display": scrub(actor.display, secrets),
            }
            if actor
            else {"anonymous": True}
        ),
        "operations": ops,
        "error": scrub(error, secrets) if error else None,
    }


def _flag(settings: Mapping, op: Operation) -> bool:
    group, name = {
        Operation.ISSUES_READ: ("read", "issues"),
        Operation.COMMENTS_READ: ("read", "comments"),
        Operation.COMMENT_CREATE: ("write", "comment"),
        Operation.LABELS_CHANGE: ("write", "labels"),
    }[op]
    return settings.get(group, {}).get(name, {}).get("enabled") is True


def render_json(doc: dict) -> str:
    return json.dumps(doc, indent=2, sort_keys=True)


def render_text(doc: dict) -> str:
    lines = [f"{doc['plugin']}  {doc['state']}"]
    lines.append(f"  capability   {doc['capability']}  (owner: {doc['owner'] or 'none selected'})")
    lines.append(f"  repo         {doc['repo'] or '(unset)'}")
    cred = doc["credential"]
    legs = ", ".join(
        f"{leg['source']} {'set' if leg['present'] else 'unset'}" for leg in cred["legs"]
    )
    lines.append(f"  credential   {legs}; gh {cred['gh']}")
    if cred.get("resolved"):
        lines.append(f"               resolved via {cred['resolved']}")
    ident = doc["identity"]
    if ident is None:
        lines.append("  identity     not probed (bh plugin tracker-github doctor --probe)")
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
