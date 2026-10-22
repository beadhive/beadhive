"""``bh hq release-catalog`` — import, verify and revoke signed release catalog entries.

HIDDEN while frame release ranges are dormant (bh-vfrem): documented in ``docs/HQ.md``. One
command with an action word (like ``bh hq placement``): ``import VERSION``, ``verify
[VERSION|--all]`` and ``revoke VERSION --reason``. Every action is bound by the separately
credentialed ``hq.sql.release_catalog`` binding (INSERT on ``hq_release_catalog`` only) in the
operator settings file named by ``--operator-settings`` or ``$BH_HQ_OPERATOR_SETTINGS``.
Verification needs the optional extra ``beadhive[release-verify]``; the rest of ``bh`` imports
without it.

``import`` gets the bundle from GitHub's attestation API, keyed by the wheel's sha256, and the
wheel from PyPI, unless ``--bundle`` / ``--wheel`` name local files (air-gapped imports).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated

import typer

from . import hq_operator_settings, hq_release_catalog

__all__ = ["release_catalog_cmd"]

ACTIONS = ("import", "verify", "revoke")


def _store(operator_settings):
    path = operator_settings or os.environ.get(hq_operator_settings.ENV) or None
    return hq_operator_settings.release_catalog_store(path)


def _verifier():
    from .hq_release_catalog_sigstore import SigstoreBundleVerifier

    return SigstoreBundleVerifier()


def _remote():
    import httpx

    return hq_release_catalog.HttpSource(httpx.Client(timeout=30))


def release_catalog_cmd(
    action: Annotated[str, typer.Argument(help="import, verify or revoke")],
    version: Annotated[str, typer.Argument(metavar="[VERSION]", help="PEP 440 version")] = "",
    reason: Annotated[
        str, typer.Option("--reason", help="revoke: why (kept in the tombstone)")
    ] = "",
    all_: Annotated[bool, typer.Option("--all", help="verify: every active entry")] = False,
    bundle: Annotated[
        Path | None,
        typer.Option("--bundle", help="import: local bundle (default: attestation API)"),
    ] = None,
    wheel: Annotated[
        Path | None, typer.Option("--wheel", help="import: local wheel (default: PyPI)")
    ] = None,
    trusted_root: Annotated[
        Path | None,
        typer.Option(
            "--trusted-root",
            help="pinned Sigstore trusted_root.json: verify offline (default: refresh TUF online)",
        ),
    ] = None,
    operator_settings: Annotated[
        str | None,
        typer.Option(
            "--operator-settings",
            help="operator settings file carrying hq.sql.release_catalog; overrides "
            "$BH_HQ_OPERATOR_SETTINGS",
        ),
    ] = None,
) -> None:
    """HIDDEN — the signed release catalog (docs/HQ.md#release-catalog).

    import VERSION [--bundle F] [--wheel F] [--trusted-root F]: verify the release's Sigstore
    bundle, recompute its digest from the wheel and append it.

    verify [VERSION|--all] [--trusted-root F]: re-verify stored bundles with no fetch.

    revoke VERSION --reason TEXT: append a revocation tombstone (a PyPI yank never revokes)."""
    try:
        if action not in ACTIONS:
            raise ValueError(f"unknown action {action!r} (expected one of {', '.join(ACTIONS)})")
        if action != "verify" and (all_ or not version):
            raise ValueError(f"{action} takes a VERSION" + (" and no --all" if all_ else ""))
        if action != "import" and (bundle or wheel):
            raise ValueError("--bundle and --wheel apply to import only")
        if action != "revoke" and reason:
            raise ValueError("--reason applies to revoke only")
        if action == "revoke" and not reason:
            raise ValueError("revoke needs --reason")
        if action == "verify" and bool(version) == all_:
            raise ValueError("verify takes a VERSION or --all")
        if action == "import":
            verifier = _verifier()
            source = hq_release_catalog.ComposedSource(_remote, bundle, wheel)
            result = hq_release_catalog.import_release(
                version,
                source=source,
                verifier=verifier,
                store=_store(operator_settings),
                trusted_root=trusted_root,
            )
        elif action == "revoke":
            result = hq_release_catalog.revoke_release(
                version, reason, store=_store(operator_settings)
            )
        else:
            result = hq_release_catalog.verify_rows(
                store=_store(operator_settings),
                verifier=_verifier(),
                version=version or None,
                trusted_root=trusted_root,
            )
            if result["failed"]:
                typer.echo(json.dumps(result, sort_keys=True))
                raise typer.Exit(1)
        typer.echo(json.dumps(result, sort_keys=True))
    except (hq_release_catalog.CatalogError, ValueError, OSError, RuntimeError) as exc:
        typer.echo(f"release-catalog refused: {exc}", err=True)
        raise typer.Exit(1) from exc
