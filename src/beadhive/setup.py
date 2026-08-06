"""ws setup — post-installation dependency gate.

Probes for the tools ws delegates to (git-workspace, gh, bd, dolt, plus the
container runtime matching ``dolt.backend`` — colima/docker/podman, none for
backends that need no runtime), records the result in ~/.ws/setup-state.json
on success, and surfaces the gate
check that ``_root`` in cli.py uses to guard every verb except
setup / config / doctor / --version / --help.

Cache schema (``~/.ws/setup-state.json``)::

    {
      "setup": true,
      "checked_at": "<iso8601>",
      "os": "<Darwin|Linux|…>",
      "backend": "<dolt|jsonl>",
      "image": {"tag": "<str>", "target": "<str>", "build_sha": "<str>"},   # in-image only
      "tools": {
        "<name>": {"found": <bool>, "version": "<str | null>"}
      }
    }

The OS + backend tag lets later OS/backend variants extend the probe table
without changing the gate contract.

Inside a Beadhive image the components are already known: the build writes
``/etc/beadhive/image-manifest.json`` naming every component, its version, and the
image tag + build SHA that validated them together.  ``run_check`` prefers that
manifest over live probing, so the in-container gate is instant and reports the
*validated* set rather than whatever happens to be on PATH.  With no manifest —
every non-container host — the probe path below runs exactly as before.
"""

from __future__ import annotations

import json
import platform
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer

from . import config, deps, dolt_health, store_locator
from .run import run

# ---- probe table ---------------------------------------------------------------

# DERIVED, not declared (bh-hsus.3). `deps.DEPS` is the ONE table these two views project
# from; editing a row there is what changes what `bh setup check` probes. Both keep their
# historical ``(name, which_binary, version_cmd)`` shape and their historical ORDER, because
# `bh setup check` prints in table order and callers unpack the triple.
#
# ``which_binary`` is the basename looked up via ``shutil.which``; ``version_cmd`` is the argv
# list used to get a version string (best-effort). The two genuinely differ — git-workspace's
# binary is ``git-workspace`` but its version comes from ``git workspace --version``.


def probe_row(dep: deps.Dep) -> tuple[str, str, list[str]]:
    """One dep as the ``(name, which_binary, version_cmd)`` triple this module has always used —
    the argv `probe_one` takes, so any caller needing an ad-hoc probe looks the ROW up instead of
    re-typing it (see `doctor._bd_dolt_fix_warnings`)."""
    return (dep.name, dep.binary, list(dep.version_cmd))


#: Required unconditionally: ``[d for d in deps.DEPS if d.required == "always"]``.
PROBE_TABLE: list[tuple[str, str, list[str]]] = [probe_row(d) for d in deps.always_required()]

# The container runtime is NOT universal — it follows ``dolt.backend`` in config, which is the
# ``store-runtime`` group's selector. colima is a macOS affordance (VM to get a docker daemon);
# a Linux seat uses native docker, and a seat that never hosts the dolt sql-server (sync over
# git+ssh) sets ``backend: none`` and needs no runtime at all. Keyed by the selector VALUE,
# which for this group is the dep name.
RUNTIME_PROBES: dict[str, tuple[str, str, list[str]]] = {
    d.name: probe_row(d) for d in deps.group_members("store-runtime")
}


# ---- bd/dolt version floor (bh-gnqc) -------------------------------------------

# The last tagged bd release known to embed a dolt WITHOUT the #4770 fix. Every tagged
# release through this one pins the same dolt commit (1bf533220ab0, 2026-06-05) — 168 commits
# behind dolt v2.2.0 (2026-07-15), which is where the fix landed. Verified by decoding go.mod
# at v1.1.0 / v1.1.1 / v1.1.2; the release notes do not mention it. Raise this the moment a
# tagged bd pins dolt >= 2.2.0 (see bh-bmsg for the re-check log).
BD_LAST_RELEASE_WITHOUT_DOLT_FIX = (1, 1, 2)
DOLT_FIX_VERSION = "2.2.0"


# Markers meaning "a build BETWEEN tags", matched as whole words so a commit hash that happens to
# contain the letters (`1.1.0 (abc123rc)`) is not read as a release candidate — a false match here
# SILENCES a real warning, which is the costlier direction to be wrong in.
_NOT_A_TAGGED_RELEASE = re.compile(r"\b(dev|head|dirty|snapshot|pre|rc\d*|alpha|beta)\b", re.I)


def _bd_release_tuple(version_line: str | None) -> tuple[int, ...] | None:
    """The ``(major, minor, patch)`` of a TAGGED bd release, or ``None`` when the version is
    not a plain tag — a HEAD build, a dev build, or unparseable.

    ``None`` deliberately means "cannot judge", never "bad". A HEAD build is how an operator
    picks the fix up ahead of a release (that is exactly what this hive's Brewfile pins), so
    treating unparseable as suspect would warn the very people who already worked around it.

    THE SUFFIX IS PART OF THE VERSION (bh-1drz). This used to match the numeric core and discard
    whatever followed, so the nixpkgs HEAD build — which reports ``bd version 1.1.0 (dev)`` — was
    judged a tagged 1.1.0 and warned. The Homebrew HEAD build escaped only by an accident of
    formatting: it prints ``HEAD-af076b6``, which has no leading digits, so the regex missed it
    and this returned ``None`` for the right reason by luck. Since ADR Decision 5 makes the Nix
    flake the supported local-install toolchain, and ``flake.nix``'s ``beadsHead`` override
    exists precisely to supply a HEAD bd that CARRIES the fix, the old behaviour warned on every
    provisioned Linux host — the exact case the paragraph above forbids.

    A parenthetical commit hash (``1.1.0 (abc123)``) is still a tagged release and still judged;
    only a between-tags marker disqualifies."""
    if not version_line:
        return None
    match = re.search(r"\bbd version (\d+)\.(\d+)\.(\d+)(\S*)", version_line)
    if not match:
        return None
    if match.group(4):
        return None  # a suffix welded to the number: 1.1.0.dev0, 1.1.0-rc1, 1.1.0+build
    if _NOT_A_TAGGED_RELEASE.search(version_line[match.end() :]):
        return None  # a marker word after it: `1.1.0 (dev)`
    return tuple(int(g) for g in match.groups()[:3])


def dolt_fix_advisory(bd_version: str | None) -> str | None:
    """A one-paragraph warning when `bd_version` is a tagged release whose EMBEDDED dolt
    predates the #4770 fix — else ``None``.

    Why this exists (bh-gnqc): dolt is statically compiled into bd, so the dolt version is
    frozen at bd build time and is NOT visible from ``bd version``. On an affected build
    ``bd dolt pull`` can hang INDEFINITELY on a large store (measured: 170s then killed, vs
    3.2s on a fixed build) — and bh's multi-host sync runs exactly that. Nothing else in bh
    states a bd version requirement, so without this an operator meets the bug as an
    unexplained hang.

    Advisory, never blocking: the hang needs a large store to bite, so a small or new hive may
    never see it, and hard-failing setup over a probabilistic issue would be worse than the
    issue. Server mode is offered as the second escape because it moves the dolt version out
    of bd entirely — the engine that does the transport is then the one the operator runs."""
    release = _bd_release_tuple(bd_version)
    if release is None or release > BD_LAST_RELEASE_WITHOUT_DOLT_FIX:
        return None
    shown = ".".join(str(n) for n in release)
    return (
        f"⚠ bd {shown} embeds dolt < {DOLT_FIX_VERSION} — `bd dolt pull` can hang indefinitely\n"
        f"    on a large store (upstream beads#4770). {config.BINARY_ALIAS}'s multi-host sync "
        f"runs that pull.\n"
        f"    dolt is compiled into bd, so upgrading the standalone dolt CLI does NOT help.\n"
        f"    Escapes: install bd from HEAD (`brew install beads --HEAD`), or run bd against an\n"
        f"    external dolt sql-server >= {DOLT_FIX_VERSION} (`bd init --server`), which takes "
        f"the dolt\n"
        f"    version out of bd's release cadence entirely."
    )


# ---- dolt server liveness (bh-areg.3) -------------------------------------------

# "Nothing in bh knows a store engine can be DOWN" — embedded mode has no liveness question at
# all (in-process engine); mode (a) — bd's shared `dolt sql-server` — can be down, wedged, or on
# the wrong port. Advisory only, copying `dolt_fix_advisory`'s shape exactly (informs without
# blocking): `setup check` probes for BINARIES, not a moving operational fact, so a down server
# must never fail the gate the way a missing tool does. Silent (returns None) when the current
# directory isn't inside a bd-managed hive, or that hive is embedded — an unmigrated hive (the
# common case today) sees byte-identical `setup check` output (bh-areg.3's own acceptance bar).


def dolt_server_advisory(cwd=None) -> str | None:
    """A warning (never a gate failure) when the hive at *cwd* (default: CWD) is mode-(a)
    server-mode and its shared dolt sql-server is unreachable, or its persisted `dolt_mode`
    has drifted from what's actually active this run. See `dolt_health` for the probe/mismatch
    mechanics — this is purely the advisory-message shape, matching `dolt_fix_advisory`."""
    res = run(["git", "rev-parse", "--show-toplevel"], check=False, capture=True, cwd=cwd)
    if res.returncode != 0:
        return None
    root = Path(res.stdout.strip())
    mismatch = dolt_health.mismatch_reason(root)
    if mismatch:
        return f"⚠ {mismatch}"
    if store_locator.dolt_mode(root) != "server":
        return None
    probe = dolt_health.probe_shared_server()
    if probe.reachable:
        return None
    return (
        f"⚠ dolt shared server unreachable: {probe.detail}\n"
        "    bd verbs against this hive will hard-fail until it's back — start it with "
        "`bd dolt start` (bh does not auto-start it or fall back to embedded)."
    )


def probe_one(name: str, which_binary: str, version_cmd: list[str]) -> dict[str, Any]:
    """Probe a single tool: check presence via ``shutil.which``, then fetch version.

    Returns ``{"found": bool, "version": str | None}``.

    Presence is determined by ``shutil.which(which_binary)`` — a missing binary
    immediately returns ``found=False``.  When found, ``version_cmd`` is run to
    get the first line of stdout/stderr; a failure there still returns ``found=True``
    with ``version=None``.

    Probe helpers are intentionally importable from this module so doctor.py can
    reuse them without duplicating the subprocess logic.
    """
    if shutil.which(which_binary) is None:
        return {"found": False, "version": None}

    try:
        result = subprocess.run(
            version_cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        out = (result.stdout or result.stderr or "").strip()
        version = out.splitlines()[0] if out else None
        return {"found": True, "version": version}
    except (OSError, subprocess.TimeoutExpired):
        return {"found": True, "version": None}


def probe_tools() -> dict[str, dict[str, Any]]:
    """Run every entry in PROBE_TABLE, plus the runtime probe for the configured
    ``dolt.backend`` (colima/docker/podman; ``none``/jsonl skip it).

    Importable by doctor.py or any other module that needs to surface tool
    availability without reimplementing the probe logic.
    """
    table = list(PROBE_TABLE)
    runtime = RUNTIME_PROBES.get(_backend_tag())
    if runtime:
        table.append(runtime)
    return {name: probe_one(name, which_bin, vcmd) for name, which_bin, vcmd in table}


# ---- image manifest ------------------------------------------------------------

# Written by the image build (see docker/write-manifest.sh). Only ever present inside a
# Beadhive image; ``BH_IMAGE_MANIFEST`` relocates it for tests and for a non-standard image.
IMAGE_MANIFEST_PATH = Path("/etc/beadhive/image-manifest.json")


def image_manifest_path() -> Path:
    """Path to the image component manifest (``BH_IMAGE_MANIFEST`` overrides)."""
    override = config.image_manifest_override()
    return Path(override).expanduser() if override else IMAGE_MANIFEST_PATH


def read_image_manifest() -> dict[str, Any] | None:
    """Read the image component manifest, or ``None`` when absent/unusable.

    Returning ``None`` is the "not in an image" signal that keeps the probe path the
    default: a missing, unreadable, malformed, or component-less manifest all fall back
    rather than half-report.
    """
    p = image_manifest_path()
    if not p.exists():
        return None
    try:
        manifest = json.loads(p.read_text())
    except Exception:
        return None
    if not isinstance(manifest, dict) or not isinstance(manifest.get("components"), list):
        return None
    return manifest


def tools_from_manifest(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Project a manifest's components onto the same ``tools`` dict ``probe_tools`` returns.

    The manifest is the build's assertion that these versions shipped together, so every
    entry is ``found`` — verifying it would mean re-probing, which is the whole point of
    having the manifest.
    """
    return {
        c["name"]: {"found": True, "version": c.get("version")}
        for c in manifest["components"]
        if isinstance(c, dict) and c.get("name")
    }


def image_block(manifest: dict[str, Any] | None) -> dict[str, Any] | None:
    """The manifest's ``image`` block (tag / target / build SHA) for the cache, or ``None``."""
    if not manifest:
        return None
    image = manifest.get("image")
    return image if isinstance(image, dict) else None


def image_ref(cache: dict[str, Any] | None) -> str | None:
    """Render a cached image block as ``<tag> (<build_sha>)`` for display."""
    image = (cache or {}).get("image")
    if not isinstance(image, dict) or not image.get("tag"):
        return None
    sha = image.get("build_sha")
    return f"{image['tag']} ({sha})" if sha else str(image["tag"])


# ---- cache I/O -----------------------------------------------------------------


def setup_state_path() -> Path:
    """Path to the setup cache file (``~/.ws/setup-state.json``)."""
    return config.home() / "setup-state.json"


def _backend_tag(cfg: dict | None = None) -> str:
    """The ``dolt.backend`` value, which is both the cache's ``backend`` tag and the
    ``store-runtime`` group's selector — one derivation, not two (bh-hsus.3)."""
    return deps.GROUPS["store-runtime"].select(cfg)


def read_cache() -> dict[str, Any] | None:
    """Read and parse the setup cache. Returns ``None`` when absent or unreadable."""
    p = setup_state_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _write_cache(
    tools: dict[str, dict[str, Any]],
    success: bool,
    image: dict[str, Any] | None = None,
) -> None:
    """Write the setup-state cache, creating ``~/.ws/`` if needed.

    ``image`` tags the cache with the manifest's image block so ``bh setup show`` can name
    which image validated this combination; it is absent on a non-container host.
    """
    state: dict[str, Any] = {
        "setup": success,
        "checked_at": datetime.now(tz=UTC).isoformat(),
        "os": platform.system(),
        "backend": _backend_tag(),
        "tools": tools,
    }
    if image:
        state["image"] = image
    p = setup_state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2))


# ---- gate helper ---------------------------------------------------------------


def is_setup_complete() -> bool:
    """Return True iff the cache exists and ``setup == true``.

    Used by the ``_root`` gate in cli.py — must be cheap (one file read).
    Returns ``False`` on any read/parse error to keep the gate conservative.
    """
    cache = read_cache()
    return bool(cache and cache.get("setup") is True)


# ---- command implementations ---------------------------------------------------


def _missing_remedy(missing: list[str], manifest) -> str:
    """The advice line under ``✗ missing: …`` — which is sharply different inside the image.

    On a host "install them" is right. Inside a Beadhive image it is never right, and for a
    container RUNTIME it is actively harmful (bh-pc2a.33): bh-pc2a.6 established that a container
    does not drive one and the host socket is deliberately NOT mounted, so an operator following
    generic advice is led straight to the one thing the design forbids. This was not theoretical —
    a stale image whose bh predated the manifest reader fell back to probing, reported
    ``✗ missing: docker``, and gated off every ``bh hive`` / ``bh bd`` verb behind that message.

    Reaching the probe path in-image AT ALL means the manifest is absent or unreadable, which is a
    defect in the IMAGE rather than in the operator's setup. Say that instead.
    """
    from .compose import in_container  # lazy: setup is imported early, compose pulls in typer/run

    if not in_container():
        return f"  Install the missing tools and re-run `{config.BINARY_ALIAS} setup check`."

    lines = ["  This is a Beadhive image — an IMAGE defect, not something to install in here."]
    if manifest is None:
        lines.append(
            f"  No readable component manifest at {image_manifest_path()}, so bh fell back to"
            " probing. The image predates the manifest or was built wrong — rebake it"
            " (`just image-local <target>`) rather than installing anything."
        )
    runtimes = sorted(set(missing) & set(RUNTIME_PROBES))
    if runtimes:
        lines.append(
            f"  Do NOT install {'/'.join(runtimes)} here and do NOT mount the host docker socket:"
            " a container never drives a container runtime (bh-pc2a.6), and mounting the socket"
            " would hand this container host root."
        )
    return "\n".join(lines)


def run_check() -> None:
    """Implement ``ws setup check``: probe all deps and cache the result.

    Inside a Beadhive image the component manifest replaces probing entirely — no
    ``--version`` subprocess runs at all.  Everywhere else this is the unchanged probe path.
    Exits 1 when one or more required deps are missing.  Re-running refreshes the cache even
    if it was previously passing.
    """
    manifest = read_image_manifest()
    if manifest is not None:
        typer.echo(f"Reading image manifest ({image_manifest_path()}) — skipping probes.")
        tools = tools_from_manifest(manifest)
    else:
        typer.echo("Checking post-ws dependencies…")
        tools = probe_tools()

    all_found = True
    for name, result in tools.items():
        status = "✓" if result["found"] else "✗"
        version_note = f"  ({result['version']})" if result["version"] else ""
        typer.echo(f"  {status} {name}{version_note}")
        if not result["found"]:
            all_found = False

    _write_cache(tools, success=all_found, image=image_block(manifest))

    # Advisory, not a gate (bh-gnqc): an affected bd is PRESENT and functional, so `found` stays
    # true and setup still passes. It is printed after the table so it reads as a note on the bd
    # line above rather than a failure of the check.
    advisory = dolt_fix_advisory((tools.get("bd") or {}).get("version"))
    if advisory:
        typer.echo(f"\n{advisory}", err=True)

    # Advisory, not a gate (bh-areg.3): a down/unreachable server is an operational fact that
    # changes hour to hour, not a missing binary — `found` stays out of it entirely. Skipped
    # in-image, same as the tool probes above: the manifest path must run ZERO subprocesses
    # (`test_setup_manifest.py`'s own contract), and a baked image is not where an operator
    # would look for THIS hive's server state anyway.
    if manifest is None:
        server_advisory = dolt_server_advisory()
        if server_advisory:
            typer.echo(f"\n{server_advisory}", err=True)

    if all_found:
        typer.echo("✓ setup complete — cache updated.")
    else:
        missing = [n for n, r in tools.items() if not r["found"]]
        typer.echo(
            f"✗ missing: {', '.join(missing)}\n{_missing_remedy(missing, manifest)}", err=True
        )
        raise typer.Exit(1)


def run_show() -> None:
    """Implement ``bh setup show``: report cached status without re-probing."""
    cache = read_cache()
    if cache is None:
        typer.echo(
            f"setup: not checked yet — run `{config.BINARY_ALIAS} setup check` "
            "to probe dependencies.",
            err=True,
        )
        raise typer.Exit(1)

    status = "complete" if cache.get("setup") else "incomplete"
    typer.echo(f"setup: {status}")
    typer.echo(f"  checked_at: {cache.get('checked_at', '(unknown)')}")
    typer.echo(f"  os:         {cache.get('os', '(unknown)')}")
    typer.echo(f"  backend:    {cache.get('backend', '(unknown)')}")
    ref = image_ref(cache)
    if ref:
        typer.echo(f"  image:      {ref}")
    typer.echo("  tools:")
    for name, result in (cache.get("tools") or {}).items():
        mark = "✓" if result.get("found") else "✗"
        ver = result.get("version") or "(version unknown)"
        typer.echo(f"    {mark} {name}: {ver if result.get('found') else 'not found'}")
