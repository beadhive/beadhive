"""Runtime install of the agent harnesses (bh-pc2a.36, bh-hsus.1).

The image deliberately ships the MEANS, not every harness. Claude Code's package declares
``SEE LICENSE IN README.md`` rather than an SPDX identifier — it is proprietary, and baking it in
would make anyone who publishes the image a redistributor of it under Anthropic's commercial
terms. So the user installs it themselves, and accepts those terms as their own choice. That is
the same category as repowise (AGPL) in bh-pc2a.21, with a stronger reason. THIS POINT IS NOT
NPM-SPECIFIC: it survives bh-hsus.1's move off npm untouched, because it was always about
redistribution, never about the install mechanism — ``claude install`` is still the user accepting
Anthropic's terms, just via a different binary.

Codex is NOT in that category — it declares Apache-2.0 and stays baked into the agent image. It
appears here anyway so a user who wants a version other than the image's pin is not forced to
accept ours.

bh-hsus.1: this used to shell out to ``npm install -g``, mirroring what the image build did at the
time. That was never how a real, off-image install happens — the native installers (measured on
the operator's Mac: claude via its own installer under ``~/.local``, codex via a Homebrew cask)
already own the binary, and an npm install alongside them creates a SECOND copy whose precedence
on PATH is down to luck. Only claude gets a bh-driven install command now, and only for the one
case that actually needs one: bootstrapping a headless host that has no claude yet to update
itself with (``curl -fsSL https://claude.ai/install.sh | bash``). After that, claude owns its own
lifecycle — ``claude install <target>`` / ``claude update``, background auto-update on by default
— and bh gets out of the way. codex has no single command that works the same way on every plane
(brew is macOS-only; Linux has a GitHub release binary; Nix has ``nixpkgs#codex`` where the flake
plane is in play), and this module deliberately does not dispatch on plane to choose between them
(ADR Decision 5, bh-q160.12) — so ``bh dep install codex`` names the remedy instead of
attempting one. No vendoring, no mirroring, no caching of a proprietary binary anywhere in this
repo or its images — the point is to not distribute it at all, not to move where it is stored.
"""

from __future__ import annotations

import os
import shutil
import textwrap
from dataclasses import dataclass

import typer

from . import deps as deps_mod
from .run import run


@dataclass(frozen=True)
class InstallRoute:
    """How bh gets one harness onto PATH — remedy text with a command attached only where bh
    genuinely drives the install. ``cmd`` of ``None`` means bh does not install this tool; the
    operator (or a future ``deps-table``) reads ``note`` instead. Deliberately NOT branched by
    platform inside bh: where a route differs by plane (macOS vs Linux vs Nix), that lives in
    ``note`` as text, not as an if/elif this module resolves — see bh-hsus.1's design note and
    ADR Decision 5 (bh-q160.12)."""

    cmd: list[str] | None
    note: str
    proprietary: bool = False


@dataclass(frozen=True)
class Harness:
    """One harness bh knows about: the binary to look for, its licence, and how it gets
    installed."""

    name: str
    binary: str
    license: str
    install: InstallRoute
    #: Environment variable naming an install-target override (stable|latest|X.Y.Z), consulted
    #: ONLY on a genuine bootstrap (the harness is not yet on PATH — see install()'s unconditional
    #: idempotence guard). Explicit opt-in: it pins what a fresh host bootstraps TO, not an
    #: ongoing version lock — claude's own background auto-updater (on by default, see
    #: `claude doctor`) takes over from there, so setting this does not fight it. bh-hsus.1
    #: considered dropping it; kept because the image still wants to name ONE validated version
    #: for the first bootstrap rather than let a bare `latest` drift silently.
    version_env: str = ""


def _from_dep(dep: deps_mod.Dep) -> Harness:
    """One `Harness` record, derived from *dep* — `deps.DEPS` is the single source of truth
    (bh-hsus.5 collapses the mirror bh-hsus.3 left named and gated). This module keeps its own
    dataclass shape rather than handing out `deps.Dep` directly only because ``cmd`` here is a
    mutable ``list`` (`install()` appends one version argument to it) where
    `deps.InstallRoute.cmd` is an immutable ``tuple``."""
    route = dep.install
    assert route is not None  # only called for rows in has_install_route()
    return Harness(
        name=dep.name,
        binary=dep.binary,
        license=dep.license,
        install=InstallRoute(
            cmd=list(route.cmd) if route.cmd is not None else None,
            note=route.note,
            proprietary=route.proprietary,
        ),
        version_env=dep.version_env,
    )


#: Rows with a documented way onto PATH, whether or not bh drives it — derived from
#: `deps.has_install_route()`, NOT `deps.installable()` (bh-hsus.2 Evidence 1 / bh-hsus.5): the
#: two sets disagree on codex (a route bh does not drive, `install.cmd is None`), and this dict
#: exists so `bh harness list`/`missing_hint` can still name codex's remedy even though
#: `install()` itself refuses to run it.
HARNESSES: dict[str, Harness] = {dep.name: _from_dep(dep) for dep in deps_mod.has_install_route()}


def _pinned_version(spec: Harness) -> str:
    """The bootstrap-target override for *spec* from its version_env, or "" when unset/unknown."""
    return os.environ.get(spec.version_env, "").strip() if spec.version_env else ""


def installed_path(spec: Harness | deps_mod.Dep) -> str | None:
    """Where *spec*'s binary resolves on PATH, or None.

    Duck-typed on purpose: accepts a `Harness` (an install-route row) or a bare `deps.Dep` —
    `role.py`'s missing-binary guard (bh-hsus.5) passes a `Dep` for a seat-capable harness that
    has no install route at all (opencode), which is never a key in `HARNESSES`."""
    return shutil.which(spec.binary)


def missing_hint(name: str) -> str:
    """The line to print when a harness is wanted but absent.

    Exists so the absence is SELF-EXPLANATORY. A bare ``claude: command not found`` is true and
    points nowhere — the bh-pc2a.33 failure mode, where a correct message sent the operator toward
    the wrong fix. Anything that resolves a harness should route through here.

    bh-hsus.1 review: naming ``bh dep install <name>`` is only honest when ``install()``
    will actually attempt it. For a harness with ``cmd=None`` (codex), that command exits 1 —
    pointing there would be the bh-pc2a.33 failure mode reproduced by this very function, just
    one hop later. When there is no bh-driven install, the hint surfaces ``install.note``
    (the real remedy) instead of a command that refuses.

    bh-hsus.5: reads `deps.harnesses()` (every `kind="harness"` row) rather than `HARNESSES`
    (only rows with an install route), so a harness bh cannot install at all — opencode has
    neither `install` nor `auth` — still gets a real hint instead of being reported "unknown".
    `deps.harnesses()` is deliberately narrower than all of `deps.DEPS`: an infra dep like
    ``bd`` is not a harness and must not be answered here as if it were one.
    """
    dep = next((d for d in deps_mod.harnesses() if d.name == name), None)
    if dep is None:
        return f"✗ unknown harness {name!r}. Known: {', '.join(sorted(HARNESSES))}"

    # Worded to be true on a HOST as well as in the image: on a host it is simply not installed,
    # and asserting "this image does not ship it" there would be a confident falsehood.
    header = f"✗ harness {name!r} is not installed."

    if dep.install is None:
        return (
            header + "\n  bh has no known install route for it — install it yourself and make "
            "sure it is on PATH."
        )

    if dep.install.cmd is None:
        wrapped = textwrap.wrap(dep.install.note, width=76)
        return header + "\n" + "\n".join(f"  {line}" for line in wrapped)

    # bh-hsus.6: names the CANONICAL verb. `bh harness install` still works as an alias, but a
    # remedy that points at the alias teaches the noun this epic just demoted to a filter.
    lines = [header, f"  Install it with:  bh dep install {name}"]
    if dep.install.proprietary:
        lines.append(
            f"  License: {dep.license}."
            "\n  The Beadhive image deliberately does not ship it — you accept those terms by"
            "\n  installing it yourself."
        )
    return "\n".join(lines)


def _ensure_redirected_prefix() -> None:
    """Make a redirected ``~/.local`` writable before an installer writes into it (bh-dy4g).

    The image points ``~/.local`` at ``~/.claude/local`` so a natively-installed harness lands
    on the harness volume instead of being discarded on container recreate. That link DANGLES
    until something creates its target, and the target cannot be pre-created in the image — the
    volume mounts over ``~/.claude`` and masks anything built there. A dangling link is not a
    directory, so the installer's own ``mkdir -p ~/.local/bin`` fails outright:

        mkdir: cannot create directory '/home/bees/.local': File exists

    Measured, not predicted — it is what the first cut of bh-dy4g did.

    A named volume would hide this (Docker copies image directory contents into a fresh one),
    but ``BH_HARNESS_MOUNT`` may be a BIND mount, which copies nothing. So the fix cannot live in
    the image, and ``/etc/profile.d`` only runs for login shells — ``docker exec`` without ``-l``
    misses it. This verb always runs on the documented path, so it is where the guarantee belongs.

    No-op everywhere else: on a host ``~/.local`` is a real directory or absent, and the installer
    handles both. Only a dangling SYMLINK is repaired, so this cannot mask a genuine permissions
    or disk failure.
    """
    local = os.path.expanduser("~/.local")
    if not os.path.islink(local) or os.path.exists(local):
        return
    os.makedirs(os.path.realpath(local), exist_ok=True)


def install(name: str, version: str = "", yes: bool = False) -> None:
    """CLI: ``bh dep install <name>`` — bootstrap a tool bh does not ship.

    Idempotent UNCONDITIONALLY: a harness already on PATH is reported and left alone no matter
    what ``--version`` says (bh-hsus.1). The old implementation skipped this guard whenever a
    version was passed, which — combined with npm-installing next to an already-present native
    binary — quietly built a second, PATH-shadowing copy. A harness that already exists owns its
    own version from here (``claude install <target>`` / ``claude update``); bh's job stops at
    getting it onto PATH the first time.

    Names the licence BEFORE acting when the harness is proprietary. The whole reason this verb
    exists is that installing it is the USER's choice rather than ours, so it has to read as one —
    a silent install would recreate exactly the situation this was built to remove.
    """
    spec = HARNESSES.get(name)
    if spec is None:
        typer.echo(f"✗ unknown harness {name!r}. Known: {', '.join(sorted(HARNESSES))}", err=True)
        raise typer.Exit(1)

    where = installed_path(spec)
    if where:
        typer.echo(f"✓ {name} already installed ({where}) — nothing to do.")
        typer.echo(f"  {spec.install.note}")
        return

    if spec.install.cmd is None:
        typer.echo(f"✗ bh does not install {name}.\n  {spec.install.note}", err=True)
        raise typer.Exit(1)

    wanted = version or _pinned_version(spec)

    if spec.install.proprietary and not yes:
        typer.echo(
            f"{name} is PROPRIETARY software, not open source.\n"
            f"  License: {spec.license}\n"
            f"  It is not shipped in this image; installing it means accepting those terms\n"
            f"  yourself. Nothing about this is done on your behalf."
        )
        typer.confirm(f"Bootstrap {name}{f'@{wanted}' if wanted else ''}?", abort=True)

    _ensure_redirected_prefix()

    typer.echo(f"bootstrapping {name}{f'@{wanted}' if wanted else ''} …")
    cmd = [*spec.install.cmd, wanted] if wanted else spec.install.cmd
    result = run(cmd, check=False, capture=False)
    if result.returncode != 0:
        typer.echo(f"✗ install failed (exit {result.returncode})", err=True)
        raise typer.Exit(result.returncode)

    where = installed_path(spec)
    typer.echo(f"✓ {name} installed{f' ({where})' if where else ''}")
    typer.echo(f"  {spec.install.note}")
