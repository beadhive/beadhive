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

from . import config

# ---- probe table ---------------------------------------------------------------

# Each entry: (name, which_binary, version_cmd)
# ``which_binary`` is the basename looked up via ``shutil.which``.
# ``version_cmd`` is the argv list used to get a version string (best-effort).
PROBE_TABLE: list[tuple[str, str, list[str]]] = [
    ("git-workspace", "git-workspace", ["git", "workspace", "--version"]),
    ("gh", "gh", ["gh", "--version"]),
    ("bd", "bd", ["bd", "--version"]),
    ("dolt", "dolt", ["dolt", "version"]),
]

# The container runtime is NOT universal — it follows ``dolt.backend`` in config.
# colima is a macOS affordance (VM to get a docker daemon); a Linux seat uses
# native docker, and a seat that never hosts the dolt sql-server (sync over
# git+ssh) sets ``backend: none`` and needs no runtime at all.
RUNTIME_PROBES: dict[str, tuple[str, str, list[str]]] = {
    "colima": ("colima", "colima", ["colima", "--version"]),
    "docker": ("docker", "docker", ["docker", "--version"]),
    "podman": ("podman", "podman", ["podman", "--version"]),
}


# ---- bd/dolt version floor (bh-gnqc) -------------------------------------------

# The last tagged bd release known to embed a dolt WITHOUT the #4770 fix. Every tagged
# release through this one pins the same dolt commit (1bf533220ab0, 2026-06-05) — 168 commits
# behind dolt v2.2.0 (2026-07-15), which is where the fix landed. Verified by decoding go.mod
# at v1.1.0 / v1.1.1 / v1.1.2; the release notes do not mention it. Raise this the moment a
# tagged bd pins dolt >= 2.2.0 (see bh-bmsg for the re-check log).
BD_LAST_RELEASE_WITHOUT_DOLT_FIX = (1, 1, 2)
DOLT_FIX_VERSION = "2.2.0"


def _bd_release_tuple(version_line: str | None) -> tuple[int, ...] | None:
    """The ``(major, minor, patch)`` of a TAGGED bd release, or ``None`` when the version is
    not a plain tag — a HEAD build, a dev build, or unparseable.

    ``None`` deliberately means "cannot judge", never "bad". A HEAD build is how an operator
    picks the fix up ahead of a release (that is exactly what this hive's Brewfile pins), so
    treating unparseable as suspect would warn the very people who already worked around it."""
    if not version_line:
        return None
    match = re.search(r"\bbd version (\d+)\.(\d+)\.(\d+)", version_line)
    return tuple(int(g) for g in match.groups()) if match else None


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
    """Derive the backend tag from config: ``dolt`` or ``jsonl``."""
    try:
        c = cfg if cfg is not None else config.load()
        backend = config.dolt_cfg(c).get("backend", "jsonl")
        return str(backend) if backend else "jsonl"
    except Exception:
        return "jsonl"


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

    if all_found:
        typer.echo("✓ setup complete — cache updated.")
    else:
        missing = [n for n, r in tools.items() if not r["found"]]
        typer.echo(
            f"✗ missing: {', '.join(missing)}\n"
            f"  Install the missing tools and re-run `{config.BINARY_ALIAS} setup check`.",
            err=True,
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
