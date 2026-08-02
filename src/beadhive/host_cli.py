"""``bh host`` — the operator-facing surface for the fleet roster AND the host lease
(bh-ytbb.5, extended by bh-ytbb.13, extended by bh-twc8.1).

``init``/``list``/``show`` (bh-ytbb.5) are over the ``hosts/<host_id>.yaml`` manifests
bh-ytbb.3 defined in Factory HQ (:mod:`beadhive.hosts`): ``init`` mints/writes THIS host's
own manifest, ``list`` renders every manifest in HQ, ``show`` details one.

``adopt``/``release``/``packup`` (bh-ytbb.13) are the operator verbs over the **host lease**
(:mod:`beadhive.host_lease`, :mod:`beadhive.host_adopt`) — the CLI's first wiring of those
already-landed primitives. ``adopt`` becomes primary for a hive (two-phase: the hive's own
epoch fence first, then HQ's lease — :mod:`beadhive.host_adopt`). ``release`` yields THIS
host's own lease for one hive. ``packup`` releases every hive this host currently holds in
one pass — the lease-bookkeeping half of ``docs/CONTROL-PLANE.md``'s
pack-up-before-host-switch ritual (the data half stays ``bh hive sync-remote --all``).

``list`` is ALSO the visibility answer for lease state — per-hive held/expiring/free, with
holder, via ``--lease-hive`` (deliberately not the reserved ``--hive`` — see :func:`list_cmd`).
bh-ytbb.5 built :func:`render_table` generic FOR exactly this
(already-assembled row dicts + a ``(row key, header)`` column spec, rather than reaching into
a :class:`HostManifest` itself) so this extension adds a ``lease`` key to rows built the SAME
way and an extended column spec, without restructuring :func:`render_table` or
:func:`list_payload`.

No ``last_seen``/``updated_at`` field exists on the manifest schema — bh-ytbb.3 deliberately
left it off (open question flagged for this exact bead). Rather than re-touch that landed
schema, "last-seen" here is derived from the manifest FILE's mtime (:func:`_last_seen`): it
IS a ref on disk, so reading its mtime keeps the "reading refs, not running a daemon" framing
intact with zero schema change.

``provision`` (bh-twc8.1, :mod:`beadhive.host_provision`) mechanizes the whole hand-assembled
new-host adoption path — ``config init`` -> ``git workspace update`` -> ``hq.remote`` ->
``hq clone`` -> ``host init`` -> per-hive bead sync -> permission fix -> a verifying gate — as
one idempotent, resumable verb. This module's own :func:`ensure_manifest` is the extraction
``host_provision``'s ``host init`` step reuses (the exact ``init`` mechanics, no CLI layer);
the command itself just wraps :func:`beadhive.host_provision.provision`.
"""

from __future__ import annotations

import platform
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

import typer

from . import config, gitref, host, host_adopt, host_fence, host_lease, hosts, otel, registry

app = typer.Typer(
    no_args_is_help=True,
    help=f"{config.BINARY_ALIAS} fleet roster: this host's manifest in Factory HQ.",
)

_AS_JSON = typer.Option(False, "--json", help="machine payload (as_json)")
_FORCE = typer.Option(False, "-f", "--force", help="overwrite an existing manifest")
_HIVE_ARG = typer.Argument(
    ..., metavar="<hive>", help="hive id — prefix / org/repo / full triplet (see `bh hive list`)"
)


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


def ensure_manifest(
    *,
    role: str,
    label: str = "",
    identity_kind: str = "none",
    identity_value: str = "",
    force: bool = False,
) -> tuple[Path, bool]:
    """Mint/write THIS host's own manifest into HQ — the core ``bh host init`` drives, extracted
    so a second caller (``bh host provision`` — bh-twc8.1) can reuse the identical no-clobber
    semantics as its own step without going through the CLI layer.

    ``host_id``/`os`/`arch` come from the local machine (:mod:`beadhive.host` + ``platform``);
    `role`/identity are the caller's explicit choice (no default role — asymmetric TTL renewal
    reads it, so a silent guess would be wrong more often than it's right). Refuses to overwrite
    an existing manifest unless `force`. Callers own `role`/`identity_kind` validation against
    :data:`hosts.HOST_ROLES` / :data:`hosts.IDENTITY_MECHANISM_KINDS` — this assumes both are
    already-valid members.

    Returns ``(path, wrote)`` — ``wrote=False`` when an existing manifest was left completely
    untouched (`path` is still the manifest's location either way)."""
    hq_dir = config.hq_dir()
    hid = host.host_id()
    target = hosts.manifest_path(hq_dir, hid)
    if target.exists() and not force:
        return target, False

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
    return written, True


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
    """CLI wrapper over :func:`ensure_manifest`: validates ``--role``/``--identity-kind``
    against their closed sets, then mints/writes — refuses to overwrite an existing manifest
    unless ``--force``, matching ``bh config init``'s templated-file idiom."""
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

    target, wrote = ensure_manifest(
        role=role, label=label, identity_kind=identity_kind, identity_value=identity_value,
        force=force,
    )
    if not wrote:
        typer.echo(f"skip {target} (exists) — use --force to overwrite")
        return
    typer.echo(f"✓ wrote {target}")


def list_payload(hq_dir: Path) -> list[dict[str, str]]:
    """The rows :func:`render_table` renders for ``list`` — the JSON payload shape too.
    Split out from the command so tests (and a future MCP resource) can call it directly."""
    return [manifest_row(m, p) for m, p in iter_manifests(hq_dir)]


# The column spec `list --lease-hive` renders — BASE_COLUMNS plus the lease-state column
# bh-ytbb.13 appends, per the render_table seam bh-ytbb.5 built for exactly this.
LEASE_COLUMNS: tuple[tuple[str, str], ...] = (*BASE_COLUMNS, ("lease", "LEASE"))


def with_lease_state(
    rows: list[dict[str, str]], prefix: str, lease: host_lease.HostLease | None, state: str
) -> tuple[list[dict[str, str]], str]:
    """`rows` (as :func:`list_payload` built them) enriched with a ``lease`` key on the
    HOLDER's row only, plus a one-line human summary — a fully ``"free"`` lease leaves no
    row visibly different at all (nobody is `held`), so the summary is what actually says so.

    A NEW list, never a mutation of `rows` in place (bh-ytbb.5's own rows are reused
    unmodified for every other hive). When the holder has no manifest in the roster (adopted
    without ever running ``bh host init``), a synthesized row is appended so "with holder" is
    never silently dropped — :func:`render_table` already renders a heterogeneous row set
    (some keys missing on some rows) without raising."""
    if lease is None or state == "free":
        return rows, f"lease ({prefix}): free"
    holder = lease.host_id
    enriched = [dict(row, lease=state) if row.get("host_id") == holder else row for row in rows]
    if not any(row.get("host_id") == holder for row in rows):
        enriched = [
            *enriched,
            {"host_id": holder, "label": lease.label, "role": "", "last_seen": "", "lease": state},
        ]
    summary = (
        f"lease ({prefix}): {state} — held by {holder} ({lease.label}), "
        f"expires {lease.expires_at}"
    )
    return enriched, summary


@app.command(
    "list",
    help="render every host manifest in HQ (label, role, last-seen); "
    "--lease-hive adds lease state.",
)
@otel.trace_verb("host.list")
def list_cmd(
    as_json: bool = _AS_JSON,
    lease_hive: str = typer.Option(
        "",
        "--lease-hive",
        help="scope a LEASE column to one hive's held/expiring/free state",
    ),
):
    """Every ``hosts/<host_id>.yaml`` manifest in Factory HQ, one row per host. Reads refs
    only — no daemon, no live probe (last-seen is the manifest file's own mtime).

    Deliberately ``--lease-hive``, NOT the reserved ``--hive`` (cli-mcp-naming-conventions-adr
    §5d/§5d-i): the ADR's ``--hive`` means "target ONE hive, default the cwd's" and is scoped
    to hive-scoped commands (``work``/``plan``/``worktree``) — ``host`` is a Fleet/HQ command,
    explicitly listed among those ``--hive`` does NOT apply to, and this flag has no
    cwd-resolution default (omit it and NOTHING changes — bh-ytbb.5's exact original output).
    A same-spelled ``--hive`` here would silently imply the wrong semantics.

    Without ``--lease-hive``, output is byte-identical to bh-ytbb.5's original (no lease column
    at all — the base case stays unchanged). With it, a live HQ read (this command is a
    reporting surface, not the hot-path write guard — see ``guard_primary``/``renew_if_due``
    for why THAT path stays cache-only) fetches the named hive's current lease and adds a
    LEASE column via :func:`with_lease_state`."""
    hq_dir = config.hq_dir()
    rows = list_payload(hq_dir)
    columns = BASE_COLUMNS
    summary = ""
    if lease_hive:
        cfg = config.load()
        entry = registry.resolve_hive(cfg, lease_hive)
        prefix = str(entry["prefix"])
        try:
            lease = host_lease.read("origin", prefix, cwd=hq_dir)
        except gitref.RemoteUnreachable as exc:
            typer.echo(f"✗ could not read {prefix}'s lease — HQ unreachable: {exc}", err=True)
            raise typer.Exit(1) from None
        state = host_lease.lease_state(lease, renew_interval=config.host_lease_renew_interval(cfg))
        rows, summary = with_lease_state(rows, prefix, lease, state)
        columns = LEASE_COLUMNS
    if as_json:
        import json as json_mod

        typer.echo(json_mod.dumps(rows, indent=2))
        return
    if summary:
        typer.echo(summary)
    typer.echo(render_table(rows, columns))


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


# ---- adopt / release / packup: the host lease's operator verbs (bh-ytbb.13) ------------


def _require_hq_dir() -> Path:
    """Factory HQ's local clone, or a loud refusal — the lease lives there, so every write
    verb below is pointless without one (unlike `list`/`show`, which degrade gracefully to an
    empty roster)."""
    hq_dir = config.hq_dir()
    if not (hq_dir / ".git").exists():
        typer.echo(
            f"✗ no Factory HQ clone at {hq_dir} — the host lease lives there.\n"
            f"  bootstrap one with `{config.BINARY_ALIAS} hq init` or "
            f"`{config.BINARY_ALIAS} hq clone`.",
            err=True,
        )
        raise typer.Exit(1)
    return hq_dir


def _require_host_id() -> str:
    try:
        return host.host_id()
    except FileNotFoundError as exc:
        typer.echo(f"✗ {exc}", err=True)
        raise typer.Exit(1) from None


def _require_manifest(hq_dir: Path, host_id: str) -> hosts.HostManifest:
    """This host's OWN manifest — `adopt` needs its `role` to size the lease TTL
    (`host_lease.ttl_for_role`); a host with no manifest has never declared one, so there is
    nothing safe to derive tenure from."""
    try:
        return hosts.load(hq_dir, host_id)
    except FileNotFoundError:
        typer.echo(
            f"✗ no manifest for this host ({host_id}) in HQ — adopting sizes its lease TTL "
            f"from this host's role.\n  run `{config.BINARY_ALIAS} host init --role <role>` "
            f"first.",
            err=True,
        )
        raise typer.Exit(1) from None
    except hosts.ManifestError as exc:
        typer.echo(f"✗ {exc}", err=True)
        raise typer.Exit(1) from None


@app.command(
    "adopt", help="become primary for <hive> — fence the hive's remote, then lease it in HQ."
)
@otel.trace_verb("host.adopt")
def adopt_cmd(
    hive: str = _HIVE_ARG,
    force: bool = typer.Option(
        False,
        "--force",
        help="seize an unexpired lease held by another host (dangerous — logged loudly).",
    ),
):
    """Become primary for `hive`: the two-phase, fail-closed adopt
    (:func:`beadhive.host_adopt.adopt`, bh-ytbb.8) — the hive's own epoch fence CAS first,
    then HQ's lease CAS second (never the reverse; see that function's docstring for why).
    This host's OWN manifest role sizes the lease TTL (:func:`beadhive.host_lease.ttl_for_role`)
    — a ``worker`` role is refused before either remote is touched.

    Refuses an unexpired lease held by another host unless ``--force``. The escape hatch is
    real (a dead host would otherwise block the fleet until its lease's natural expiry) and
    using it is exactly how split-brain happens — a forced takeover is logged loudly at the
    primitive level (``host_lease.adopt``'s ``host_lease_forced_takeover`` warning); this
    command surfaces it, it never swallows it."""
    cfg = config.load()
    entry = registry.resolve_hive(cfg, hive)
    prefix = str(entry["prefix"])
    hive_dir = registry.hive_dir(entry)
    hq_dir = _require_hq_dir()
    host_id = _require_host_id()
    manifest = _require_manifest(hq_dir, host_id)

    try:
        ttl = host_lease.ttl_for_role(manifest.role, config.host_lease_ttl(cfg))
    except host_lease.HostLeaseRejected as exc:
        typer.echo(f"✗ {exc}", err=True)
        raise typer.Exit(1) from None

    try:
        outcome = host_adopt.adopt(
            prefix=prefix,
            hive_remote="origin",
            hq_remote="origin",
            hive_cwd=hive_dir,
            hq_cwd=hq_dir,
            host_id=host_id,
            label=manifest.label,
            ttl=ttl,
            force=force,
        )
    except (
        host_lease.HostLeaseRejected,
        host_adopt.AdoptHalfDone,
        host_fence.FenceRejected,
    ) as exc:
        typer.echo(f"✗ {exc}", err=True)
        raise typer.Exit(1) from None

    typer.echo(f"✓ adopted {prefix} — epoch {outcome.epoch}, expires {outcome.lease.expires_at}")


@app.command("release", help="yield THIS host's lease for <hive> (a tombstone; epoch survives).")
@otel.trace_verb("host.release")
def release_cmd(hive: str = _HIVE_ARG):
    """Yield this host's own lease for `hive` — CAS to a tombstone in HQ
    (:func:`beadhive.host_lease.release`, bh-ytbb.6).

    Deliberately HQ-only, unlike `adopt`'s two-phase dance: releasing needs no other host's
    cooperation, and the epoch fence on the hive's own remote is left untouched on purpose.
    The NEXT `adopt` already resets enforcement from scratch — fence-first, with a fresh
    epoch that invalidates every prior token, regardless of what this host's now-vacated fence
    token still says in the meantime — so touching the fence here would either be a no-op or
    require inventing a "released but still fenced" state nothing else in this design checks
    for. The local cache mirrors the tombstone immediately, so THIS host's own future
    `guard_primary` calls refuse right away rather than waiting out the TTL."""
    cfg = config.load()
    entry = registry.resolve_hive(cfg, hive)
    prefix = str(entry["prefix"])
    hq_dir = _require_hq_dir()
    host_id = _require_host_id()

    try:
        outcome = host_lease.release("origin", prefix, host_id=host_id, cwd=hq_dir)
    except host_lease.HostLeaseRejected as exc:
        typer.echo(f"✗ {exc}", err=True)
        raise typer.Exit(1) from None

    host_lease.cache(prefix, outcome, cwd=hq_dir)
    typer.echo(f"✓ released {prefix} (epoch {outcome.lease.epoch})")


# ---- provision: the whole new-host adoption path in one idempotent verb (bh-twc8.1) ----


@app.command(
    "provision",
    help="run the whole new-host adoption path — config init, git-workspace update, "
    "hq.remote, hq clone, host init, per-hive bead sync, permission fix, verify — "
    "idempotently, probing before each step.",
)
@otel.trace_verb("host.provision")
def provision_cmd(
    role: str = typer.Option(
        ..., "--role", help=f"host role for `host init`: one of {list(hosts.HOST_ROLES)}"
    ),
    auto: bool = typer.Option(
        False, "--auto",
        help="never prompt (CI/headless) — take the derived hq.remote as-is",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="print the ordered plan; make no changes"
    ),
    force: bool = typer.Option(
        False, "-f", "--force",
        help="re-mint this host's manifest even if one is already registered "
        "(never re-mints host_id/host.yaml itself)",
    ),
):
    """Thin CLI wrapper over :func:`beadhive.host_provision.provision` — see that module's
    docstring for the full pipeline + the hard requirements it holds itself to (never clobber
    ``host.yaml``, confirm ``hq.remote`` interactively unless ``--auto``, zero-mutation
    ``--dry-run``, a verifying gate at the end). Lazy-imports ``host_provision`` (it imports
    this module back, for :func:`ensure_manifest`) so the two stay import-cycle-safe."""
    from . import host_provision

    if role not in hosts.HOST_ROLES:
        typer.echo(f"✗ --role must be one of {list(hosts.HOST_ROLES)} (got {role!r})", err=True)
        raise typer.Exit(1)

    tag = "DRY-RUN " if dry_run else ""
    typer.echo(f"{tag}{config.BINARY_ALIAS} host provision — ordered plan:")
    results = host_provision.provision(role=role, auto=auto, dry_run=dry_run, force_manifest=force)
    for i, r in enumerate(results, start=1):
        tail = f" — {r.detail}" if r.detail else ""
        typer.echo(f"  {i}. {host_provision.GLYPH[r.status]} {r.name}{tail}")

    if dry_run:
        typer.echo("\nDRY-RUN — no changes made.")
        return

    if any(r.status == "failed" for r in results):
        typer.echo("\n✗ provisioning incomplete — see the failed step(s) above.", err=True)
        raise typer.Exit(1)
    typer.echo("\n✓ host fully provisioned.")


@app.command("packup", help="release every hive lease this host currently holds.")
@otel.trace_verb("host.packup")
def packup_cmd():
    """Release every registered hive's lease THIS host currently holds — held or expiring,
    never a `free` one (nothing to release there). Mechanizes the lease-bookkeeping half of
    ``docs/CONTROL-PLANE.md``'s pack-up-before-host-switch ritual: `bh hive sync-remote --all`
    still does the DATA half (pushing every hive's unpushed branches/refs/dolt/data); packup's
    job is making sure the NEXT host's adopt never has to wait out a TTL it doesn't need to.

    One hive's failure (a lost CAS race, or HQ going unreachable mid-pass) is reported and
    does not stop the rest — a partial packup still frees everything it safely could."""
    cfg = config.load()
    hq_dir = _require_hq_dir()
    host_id = _require_host_id()
    renew_interval = config.host_lease_renew_interval(cfg)

    released: list[str] = []
    failed: list[tuple[str, str]] = []
    for prefix, _hive_dir in registry.all_hive_targets(cfg):
        try:
            lease = host_lease.read("origin", prefix, cwd=hq_dir)
        except gitref.RemoteUnreachable as exc:
            failed.append((prefix, f"could not read the lease (HQ unreachable): {exc}"))
            continue
        if lease is None or lease.host_id != host_id:
            continue  # not ours: nothing to release
        if host_lease.lease_state(lease, renew_interval=renew_interval) == "free":
            continue  # already expired/tombstoned: nothing to release

        try:
            outcome = host_lease.release("origin", prefix, host_id=host_id, cwd=hq_dir)
        except host_lease.HostLeaseRejected as exc:
            failed.append((prefix, str(exc)))
            continue
        host_lease.cache(prefix, outcome, cwd=hq_dir)
        released.append(prefix)

    if released:
        typer.echo(f"✓ released {len(released)} hive(s): {', '.join(released)}")
    else:
        typer.echo("(nothing held on this host — no leases released)")
    for prefix, detail in failed:
        typer.echo(f"✗ {prefix}: {detail}", err=True)
    if failed:
        raise typer.Exit(1)
