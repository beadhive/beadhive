"""``bh plugin tracker-github status`` and ``doctor``: the read-only GitHub tracker readout.

A thin Typer projection (mounted lazily from ``PLUGIN_CLI_RUNTIME_CATALOG``, declared by the
manifest's ``presentation.cli``) over the generic ``tracker.adapter`` renderer in
``beadhive.bootstrap.tracker_status``. It lives in root because a plugin package may not import
``beadhive.bootstrap``, and it never imports the package. Both verbs are read-only and, by
default, offline: they show the plugin's state, the credential SOURCE names that are set or
unset (never a value), whether ``gh`` is on the path, and the per-operation capability tri-state as
``not-probed``.

Identity (``whoami``) and the live capability probe need the network, so they run only on the
explicit ``doctor --probe``: one bound store's adapter is built from the configured credential
chain and asked for ``probe_capabilities()``, which makes GET requests only. No verb here writes
to GitHub, spends a grant or enables anything; a disabled plugin renders ``disabled`` and a
probe on it is refused with the binding rule's message, before any request or credential read.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import typer

PLUGIN_ID = "tracker-github"

__all__ = ["app"]

app = typer.Typer(no_args_is_help=True, help="GitHub tracker adapter: read-only status and doctor.")

_JSON = typer.Option(False, "--json", help="emit the machine-readable document.")
_PROBE = typer.Option(
    False,
    "--probe",
    help="ALSO make the adapter's GET-only identity and capability probe (needs a credential and "
    "the network; off by default).",
)
_HIVE = typer.Option(
    None, "--hive", help="with --probe: the bound hive to probe, as provider/org/repo."
)


def _load_config() -> Mapping[str, Any]:
    from .config_consumer_ports import plugin_settings as config

    try:
        return config.load()
    except FileNotFoundError:
        return {}


def _build_adapters(cfg: Mapping[str, Any]):
    """The ``TrackerAdapters`` a probe uses: the configured credential chain, nothing cached.

    The one seam tests replace, so no test reads a real token or spawns ``gh auth token``."""
    from . import run
    from .bootstrap.tracker import TrackerAdapters, plugin_settings

    values = plugin_settings(PLUGIN_ID, cfg.get("plugins")).values
    names = tuple(((values.get("credential") or {}).get("sources")) or ())
    return TrackerAdapters(
        plugins=cfg.get("plugins"),
        plugin_kernel=cfg.get("plugin_kernel"),
        token_source=run.github_token_source(names=names),
    )


def _probe(cfg: Mapping[str, Any], hives: list, hive: str | None):
    """``(report, error)`` for one bound store; exactly one is ``None``."""
    from .bootstrap import tracker as root
    from .bootstrap.tracker import TrackerAdapters

    bound = []
    for entry in hives:
        try:
            if root.bound_adapter_id(entry, "public") == PLUGIN_ID:
                bound.append(entry)
        except (root.TrackerBindingError, TypeError):
            continue
    if hive is not None:
        bound = [entry for entry in bound if root.hive_label(entry) == hive]
    if not bound:
        which = f"hive {hive!r}" if hive else "any hive"
        return None, f"--probe: stores.public.adapter binds {PLUGIN_ID} on {which}: none found"
    if len(bound) > 1:
        names = ", ".join(sorted(root.hive_label(entry) for entry in bound))
        return None, f"--probe: more than one hive binds {PLUGIN_ID} ({names}); pass --hive"
    adapters: TrackerAdapters = _build_adapters(cfg)
    try:
        # No requested operations: the probe is identity and a GET-only capability read, and a
        # disabled plugin is refused here with the binding rule's message, before any request.
        return adapters.for_store(bound[0]).port.probe_capabilities(), None
    except root.TrackerBindingError as exc:
        return None, exc.message
    except Exception as exc:  # noqa: BLE001 - adapter refusals are typed, scrubbed wording
        return None, f"{type(exc).__name__}: {exc}"


def _document(probe: bool, hive: str | None) -> dict[str, Any]:
    from . import registry
    from .bootstrap import tracker as root
    from .bootstrap.tracker_status import tracker_status

    cfg = _load_config()
    hives = registry.hives(cfg)
    composition = root.compose_tracker_category(
        plugins=cfg.get("plugins"), plugin_kernel=cfg.get("plugin_kernel")
    )
    report, error = _probe(cfg, hives, hive) if probe else (None, None)
    return tracker_status(
        PLUGIN_ID, composition=composition, hives=hives, report=report, error=error
    )


@app.command("status")
def status(as_json: bool = _JSON) -> None:
    """Show the plugin's state, credential source presence and capability tri-state (offline)."""
    from .bootstrap.tracker_status import render_json, render_text

    doc = _document(False, None)
    typer.echo(render_json(doc) if as_json else render_text(doc))


@app.command("doctor")
def doctor(as_json: bool = _JSON, probe: bool = _PROBE, hive: str | None = _HIVE) -> None:
    """Status plus diagnostics; exits 1 only on an error while the plugin is enabled."""
    from .bootstrap.tracker_status import (
        doctor_exit_code,
        render_doctor_json,
        render_doctor_text,
        tracker_doctor,
    )

    doc = _document(probe, hive)
    rows = tracker_doctor(doc)
    typer.echo(render_doctor_json(doc, rows) if as_json else render_doctor_text(doc, rows))
    code = doctor_exit_code(doc, rows)
    if code:
        raise typer.Exit(code)
