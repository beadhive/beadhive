"""``bh host`` — the operator-facing surface for the fleet roster AND the host lease
(bh-ytbb.5, extended by bh-ytbb.13, bh-salu, bh-twc8.1, bh-twc8.2).

``init``/``list``/``show`` (bh-ytbb.5) are over the ``hosts/<host_id>.yaml`` manifests
bh-ytbb.3 defined in Factory HQ (:mod:`beadhive.hosts`): ``init`` mints/writes THIS host's
own manifest, ``list`` renders every manifest in HQ, ``show`` details one.

``lease adopt``/``lease release`` (bh-ytbb.13, regrouped by bh-onm1) are the operator verbs over
the **host lease** (:mod:`beadhive.host_lease`, :mod:`beadhive.host_adopt`) — the CLI's first
wiring of those already-landed primitives. ``adopt`` becomes primary for a hive (two-phase: the
hive's own epoch fence first, then HQ's lease — :mod:`beadhive.host_adopt`). ``release`` yields
THIS host's own lease for one hive; ``release --all`` does every hive this host currently holds
in one pass — the lease-bookkeeping half of ``docs/CONTROL-PLANE.md``'s
pack-up-before-host-switch ritual (the data half stays ``bh hive sync-remote --all``).

They live under a ``lease`` subgroup because their OBJECT is a lease, not the host: every other
``bh host`` verb acts on this machine's own existence in the fleet, while these act on a
renewable, time-bounded claim over a hive. Flat, ``host release`` sat three rows from
``host retire`` and read as its milder synonym, when in fact one is reversible and the other
terminal — and ``host adopt <hive>`` silently took a HIVE argument in a group whose every other
verb takes a host_id or nothing. See the verb model in
``docs/design/cli-mcp-naming-conventions-adr.md`` §5b-i. The flat spellings stay registered as
hidden aliases.

``rm`` (bh-salu; spelled ``remove`` until bh-2v6d) drops a manifest from HQ — ``host_id``
is minted once by ``bh config init`` and never regenerated or synced
(:mod:`beadhive.host`'s module docstring), so a wiped-and-rebuilt host comes back under a
DIFFERENT identity and its old manifest never goes away on its own. Deliberately gated so it
cannot silently evict a live machine — see :func:`rm_cmd`.

``list`` is ALSO the visibility answer for lease state — per-hive held/expiring/free, with
holder, via ``--lease-hive`` (deliberately not the reserved ``--hive`` — see :func:`list_cmd`).
bh-ytbb.5 built :func:`render_table` generic FOR exactly this
(already-assembled row dicts + a ``(row key, header)`` column spec, rather than reaching into
a :class:`HostManifest` itself) so this extension adds a ``lease`` key to rows built the SAME
way and an extended column spec, without restructuring :func:`render_table` or
:func:`list_payload`. bh-salu reuses the SAME seam again for a ``STALE`` column (see
:func:`_stale_after`) rather than a third bespoke rendering path.

No ``last_seen``/``updated_at`` field exists on the manifest schema — bh-ytbb.3 deliberately
left it off (open question flagged for this exact bead). Rather than re-touch that landed
schema, "last-seen" here is derived from the manifest FILE's mtime (:func:`_last_seen`): it
IS a ref on disk, so reading its mtime keeps the "reading refs, not running a daemon" framing
intact with zero schema change.

``provision`` (bh-twc8.1, :mod:`beadhive.host_provision`) mechanizes the whole hand-assembled
new-host adoption path — ``setup check`` -> ``config init`` -> ``git workspace update`` ->
``hq.remote`` -> ``hq clone`` -> ``host init`` -> per-hive bead sync -> permission fix -> a
verifying gate — as
one idempotent, resumable verb. This module's own :func:`ensure_manifest` is the extraction
``host_provision``'s ``host init`` step reuses (the exact ``init`` mechanics, no CLI layer);
the command itself just wraps :func:`beadhive.host_provision.provision`.
"""

from __future__ import annotations

import asyncio
import json
import platform
import time
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

import typer

from . import (
    config,
    dispatch_hive_run,
    dispatch_log,
    dispatch_status,
    dispatch_supervisor,
    git_identity,
    gitref,
    guard,
    host,
    host_adopt,
    host_fence,
    host_lease,
    hosts,
    hq,
    jsonout,
    otel,
    registry,
)

app = typer.Typer(
    no_args_is_help=True,
    help=f"{config.BINARY_ALIAS} fleet roster: this host's manifest in Factory HQ.",
)

# `bh host lease <verb>` (bh-onm1). The lease verbs act on a HIVE LEASE, not on the host — a
# different object from every other `bh host` verb, and a different axis from the removal
# vocabulary (see the verb model in docs/design/cli-mcp-naming-conventions-adr.md §5b-i).
# Flat `bh host adopt|release|packup` stay registered as hidden aliases so landed scripts and
# docs keep working; `packup` is `release --all` under convention 1 (fan-out is a flag).
lease_app = typer.Typer(
    no_args_is_help=True,
    help="hive leases THIS host holds — the renewable claim, not the host itself.",
)
app.add_typer(lease_app, name="lease")

# `bh host dispatch <verb>` (bh-e7r9q.5) — the operator surface for UNATTENDED dispatch: a
# supervised, restart-on-crash `bh host dispatch run --hive <hive>` per hive, installed via
# `beadhive.dispatch_supervisor`. Sibling of `lease` for the same reason `host adopt` and
# `host dispatch enable` are adjacent in the naming ADR: enable REQUIRES a held lease, so the
# precondition is visible as a nearby command rather than an opaque error.
dispatch_app = typer.Typer(
    no_args_is_help=True,
    help="unattended dispatch supervision — enable/disable/status/logs per hive.",
)
app.add_typer(dispatch_app, name="dispatch")

_AS_JSON = typer.Option(False, "--json", help="machine payload (as_json)")
_FORCE = typer.Option(False, "-f", "--force", help="overwrite an existing manifest")
_HIVE_ARG = typer.Argument(
    ..., metavar="<hive>", help="hive id — prefix / org/repo / full triplet (see `bh hive list`)"
)
_HIVE_ARG_OPT = typer.Argument(
    None, metavar="[<hive>]", help="hive id — omit only with --all (see `bh hive list`)"
)
_ALL_HELD = typer.Option(False, "--all", help="every hive THIS host currently holds")

# ---- `bh host dispatch` flags — validated against docs/design/cli-mcp-naming-conventions-adr.md:
# `--hive` is hive-scoped (defaults to cwd's hive, no short flag), `--all` is a legitimate
# AGGREGATE READ on `status` and FORBIDDEN on the per-entity mutations `enable`/`disable`.
_DISPATCH_HIVE = typer.Option("", "--hive", help="hive id (defaults to cwd's hive)")
_DISPATCH_ALL_STATUS = typer.Option(False, "--all", help="every hive on this host (status only)")
# `enable`/`disable` DECLARE NO `--all`. An option advertised in `--help` whose own help text
# says it is invalid is worse than the refusal it was trying to be friendly about: Typer's
# built-in "No such option: --all" is unambiguous, arrives before any work is done, and cannot
# drift out of sync with the handler. `tests/test_host_cli.py` asserts that Typer error.
_DISPATCH_LOGS_LINES = typer.Option(
    200,
    "-n",
    "--limit",
    help="how many recent records to show",
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


def _stale_after(cfg: dict) -> float:
    """Seconds after which a manifest's last-seen mtime reads as "stale" (``bh host list``'s
    STALE column) or blocks ``bh host rm`` without ``--force`` (the target is plausibly
    still alive): the LONGEST tenure any host role's lease can hold —
    ``host.lease.ttl`` (default 1800s) scaled by the biggest entry in
    :data:`beadhive.host_lease.ROLE_TTL_SCALE` (``executor``'s 4x ⇒ 7200s/2h by
    default). One number shared by both surfaces, rather than two independently-tuned
    thresholds that could disagree about what "recent" means."""
    return config.host_lease_ttl(cfg) * max(host_lease.ROLE_TTL_SCALE.values())


def _is_stale(path: Path, threshold: float, *, at: float | None = None) -> bool:
    """Whether ``path`` (a manifest file) hasn't been touched more recently than
    ``threshold`` seconds ago."""
    now = at if at is not None else time.time()
    return (now - path.stat().st_mtime) > threshold


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


def manifest_row(
    manifest: hosts.HostManifest, path: Path, *, stale: bool = False
) -> dict[str, str]:
    """One roster row's base fields. A dict, not a tuple/dataclass, on purpose: a later
    caller (bh-ytbb.13) builds its OWN rows the same way — this manifest-only dict plus an
    extra lease-state key — and passes an extended column spec to :func:`render_table`
    without this function or that one changing shape. ``stale`` (bh-salu) is precomputed by
    the caller (:func:`list_payload`) against :func:`_stale_after`'s shared threshold, so this
    function stays a pure dict-builder with no config/clock dependency of its own."""
    return {
        "host_id": manifest.host_id,
        "label": manifest.label,
        "role": manifest.role,
        "last_seen": _last_seen(path),
        "stale": "stale" if stale else "",
    }


# The column spec `list` renders today. Exported so a later caller can extend it
# (`(*BASE_COLUMNS, ("lease", "LEASE"))`) instead of hard-coding a fresh tuple.
BASE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("host_id", "HOST_ID"),
    ("label", "LABEL"),
    ("role", "ROLE"),
    ("last_seen", "LAST_SEEN"),
    ("stale", "STALE"),
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
    unless ``--force``, matching ``bh config init``'s templated-file idiom.

    A deprecated role spelling is RESOLVED (with a warning) rather than refused: an operator
    re-running a command from a v0.8.0 runbook should land on the right role, not on a "must be
    one of" list that does not contain the word they were told to type (bh-7ztwe)."""
    role = hosts.canonical_role(role)
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
        role=role,
        label=label,
        identity_kind=identity_kind,
        identity_value=identity_value,
        force=force,
    )
    if not wrote:
        typer.echo(f"skip {target} (exists) — use --force to overwrite")
        return
    typer.echo(f"✓ wrote {target}")


@app.command(
    "identity",
    help="fill THIS host's git identity gaps (name/email/signing key) from bh's config.",
)
@otel.trace_verb("host.identity")
def identity_cmd(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="print what would be filled; write nothing"
    ),
):
    """Marry the two halves of this host's git identity (bh-ijd4) — the per-host signing key
    from ``host.yaml`` and the operator's name/email from bh's config (which ``fleet.yaml``
    publishes fleet-wide) — into the host's GLOBAL git config, and enroll this host's PUBLIC
    key in HQ's ``allowed_signers``.

    Standalone as well as a provisioning step because a host can be set up without ever being
    provisioned: someone installs bh and works in a clone directly, and that host would
    otherwise keep the same empty git config forever with nothing to fix it.

    There is deliberately NO ``--force``. Every write is a gap-fill; a value git already
    carries is always kept. A host with a working human identity is left byte-identical."""
    fills = git_identity.establish(dry_run=dry_run)
    for f in fills:
        glyph = {
            git_identity.SET: "✓",
            git_identity.KEPT: "•",
            git_identity.WOULD: "→",
            git_identity.UNRESOLVED: "✗",
        }.get(f.action, "?")
        suffix = f"  ({f.detail})" if f.detail else ""
        typer.echo(f"{glyph} {f.key} = {f.value or '(none)'}{suffix}")
    ok, summary = git_identity.summary()
    typer.echo(("✓ " if ok else "✗ ") + summary)
    if not ok and not dry_run:
        raise typer.Exit(1)


def list_payload(hq_dir: Path, cfg: dict | None = None) -> list[dict[str, str]]:
    """The rows :func:`render_table` renders for ``list`` — the JSON payload shape too.
    Split out from the command so tests (and a future MCP resource) can call it directly.
    ``cfg`` (default: :func:`beadhive.config.load`) sizes the STALE marker's threshold
    (:func:`_stale_after`) — accepted rather than always reloaded so a caller that already
    has one (:func:`list_cmd`) doesn't pay a second read."""
    cfg = cfg if cfg is not None else config.load()
    threshold = _stale_after(cfg)
    now = time.time()
    return [
        manifest_row(m, p, stale=_is_stale(p, threshold, at=now)) for m, p in iter_manifests(hq_dir)
    ]


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
        f"lease ({prefix}): {state} — held by {holder} ({lease.label}), expires {lease.expires_at}"
    )
    return enriched, summary


@app.command(
    "list",
    help="render every host manifest in HQ (label, role, last-seen, stale); "
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
    only — no daemon, no live probe (last-seen is the manifest file's own mtime; STALE is
    derived from it too — see :func:`_stale_after`).

    Deliberately ``--lease-hive``, NOT the reserved ``--hive`` (cli-mcp-naming-conventions-adr
    §5d/§5d-i): the ADR's ``--hive`` means "target ONE hive, default the cwd's" and is scoped
    to hive-scoped commands (``work``/``plan``/``worktree``) — ``host`` is a Fleet/HQ command,
    explicitly listed among those ``--hive`` does NOT apply to, and this flag has no
    cwd-resolution default (omit it and NOTHING changes beyond the STALE column bh-salu
    added to the base case). A same-spelled ``--hive`` here would silently imply the wrong
    semantics.

    Without ``--lease-hive``, the base columns are HOST_ID/LABEL/ROLE/LAST_SEEN/STALE — the
    same shape bh-ytbb.5 shipped, plus bh-salu's STALE marker so an orphaned manifest from a
    wiped/rebuilt host (see :mod:`beadhive.host_cli`'s module docstring) is identifiable
    without cross-referencing by hand. With ``--lease-hive``, a live HQ read (this command is a
    reporting surface, not the hot-path write guard — see ``guard_primary``/``renew_if_due``
    for why THAT path stays cache-only) fetches the named hive's current lease and adds a
    LEASE column via :func:`with_lease_state`."""
    hq_dir = config.hq_dir()
    cfg = config.load()
    rows = list_payload(hq_dir, cfg)
    columns = BASE_COLUMNS
    summary = ""
    if lease_hive:
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


def _this_host_id() -> str | None:
    """This machine's own ``host_id``, for `remove`'s self-removal gate (bh-salu) — ``None``
    when ``host.yaml`` hasn't been minted here yet. Unlike :func:`_require_host_id` (which
    `adopt`/`release`/`packup` genuinely cannot proceed without), a missing local identity is
    not an error for `remove`: it just means the target being removed cannot possibly be
    THIS host."""
    try:
        return host.host_id()
    except FileNotFoundError:
        return None


def _scan_leases(
    hq_dir: Path, cfg: dict, *, host_id: str
) -> tuple[list[tuple[str, host_lease.HostLease]], list[tuple[str, str]]]:
    """Every registered hive's host lease currently held (`held` or `expiring`, never `free`)
    by `host_id` — `(prefix, lease)` pairs — plus `(prefix, detail)` for any hive whose lease
    could not even be READ (HQ unreachable), reported rather than silently ignored. Shared by
    `packup_cmd` (releases everything found) and `remove_cmd` (bh-salu: refuses to remove a
    host that still holds one, unless `--force`)."""
    renew_interval = config.host_lease_renew_interval(cfg)
    held: list[tuple[str, host_lease.HostLease]] = []
    unreadable: list[tuple[str, str]] = []
    for prefix, _hive_dir in registry.all_hive_targets(cfg):
        try:
            lease = host_lease.read("origin", prefix, cwd=hq_dir)
        except gitref.RemoteUnreachable as exc:
            unreadable.append((prefix, str(exc)))
            continue
        if lease is None or lease.host_id != host_id:
            continue  # not this host_id's: nothing to act on
        if host_lease.lease_state(lease, renew_interval=renew_interval) == "free":
            continue  # already expired/tombstoned: nothing to act on
        held.append((prefix, lease))
    return held, unreadable


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


def adopt_one(hive: str, *, force: bool = False):
    """Become primary for *hive*, as a callable — the assembly `adopt_cmd` used to inline.

    Extracted for `host_provision`'s adopt step (bh-q160.2) so provisioning and the interactive
    verb cannot drift on the parts that matter: the role-derived TTL, and the two-phase
    fence-then-lease ordering inside `host_adopt.adopt`.

    Raises rather than exiting — a step needs to turn a refusal into a failed StepResult, and a
    `typer.Exit` from inside a provision run would abort the whole plan instead of reporting.
    """
    cfg = config.load()
    entry = registry.resolve_hive(cfg, hive)
    hq_dir = _require_hq_dir()
    host_id = _require_host_id()
    manifest = _require_manifest(hq_dir, host_id)
    ttl = host_lease.ttl_for_role(manifest.role, config.host_lease_ttl(cfg))
    return host_adopt.adopt(
        prefix=str(entry["prefix"]),
        hive_remote="origin",
        hq_remote="origin",
        hive_cwd=registry.hive_dir(entry),
        hq_cwd=hq_dir,
        host_id=host_id,
        label=manifest.label,
        ttl=ttl,
        force=force,
    )


@lease_app.command(
    "adopt", help="become primary for <hive> — fence the hive's remote, then lease it in HQ."
)
@app.command("adopt", hidden=True)
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
    — a ``viewer`` role is refused before either remote is touched.

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
        # AdoptError, not just AdoptHalfDone: it also covers HiveNotCloned, the precondition
        # bh-1atj added — an unhandled traceback is what that bead exists to stop.
        host_adopt.AdoptError,
        host_fence.FenceRejected,
    ) as exc:
        typer.echo(f"✗ {exc}", err=True)
        raise typer.Exit(1) from None

    typer.echo(f"✓ adopted {prefix} — epoch {outcome.epoch}, expires {outcome.lease.expires_at}")


@lease_app.command(
    "release",
    help="yield THIS host's lease for <hive> (a tombstone; epoch survives), or --all of them.",
)
@app.command("release", hidden=True)
@otel.trace_verb("host.release")
def release_cmd(hive: str = _HIVE_ARG_OPT, all_hives: bool = _ALL_HELD):
    """Yield this host's own lease for `hive` — CAS to a tombstone in HQ
    (:func:`beadhive.host_lease.release`, bh-ytbb.6). With ``--all``, does that for every hive
    this host currently holds (bh-onm1's absorption of the former ``packup`` verb — see
    :func:`_release_every_held`).

    Deliberately HQ-only, unlike `adopt`'s two-phase dance: releasing needs no other host's
    cooperation, and the epoch fence on the hive's own remote is left untouched on purpose.
    The NEXT `adopt` already resets enforcement from scratch — fence-first, with a fresh
    epoch that invalidates every prior token, regardless of what this host's now-vacated fence
    token still says in the meantime — so touching the fence here would either be a no-op or
    require inventing a "released but still fenced" state nothing else in this design checks
    for. The local cache mirrors the tombstone immediately, so THIS host's own future
    `guard_primary` calls refuse right away rather than waiting out the TTL."""
    if all_hives == (hive is not None):
        typer.echo("✗ pass exactly one of <hive> or --all", err=True)
        raise typer.Exit(1)

    cfg = config.load()
    if all_hives:
        _release_every_held(cfg, _require_hq_dir(), _require_host_id())
        return

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
    help="run the whole new-host adoption path — setup check, config init, git-workspace "
    "update, hq.remote, hq clone, host init, per-hive bead sync, permission fix, verify — "
    "idempotently, probing before each step.",
)
@otel.trace_verb("host.provision")
def provision_cmd(
    role: str = typer.Option(
        "", "--role", help=f"host role for `host init`: one of {list(hosts.HOST_ROLES)}"
    ),
    answers: str = typer.Option(
        "",
        "--answers",
        help="declarative plan (role, hq.remote, hives, adopt) — for unattended installs.",
    ),
    auto: bool = typer.Option(
        False,
        "--auto",
        help="never prompt (CI/headless) — take the derived hq.remote as-is",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="print the ordered plan; make no changes"
    ),
    force: bool = typer.Option(
        False,
        "-f",
        "--force",
        help="re-mint this host's manifest even if one is already registered "
        "(never re-mints host_id/host.yaml itself)",
    ),
):
    """Thin CLI wrapper over :func:`beadhive.host_provision.provision` — see that module's
    docstring for the full pipeline + the hard requirements it holds itself to (never clobber
    ``host.yaml``, confirm ``hq.remote`` interactively unless ``--auto``, zero-mutation
    ``--dry-run``, a verifying gate at the end). Lazy-imports ``host_provision`` (it imports
    this module back, for :func:`ensure_manifest`) so the two stay import-cycle-safe."""
    from . import host_answers, host_provision

    # VALIDATE THE FILE BEFORE ANY STEP RUNS. A typo'd key must not be discovered halfway
    # through a provision that has already written config and cloned HQ (bh-q160.2).
    plan = None
    if answers:
        if role:
            typer.echo("✗ pass --role OR --answers, not both — the file states the role.", err=True)
            raise typer.Exit(2)
        try:
            plan = host_answers.load(Path(answers))
        except host_answers.AnswersInvalid as exc:
            typer.echo(f"✗ {answers}: {exc}", err=True)
            raise typer.Exit(2) from None
        role = plan.role
    elif not role:
        typer.echo("✗ --role is required (or use --answers)", err=True)
        raise typer.Exit(2)

    if role not in hosts.HOST_ROLES:
        typer.echo(f"✗ --role must be one of {list(hosts.HOST_ROLES)} (got {role!r})", err=True)
        raise typer.Exit(1)

    tag = "DRY-RUN " if dry_run else ""
    typer.echo(f"{tag}{config.BINARY_ALIAS} host provision — ordered plan:")
    results = host_provision.provision(
        role=role,
        auto=auto or plan is not None,  # an answers file IS the answer — never prompt with one
        dry_run=dry_run,
        force_manifest=force,
        adopt=plan.adopt if plan else None,
        hives=plan.hives if plan else None,
    )
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


def _release_every_held(cfg: dict, hq_dir: Path, host_id: str) -> None:
    """Release every registered hive's lease THIS host currently holds — held or expiring,
    never a `free` one (nothing to release there). Mechanizes the lease-bookkeeping half of
    ``docs/CONTROL-PLANE.md``'s pack-up-before-host-switch ritual: `bh hive sync-remote --all`
    still does the DATA half (pushing every hive's unpushed branches/refs/dolt/data); this
    half's job is making sure the NEXT host's adopt never has to wait out a TTL it doesn't need.

    One hive's failure (a lost CAS race, or HQ going unreachable mid-pass) is reported and
    does not stop the rest — a partial pass still frees everything it safely could."""
    held, unreadable = _scan_leases(hq_dir, cfg, host_id=host_id)
    failed: list[tuple[str, str]] = [
        (prefix, f"could not read the lease (HQ unreachable): {detail}")
        for prefix, detail in unreadable
    ]

    released: list[str] = []
    for prefix, _lease in held:
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


@app.command("packup", hidden=True)
@otel.trace_verb("host.packup")
def packup_cmd():
    """Deprecated spelling of `host lease release --all` (bh-onm1) — kept working, off the
    panels. Convention 1 makes fan-out a `--all` flag rather than its own verb name."""
    _release_every_held(config.load(), _require_hq_dir(), _require_host_id())


# ---- dispatch: bh host dispatch enable|disable|status|logs (bh-e7r9q.5) -----------------


def _ensure_lease_for_enable(hive: str, cfg: dict) -> tuple[bool, str]:
    """Verify (or adopt) the host lease before `enable` installs anything. Never starts a loop
    that will silently idle because the operator did not notice — a lease held elsewhere is a
    REFUSAL with the actionable next command, never a warning `enable` proceeds past."""
    state_info = guard.primary_state(hive, cfg=cfg)
    if state_info is None:
        # The multi-host model is not in force for this hive (no HQ clone / never adopted) —
        # single-host default. `dispatch_hive_run`'s NullLeaseKeeper agrees: `held=True` always.
        return True, "no host lease in force for this hive (single-host default)"
    _prefix, this_host, lease = state_info
    if lease.held_by(this_host):
        return True, f"lease already held — {lease.describe()}"
    if lease.is_tombstone or lease.is_expired():
        try:
            outcome = adopt_one(hive)
        except (
            host_lease.HostLeaseRejected,
            host_adopt.AdoptError,
            host_fence.FenceRejected,
        ) as exc:
            return (
                False,
                f"could not adopt the lease for {hive}: {exc}. Run "
                f"`bh host lease adopt {hive}` and retry `bh host dispatch enable --hive {hive}`.",
            )
        return True, f"adopted lease — epoch {outcome.epoch}, expires {outcome.lease.expires_at}"
    return (
        False,
        f"lease held elsewhere ({lease.describe()}); run `bh host lease adopt {hive} --force` "
        f"to take over, then retry `bh host dispatch enable --hive {hive}`.",
    )


def _dispatch_entry(hive: str, cfg: dict):
    main = registry.hive_dir_for(cfg, hive)
    entry = registry.entry_for_dir(cfg, main) or {}
    resolved = hive or registry.hive_key(entry)
    return entry, resolved


@dispatch_app.command(
    "enable",
    help="verify/adopt the lease, install+start+persist unattended dispatch for a hive.",
)
@otel.trace_verb("host.dispatch.enable")
def dispatch_enable_cmd(
    hive: str = _DISPATCH_HIVE,
    as_json: bool = _AS_JSON,
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help=(
            "supervise a DECIDE-ONLY loop: every `bh work loop <epic>` this picker spawns "
            "runs `--dry-run` and exits after one pass, never claiming/provisioning/spawning/"
            "writing (bh-3xl60). Persists across restarts like any other `enable`."
        ),
    ),
    seat_binary: str = typer.Option(
        "",
        "--seat-binary",
        help=(
            "supervise a NO-OP HARNESS: every seat this picker's children spawn runs THIS "
            "binary instead of the configured role binary (bh-3xl60)."
        ),
    ),
):
    """Idempotent, like every other bh provisioning step: re-running `enable` converges a
    half-state (unit installed but stopped, started but not persisted) rather than erroring.

    Verifies this host holds `hive`'s lease — adopting it when it is free, refusing with the
    actionable next command when it is held elsewhere — THEN installs/starts/persists the
    supervisor backend (`beadhive.dispatch_supervisor`). Order matters: a loop enabled before
    the lease question is settled would start and immediately idle, which is correct behavior
    for the RUN process but a confusing first impression for `enable` itself.

    `--dry-run` / `--seat-binary` (bh-3xl60) are carried onto `bh host dispatch run --hive
    <hive>`'s own argv via a per-instance systemd drop-in
    (`bh-dispatch@<slug>.service.d/override.conf`) — the unattended path is where surprise is
    expensive, so both modes belong here too, not only on `bh work loop`. Re-running `enable`
    with neither flag converges the override away.

    There is deliberately NO `--all`: the naming ADR's flag table rules it out for per-entity
    mutations, and switching unattended dispatch on across nineteen hives in one keystroke is
    the command an operator would regret. It is not declared at all, so `--all` gets Typer's
    "No such option" rather than an advertised flag that refuses itself."""
    cfg = config.load()
    entry, resolved_hive = _dispatch_entry(hive, cfg)

    lease_ok, lease_msg = _ensure_lease_for_enable(resolved_hive, cfg)
    if not lease_ok:
        typer.echo(f"✗ {lease_msg}", err=True)
        raise typer.Exit(1)

    dispatch_log.ensure_sink_dir()
    slug = dispatch_log.hive_slug(entry)
    try:
        backend = dispatch_supervisor.get_supervisor_backend(cfg)
    except (NotImplementedError, ValueError) as exc:
        typer.echo(f"✗ {exc}", err=True)
        raise typer.Exit(1) from None
    exec_argv: list[str] = []
    if dry_run:
        exec_argv.append("--dry-run")
    if seat_binary:
        exec_argv += ["--seat-binary", seat_binary]
    backend.enable(slug, exec_argv=exec_argv, env={})
    status = dispatch_status.compute_status(resolved_hive, cfg=cfg, backend=backend)
    if as_json:
        jsonout.emit(status.as_dict())
        return
    typer.echo(
        f"✓ dispatch enabled for {resolved_hive} — backend={backend.name} state={status.state}"
    )
    if dry_run:
        typer.echo("  ⚠ DRY RUN — every dispatched pass will decide-only, never act.")
    if seat_binary:
        typer.echo(f"  seats spawn {seat_binary!r} instead of the configured role binary.")
    typer.echo(f"  {lease_msg}")


@dispatch_app.command(
    "disable", help="stop and de-persist unattended dispatch for a hive. Destroys nothing."
)
@otel.trace_verb("host.dispatch.disable")
def dispatch_disable_cmd(
    hive: str = _DISPATCH_HIVE,
    as_json: bool = _AS_JSON,
):
    """Stops the supervised process and removes it from boot persistence. Never removes the
    clone, the worktrees, any bead, or the lease — `disable` is reversible; a later `enable`
    just starts it again. No `--all`, for the same reason as `enable` (see its docstring)."""
    cfg = config.load()
    entry, resolved_hive = _dispatch_entry(hive, cfg)
    slug = dispatch_log.hive_slug(entry)
    try:
        backend = dispatch_supervisor.get_supervisor_backend(cfg)
    except (NotImplementedError, ValueError) as exc:
        typer.echo(f"✗ {exc}", err=True)
        raise typer.Exit(1) from None
    backend.disable(slug)
    status = dispatch_status.compute_status(resolved_hive, cfg=cfg, backend=backend)
    if as_json:
        jsonout.emit(status.as_dict())
        return
    typer.echo(
        f"✓ dispatch disabled for {resolved_hive} (nothing destroyed — state={status.state})"
    )


def _render_dispatch_status_row(status: dispatch_status.DispatchStatus) -> str:
    lease = "n/a" if not status.lease_in_force else ("held" if status.lease_held else "NOT held")
    return (
        f"{status.hive:40} {status.state:24} backend={status.backend:8} "
        f"lease={lease:9} seats_in_flight={status.seats_in_flight} "
        f"epics_in_flight={status.epics_in_flight} last_pass={status.last_pass_at or '—'}"
    )


@dispatch_app.command(
    "status",
    help="supervised? running? lease held and expiring when? last pass? seats in flight?",
)
@otel.trace_verb("host.dispatch.status")
def dispatch_status_cmd(
    hive: str = _DISPATCH_HIVE,
    all_hives: bool = _DISPATCH_ALL_STATUS,
    as_json: bool = _AS_JSON,
):
    """The read that replaces SSH: an operator who cannot see what the loop is doing from their
    laptop will keep logging into the VM, and then the ergonomics gain of unattended dispatch is
    fiction. `--all` is a legitimate aggregate read here (unlike on `enable`/`disable`)."""
    cfg = config.load()
    if all_hives:
        rows = dispatch_status.compute_status_all(cfg)
        if as_json:
            jsonout.emit({"hives": [r.as_dict() for r in rows]})
            return
        if not rows:
            typer.echo("(no hives registered)")
            return
        for row in rows:
            typer.echo(_render_dispatch_status_row(row))
        return

    _entry, resolved_hive = _dispatch_entry(hive, cfg)
    status = dispatch_status.compute_status(resolved_hive, cfg=cfg)
    if as_json:
        jsonout.emit(status.as_dict())
        return
    typer.echo(_render_dispatch_status_row(status))
    if status.detail:
        typer.echo(f"  {status.detail}")
    if status.lease_expiring_soon:
        typer.echo(f"  ⚠ lease expiring soon ({status.lease_expires_at})")
    if status.last_escalation:
        typer.echo(f"  ⚠ last escalation: {status.last_escalation.get('reason', '')}")


@dispatch_app.command(
    "logs", help="backend-agnostic tail of the hive's ONE aggregate dispatch log."
)
@otel.trace_verb("host.dispatch.logs")
def dispatch_logs_cmd(
    hive: str = _DISPATCH_HIVE,
    lines: int = _DISPATCH_LOGS_LINES,
    as_json: bool = _AS_JSON,
):
    """Reads the structured JSONL sink every loop for this hive writes to
    (`beadhive.dispatch_log`) — never `journalctl`/`log show`/`docker logs`, so this command
    behaves identically under every supervision backend.

    The row-count flag is `-n/--limit`, the established spelling in this CLI and in `bd`
    (`work.py` even defines `_READY_LIMIT_FLAGS = {"-n", "--limit"}`); it is NOT `--lines`."""
    cfg = config.load()
    entry, _resolved_hive = _dispatch_entry(hive, cfg)
    path = dispatch_log.sink_path(cfg, entry)
    records = dispatch_log.tail_records(path, lines=lines)
    if as_json:
        jsonout.emit({"records": records})
        return
    if not records:
        typer.echo(f"(no dispatch log records yet — {path})")
        return
    for record in records:
        typer.echo(json.dumps(record, sort_keys=True))


@dispatch_app.command(
    "run",
    hidden=True,
    help="INTERNAL: the process a supervision backend starts. Not for interactive use.",
)
@otel.trace_verb("host.dispatch.run")
def dispatch_run_cmd(
    hive: str = typer.Option(..., "--hive", help="hive this loop supervises"),
    passes: int = typer.Option(0, "--passes", help="stop after N passes (0 = run forever)"),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help=(
            "forward `--dry-run` to every `bh work loop <epic>` child this picker would "
            "spawn: each runs one decide-only pass and exits, never claiming/provisioning/"
            "spawning/writing (bh-3xl60)."
        ),
    ),
    seat_binary: str = typer.Option(
        "",
        "--seat-binary",
        help="forward `--seat-binary <path>` to every `bh work loop <epic>` child (bh-3xl60).",
    ),
):
    """The hive-level picker (`beadhive.dispatch_hive_run.HiveDispatchRun`) — what
    `bh-dispatch@<hive-slug>.service` (or the equivalent unit on another backend) actually runs.
    A human can run this directly for debugging, which is why it is `hidden`, not `internal`-only
    gated: hidden keeps it off `--help` without making it inaccessible."""
    cfg = config.load()
    driver = dispatch_hive_run.build_run(hive, cfg=cfg, dry_run=dry_run, seat_binary=seat_binary)
    from . import log as log_mod

    log_mod.add_file_sink(str(driver.sink_path))
    asyncio.run(driver.run(max_passes=passes or None))


# ---- retire: guarded, host-local decommission of THIS host (bh-twc8.2) ----------------


@app.command(
    "retire",
    help="HOST-LOCAL: guarded decommission of THIS host — one SAFE/NEEDS_BACKUP/BLOCKED "
    "verdict folding every hive, managed worktree, held lease, and Factory HQ (both halves), "
    "then the guarded ordered teardown: release leases -> sync+push every hive (beads AND "
    "code) -> reclaim local clones/worktrees -> deregister this host's manifest -> push HQ. "
    "NEVER touches managed_repos/fleet registration — for that, see `bh hive retire`/"
    "`bh hive reclaim`. --dry-run previews the full ordered plan with zero mutation; --backup "
    "snapshots unpushed/dirty work first; --confirm accepts remaining risk and performs the "
    "teardown.",
)
@otel.trace_verb("host.retire")
def retire_cmd(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="print the full ordered plan and change nothing (default-safe)"
    ),
    backup: bool = typer.Option(
        False, "--backup", help="snapshot unpushed/dirty work to durable wip branches first"
    ),
    confirm: bool = typer.Option(
        False, "--confirm", help="proceed past the safety gate, explicitly accepting data loss"
    ),
    purge: bool = typer.Option(
        False,
        "--purge",
        help="hard-delete each hive's clone instead of soft-archiving it (still gated)",
    ),
):
    """Thin CLI wrapper over :func:`beadhive.host_retire.retire` — see that module's docstring
    for the full order + the guardrail contract (a host must never lose bead state, its own
    identity, or a stuck lease without operator consent). Lazy-imports ``host_retire`` (it
    imports this module back, for :func:`_require_hq_dir`/:func:`_require_host_id`/
    :func:`_scan_leases`) so the two stay import-cycle-safe, matching :func:`provision_cmd`."""
    from . import host_retire

    results = host_retire.retire(dry_run=dry_run, backup=backup, confirm=confirm, purge=purge)
    if not dry_run and any(r.status == "failed" for r in results):
        raise typer.Exit(1)


# ---- remove: drop an orphaned manifest from HQ (bh-salu) ------------------------------


@app.command(
    "rm",
    help="FLEET-WIDE: unregister <host_id>'s manifest from HQ (registry-only; touches no "
    "clone, worktree, or history). Gated against evicting a live host or removing THIS one "
    "by accident. Requires --confirm; --dry-run previews with zero mutation.",
)
@otel.trace_verb("host.rm")
def rm_cmd(
    host_id: str = typer.Argument(..., metavar="<host_id>", help="host_id from `bh host list`"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="print what would be unregistered and change nothing"
    ),
    confirm: bool = typer.Option(
        False, "--confirm", help="proceed with the FLEET-WIDE unregister of <host_id>"
    ),
    force: bool = typer.Option(
        False,
        "-f",
        "--force",
        help="release any live host lease(s) held by <host_id> first, and remove even when "
        "last-seen looks recent",
    ),
):
    """Drop ``hosts/<host_id>.yaml`` from Factory HQ and commit the removal — the verb bh-salu
    adds because none existed: ``host_id`` is minted once by ``bh config init`` and never
    regenerated or synced (:mod:`beadhive.host`'s module docstring), so a machine that gets
    wiped and rebuilt comes back under a DIFFERENT identity, and its old manifest would
    otherwise sit in HQ forever with no way to clear it (short of hand-editing
    ``hosts/<host_id>.yaml`` out of the clone).

    GATED on three independent axes, so it can never silently evict a live machine:

    * **live leases** — refused when ``host_id`` holds a live (unexpired) host lease for ANY
      registered hive; each is named, with a pointer at ``host lease release``.
      ``--force`` releases every one first (:func:`beadhive.host_lease.release`), then removes.
    * **recent last-seen** — refused when the manifest's own mtime (the same "last-seen" `list`
      shows) is more recent than :func:`_stale_after`'s threshold — the host is plausibly still
      alive even if it holds no lease right now. ``--force`` bypasses this too.
    * **intent** — refused without ``--confirm`` (bh-gbcw), the same gate `hive rm` carries,
      for the same reason: this is FLEET-WIDE registry truth. ``--confirm`` also covers the
      self-removal case that used to need its own ``--yes``; the two flags asked the same
      question ("do you mean it?") at different scopes, so bh-gbcw collapsed them into the
      one spelling the rest of the surface uses. ``--confirm`` does NOT bypass the live-lease
      or recency gates above — those are ``--force``'s job, and a self-removal of a live host
      still needs both.

    ``--dry-run`` reports the same verdict and the exact removal plan with zero mutation.
    """
    if dry_run and confirm:
        typer.echo("✗ pass one of --dry-run or --confirm, not both", err=True)
        raise typer.Exit(1)

    hq_dir = _require_hq_dir()
    try:
        manifest = hosts.load(hq_dir, host_id)
    except FileNotFoundError:
        typer.echo(f"✗ no host manifest for {host_id!r} in HQ", err=True)
        raise typer.Exit(1) from None
    except hosts.ManifestError as exc:
        typer.echo(f"✗ {exc}", err=True)
        raise typer.Exit(1) from None

    is_self = host_id == _this_host_id()
    cfg = config.load()
    held, unreadable = _scan_leases(hq_dir, cfg, host_id=host_id)
    for prefix, detail in unreadable:
        typer.echo(f"⚠ could not check {prefix}'s host lease — HQ unreachable: {detail}", err=True)

    if held and not force:
        named = ", ".join(f"{prefix} (expires {lease.expires_at})" for prefix, lease in held)
        typer.echo(
            f"✗ {host_id} still holds live host lease(s): {named}\n"
            f"  release them first (`{config.BINARY_ALIAS} host lease release <hive>` run from "
            f"that host, or `{config.BINARY_ALIAS} host lease release --all`), or pass --force "
            f"to release them and remove anyway.",
            err=True,
        )
        raise typer.Exit(1)

    path = hosts.manifest_path(hq_dir, host_id)
    if not _is_stale(path, _stale_after(cfg)) and not force:
        typer.echo(
            f"✗ {host_id} was last seen {_last_seen(path)} — recently enough it is plausibly "
            f"still alive; pass --force to remove anyway.",
            err=True,
        )
        raise typer.Exit(1)

    if dry_run or not confirm:
        self_note = " (THIS host's own manifest)" if is_self else ""
        typer.echo(f"{'DRY-RUN ' if dry_run else ''}would unregister {host_id}{self_note}:")
        typer.echo(f"  - drop {hosts.manifest_path(hq_dir, host_id)} from HQ")
        for prefix, _lease in held:
            typer.echo(f"  - release {prefix}'s lease first (--force)")
        typer.echo("  - leaves every clone, worktree, and all history untouched")
        if dry_run:
            typer.echo("\nDRY-RUN — no changes made.")
            return
        typer.echo(
            f"\n✗ refusing without --confirm — this is FLEET-WIDE registry truth"
            f"{' and it is your own roster entry' if is_self else ''}.",
            err=True,
        )
        raise typer.Exit(1)

    for prefix, _lease in held:
        try:
            outcome = host_lease.release("origin", prefix, host_id=host_id, cwd=hq_dir)
        except host_lease.HostLeaseRejected as exc:
            typer.echo(f"✗ could not release {prefix}'s lease for {host_id}: {exc}", err=True)
            raise typer.Exit(1) from None
        host_lease.cache(prefix, outcome, cwd=hq_dir)
        typer.echo(f"  ✓ released {prefix} (was held by {host_id})")

    removed = hosts.remove(hq_dir, host_id)
    hq._commit_if_dirty(hq_dir, f"chore(host): remove {host_id} ({manifest.label})")
    typer.echo(f"✓ removed {removed}")
