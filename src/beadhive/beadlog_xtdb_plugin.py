"""``bh plugin beadlog-xtdb status`` and ``doctor``: the XTDB BeadLog store readout.

A thin Typer projection (mounted lazily from ``PLUGIN_CLI_RUNTIME_CATALOG``, declared by the
manifest's ``presentation.cli``). It lives in root because it composes root-owned pieces (the
config loader and the fnox secret broker behind the password port), and it never imports the
plugin package or psycopg at import time: ``status`` is configuration only and works with the
package absent; ``doctor`` imports the package's checks (``beadhive_beadlog_xtdb.doctor``) only
when the package is installed.

Neither verb starts, stops or writes to the node, and neither prints the password: ``status``
shows only whether a fnox entry is configured (its profile and key names), and ``doctor``
resolves it only to connect, never to render it. While the plugin is disabled (the default)
``doctor`` checks the configuration and the data directory only (no lookup, connection or
request) and always exits 0; enabled, it exits 1 when any check is an error.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from importlib import import_module, util
from typing import Any

import typer

PLUGIN_ID = "beadlog-xtdb"
PACKAGE = "beadhive_beadlog_xtdb"
INSTALL_HINT = "beadhive[xtdb]"
SCHEMA = "beadlog-store-status/1"

__all__ = ["app"]

app = typer.Typer(
    no_args_is_help=True, help="XTDB BeadLog store (beadlog.store): read-only status and doctor."
)

_JSON = typer.Option(False, "--json", help="emit the machine-readable document.")


def _load_config() -> Mapping[str, Any]:
    from .config_consumer_ports import plugin_settings as config

    try:
        return config.load()
    except FileNotFoundError:
        return {}


def package_installed() -> bool:
    """Whether the plugin package is importable (found, not imported)."""
    return util.find_spec(PACKAGE) is not None


def _password_source(values: Mapping[str, Any]):
    """The store's password port (fnox); the one seam tests replace."""
    from .beadlog_store_credentials import fnox_password_source

    return fnox_password_source(values)


def _document(cfg: Mapping[str, Any]) -> dict[str, Any]:
    from .bootstrap.beadlog_store import compose_store_category, credential_configured

    composition = compose_store_category(
        plugins=cfg.get("plugins"), plugin_kernel=cfg.get("plugin_kernel")
    )
    settings = composition.settings[PLUGIN_ID]
    enabled = composition.plugin_enabled(PLUGIN_ID)
    installed = package_installed()
    if settings.own_enabled and not settings.valid:
        state = "invalid-config"
    elif not enabled:
        state = "disabled"
    elif not installed:
        state = "not-installed"
    else:
        state = "ready"
    values = dict(settings.values)
    credential = values.get("credential") or {}
    return {
        "schema": SCHEMA,
        "plugin": PLUGIN_ID,
        "store": "xtdb",
        "state": state,
        "enabled": enabled,
        "installed": installed,
        "valid": settings.valid,
        "dsn": values.get("dsn"),
        "credential": {
            "configured": credential_configured(values),
            "profile": credential.get("profile") or None,
            "key": credential.get("key") or None,
        },
        "image": values.get("image"),
        "data_dir": values.get("data_dir") or None,
        "health_url": values.get("health_url"),
        "values": values,
    }


def render_status_text(doc: Mapping[str, Any]) -> str:
    credential = doc["credential"]
    lines = [f"{doc['plugin']}  {doc['state']}"]
    package = "installed" if doc["installed"] else f"not installed (install `{INSTALL_HINT}`)"
    lines.append(f"  package      {package}")
    lines.append("  store        bh backfill apply|export --store xtdb")
    if not doc["valid"]:
        lines.append("  config       invalid — run `bh config validate`")
        return "\n".join(lines)
    lines.append(f"  dsn          {doc['dsn']}")
    if credential["configured"]:
        lines.append(
            f"  credential   fnox profile={credential['profile']} key={credential['key']} "
            "(value not shown)"
        )
    else:
        lines.append("  credential   not configured (no password)")
    lines.append(f"  image        {doc['image']}")
    lines.append(f"  data_dir     {doc['data_dir'] or 'unset'}")
    lines.append(f"  health_url   {doc['health_url']}")
    if not doc["enabled"]:
        lines.append(
            f"  enable       bh config set plugins.{PLUGIN_ID}.enabled true "
            "(bh never starts the node)"
        )
    return "\n".join(lines)


def _public(doc: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in doc.items() if key != "values"}


@app.command("status")
def status(as_json: bool = _JSON) -> None:
    """Show the plugin's state and configuration (offline; the password is never shown)."""
    doc = _document(_load_config())
    if as_json:
        typer.echo(json.dumps(_public(doc), indent=2, sort_keys=True))
    else:
        typer.echo(render_status_text(doc))


def doctor_rows(doc: Mapping[str, Any]) -> list[dict[str, str]]:
    """Every doctor row for ``doc``: plugin-level rows first, then the package's checks."""
    rows: list[dict[str, str]] = []
    if not doc["enabled"]:
        rows.append(
            {
                "check": "disabled-plugin",
                "severity": "info",
                "detail": f"{PLUGIN_ID}: disabled (plugins.{PLUGIN_ID}.enabled is not true); "
                "network checks skipped",
                "remedy": f"bh config set plugins.{PLUGIN_ID}.enabled true",
            }
        )
    if not doc["valid"]:
        rows.append(
            {
                "check": "config",
                "severity": "error",
                "detail": f"plugins.{PLUGIN_ID} is invalid (retained inactive)",
                "remedy": "run `bh config validate`",
            }
        )
        return rows
    if not doc["installed"]:
        rows.append(
            {
                "check": "package",
                "severity": "error" if doc["enabled"] else "info",
                "detail": f"the {PLUGIN_ID} plugin package is not installed",
                "remedy": f"install `{INSTALL_HINT}` (`uv sync --extra xtdb` in a checkout)",
            }
        )
        return rows
    checks = import_module(f"{PACKAGE}.doctor")
    plugin_settings = import_module(f"{PACKAGE}.settings")
    from .bootstrap.beadlog_store import credential_configured

    values = doc["values"]
    results = checks.run_checks(
        plugin_settings.XtdbSettings.from_mapping(values),
        credential_configured=credential_configured(values),
        password_source=_password_source(values),
        network=bool(doc["enabled"]),
    )
    rows.extend(row.to_json() for row in results)
    return rows


def render_doctor_text(doc: Mapping[str, Any], rows: list[dict[str, str]]) -> str:
    lines = [f"{doc['plugin']}  {doc['state']}"]
    for row in rows:
        lines.append(f"{row['severity']:<8} {row['check']}: {row['detail']}")
        if row["remedy"] and row["severity"] != "ok":
            lines.append(f"         -> {row['remedy']}")
    return "\n".join(lines)


def doctor_exit_code(doc: Mapping[str, Any], rows: list[dict[str, str]]) -> int:
    """1 only when the plugin is enabled and a row is an error."""
    return int(bool(doc["enabled"]) and any(row["severity"] == "error" for row in rows))


@app.command("doctor")
def doctor(as_json: bool = _JSON) -> None:
    """Check the node and this host; exits 1 only on an error while the plugin is enabled."""
    doc = _document(_load_config())
    rows = doctor_rows(doc)
    if as_json:
        typer.echo(json.dumps({**_public(doc), "checks": rows}, indent=2, sort_keys=True))
    else:
        typer.echo(render_doctor_text(doc, rows))
    code = doctor_exit_code(doc, rows)
    if code:
        raise typer.Exit(code)
