"""``bh host`` — the operator-facing surface for the fleet roster (bh-ytbb.5).

Three verbs over the ``hosts/<host_id>.yaml`` manifests bh-ytbb.3 defined in Factory HQ
(:mod:`beadhive.hosts`): ``init`` mints/writes THIS host's own manifest, ``list`` renders
every manifest in HQ, ``show`` details one. All three are pure reads-of-refs-or-a-write-of-
one-file — no daemon, matching the epic's framing.

``list`` is ALSO the visibility answer for lease state a later bead extends
(``bh-ytbb.13``, ``bh host adopt|release|packup`` — per-hive lease held/expiring/free, with
holder — see ``docs/design/multi-host-model-adr.md`` Amendment 1 Consequences). That bead
was neither filed nor landed when this one was written, so the row-rendering below is
deliberately generic: :func:`render_table` takes already-assembled row dicts + a
``(row key, header)`` column spec, rather than reaching into a :class:`HostManifest` itself.
bh-ytbb.13 appends its lease-state column to rows built the same way, without restructuring
this function.

No ``last_seen``/``updated_at`` field exists on the manifest schema — bh-ytbb.3 deliberately
left it off (open question flagged for this exact bead). Rather than re-touch that landed
schema, "last-seen" here is derived from the manifest FILE's mtime (:func:`_last_seen`): it
IS a ref on disk, so reading its mtime keeps the "reading refs, not running a daemon" framing
intact with zero schema change.
"""

from __future__ import annotations

import platform
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

import typer

from . import config, host, hosts, otel

app = typer.Typer(
    no_args_is_help=True,
    help=f"{config.BINARY_ALIAS} fleet roster: this host's manifest in Factory HQ.",
)

_AS_JSON = typer.Option(False, "--json", help="machine payload (as_json)")
_FORCE = typer.Option(False, "-f", "--force", help="overwrite an existing manifest")


# ---- local machine facts -----------------------------------------------------


def _local_os_arch() -> tuple[str, str]:
    """``(os, arch)`` derived from the running Python's ``platform`` info — lowercased to
    match the manifest schema's own examples (``darwin``/``linux``/``windows``,
    ``arm64``/``x86_64``)."""
    return platform.system().lower(), platform.machine().lower()


def _last_seen(path: Path) -> str:
    """Human-facing "last-seen" for one manifest: its FILE mtime — local-zone ISO-8601,
    seconds precision (matches :mod:`beadhive.worktree`'s validation-verdict timestamp
    convention). Never a schema field; see module docstring."""
    return datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")


# ---- roster: enumerate + render -----------------------------------------------


def iter_manifests(hq_dir: Path) -> list[tuple[hosts.HostManifest, Path]]:
    """Every manifest under ``hq_dir``'s ``hosts/`` dir (glob ``*.yaml``), loaded +
    validated, paired with its own path (:func:`_last_seen` reads its mtime). Sorted by
    ``host_id`` for a stable rendering order. A malformed manifest is skipped — with a
    warning on stderr — rather than aborting the whole roster read; one broken host must
    not black out visibility into every other one."""
    manifest_dir = hosts.hosts_dir(hq_dir)
    if not manifest_dir.is_dir():
        return []
    out: list[tuple[hosts.HostManifest, Path]] = []
    for p in sorted(manifest_dir.glob("*.yaml")):
        try:
            out.append((hosts.load(hq_dir, p.stem), p))
        except hosts.ManifestError as exc:
            typer.echo(f"⚠ skipping {p}: {exc}", err=True)
    return out


def manifest_row(manifest: hosts.HostManifest, path: Path) -> dict[str, str]:
    """One roster row's base fields. A dict, not a tuple/dataclass, on purpose: a later
    caller (bh-ytbb.13) builds its OWN rows the same way — this manifest-only dict plus an
    extra lease-state key — and passes an extended column spec to :func:`render_table`
    without this function or that one changing shape."""
    return {
        "host_id": manifest.host_id,
        "label": manifest.label,
        "role": manifest.role,
        "last_seen": _last_seen(path),
    }


# The column spec `list` renders today. Exported so a later caller can extend it
# (`(*BASE_COLUMNS, ("lease", "LEASE"))`) instead of hard-coding a fresh tuple.
BASE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("host_id", "HOST_ID"),
    ("label", "LABEL"),
    ("role", "ROLE"),
    ("last_seen", "LAST_SEEN"),
)


def render_table(rows: Sequence[dict[str, str]], columns: Sequence[tuple[str, str]]) -> str:
    """Render already-assembled row dicts against a ``(row key, header)`` column spec as a
    padded plain-text table. Generic on purpose — the seam a later caller (bh-ytbb.13) uses
    to add a lease-state column: it builds rows + an extended `columns` tuple and calls this
    same function, never reaching into a :class:`HostManifest` here. Missing keys render
    blank rather than raising, so a heterogeneous row set (e.g. some hosts have no lease
    row yet) still renders."""
    if not rows:
        return "(no hosts registered)"
    widths = {
        key: max(len(header), *(len(str(row.get(key, ""))) for row in rows))
        for key, header in columns
    }

    def _line(values: dict[str, str]) -> str:
        return "  ".join(f"{str(values.get(key, '')):<{widths[key]}}" for key, _h in columns)

    lines = [_line(dict(columns))]
    lines.extend(_line(row) for row in rows)
    return "\n".join(lines)


# ---- verbs ---------------------------------------------------------------------


@app.command("init", help="mint/write THIS host's own manifest into HQ (hosts/<host_id>.yaml).")
@otel.trace_verb("host.init")
def init_cmd(
    role: str = typer.Option(..., "--role", help=f"host role: one of {list(hosts.HOST_ROLES)}"),
    label: str = typer.Option(
        "", "--label", help="override the manifest label (default: host.yaml's label)"
    ),
    identity_kind: str = typer.Option(
        "none",
        "--identity-kind",
        help=f"identity mechanism kind: one of {list(hosts.IDENTITY_MECHANISM_KINDS)}",
    ),
    identity_value: str = typer.Option(
        "", "--identity-value", help="the identity mechanism's concrete value"
    ),
    force: bool = _FORCE,
):
    """Mint this host's manifest — ``host_id``/`os`/`arch` from the local machine
    (``beadhive.host`` + ``platform``), ``role``/identity explicit on the command line (no
    default role — asymmetric TTL renewal reads it, so a silent guess would be wrong more
    often than it's right). Refuses to overwrite an existing manifest unless ``--force``,
    matching ``bh config init``'s templated-file idiom."""
    if role not in hosts.HOST_ROLES:
        typer.echo(f"✗ --role must be one of {list(hosts.HOST_ROLES)} (got {role!r})", err=True)
        raise typer.Exit(1)
    if identity_kind not in hosts.IDENTITY_MECHANISM_KINDS:
        typer.echo(
            f"✗ --identity-kind must be one of {list(hosts.IDENTITY_MECHANISM_KINDS)} "
            f"(got {identity_kind!r})",
            err=True,
        )
        raise typer.Exit(1)

    hq_dir = config.hq_dir()
    hid = host.host_id()
    target = hosts.manifest_path(hq_dir, hid)
    if target.exists() and not force:
        typer.echo(f"skip {target} (exists) — use --force to overwrite")
        return

    os_name, arch = _local_os_arch()
    manifest = hosts.HostManifest(
        host_id=hid,
        label=label or host.label(),
        os=os_name,
        arch=arch,
        role=role,
        identity=hosts.IdentityMechanism(kind=identity_kind, value=identity_value),
    )
    written = hosts.save(hq_dir, manifest)
    typer.echo(f"✓ wrote {written}")


def list_payload(hq_dir: Path) -> list[dict[str, str]]:
    """The rows :func:`render_table` renders for ``list`` — the JSON payload shape too.
    Split out from the command so tests (and a future MCP resource) can call it directly."""
    return [manifest_row(m, p) for m, p in iter_manifests(hq_dir)]


@app.command("list", help="render every host manifest in HQ (label, role, last-seen).")
@otel.trace_verb("host.list")
def list_cmd(as_json: bool = _AS_JSON):
    """Every ``hosts/<host_id>.yaml`` manifest in Factory HQ, one row per host. Reads refs
    only — no daemon, no live probe (last-seen is the manifest file's own mtime)."""
    hq_dir = config.hq_dir()
    rows = list_payload(hq_dir)
    if as_json:
        import json as json_mod

        typer.echo(json_mod.dumps(rows, indent=2))
        return
    typer.echo(render_table(rows, BASE_COLUMNS))


@app.command("show", help="show one host's full manifest detail.")
@otel.trace_verb("host.show")
def show_cmd(
    host_id: str = typer.Argument(..., metavar="<host_id>", help="host_id from `bh host list`"),
    as_json: bool = _AS_JSON,
):
    """One host's manifest in full — every field ``bh host list``'s table doesn't have room
    for (identity mechanism, capacity, harnesses), plus the same file-mtime last-seen."""
    hq_dir = config.hq_dir()
    try:
        manifest = hosts.load(hq_dir, host_id)
    except FileNotFoundError:
        typer.echo(f"✗ no host manifest for {host_id!r} in HQ", err=True)
        raise typer.Exit(1) from None
    except hosts.ManifestError as exc:
        typer.echo(f"✗ {exc}", err=True)
        raise typer.Exit(1) from None

    path = hosts.manifest_path(hq_dir, host_id)
    if as_json:
        import json as json_mod

        payload = manifest.model_dump(mode="json")
        payload["last_seen"] = _last_seen(path)
        typer.echo(json_mod.dumps(payload, indent=2))
        return

    typer.echo(f"host_id:    {manifest.host_id}")
    typer.echo(f"label:      {manifest.label}")
    typer.echo(f"role:       {manifest.role}")
    typer.echo(f"os/arch:    {manifest.os}/{manifest.arch}")
    typer.echo(f"last_seen:  {_last_seen(path)}")
    typer.echo(f"identity:   {manifest.identity.kind} ({manifest.identity.value or '—'})")
    typer.echo(f"capacity:   {manifest.capacity or '(none)'}")
    typer.echo(f"harnesses:  {manifest.harnesses or '(none)'}")
