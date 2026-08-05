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
(ADR Decision 5, bh-q160.12) — so ``bh harness install codex`` names the remedy instead of
attempting one. No vendoring, no mirroring, no caching of a proprietary binary anywhere in this
repo or its images — the point is to not distribute it at all, not to move where it is stored.
"""

from __future__ import annotations

import os
import shutil
import textwrap
from dataclasses import dataclass

import typer

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


HARNESSES: dict[str, Harness] = {
    "claude": Harness(
        name="claude",
        binary="claude",
        license="SEE LICENSE IN README.md (proprietary — Anthropic's commercial terms)",
        install=InstallRoute(
            cmd=["bash", "-c", 'curl -fsSL https://claude.ai/install.sh | bash -s -- "$@"', "bash"],
            note=(
                "bh only bootstraps a host with no claude on it yet. After that: "
                "`claude install <stable|latest|X.Y.Z>` to change version, `claude update` to "
                "check for updates — background auto-update is on by default (`claude doctor`)."
            ),
            proprietary=True,
        ),
        version_env="BH_CLAUDE_CODE_VERSION",
    ),
    "codex": Harness(
        name="codex",
        binary="codex",
        license="Apache-2.0",
        install=InstallRoute(
            cmd=None,
            note=(
                "brew install --cask codex (macOS) · a release binary from "
                "https://github.com/openai/codex/releases (Linux) · `nixpkgs#codex` where Nix is "
                "the plane (bh-q160.12) — bh does not drive any of these itself."
            ),
        ),
    ),
}


def _pinned_version(spec: Harness) -> str:
    """The bootstrap-target override for *spec* from its version_env, or "" when unset/unknown."""
    return os.environ.get(spec.version_env, "").strip() if spec.version_env else ""


def installed_path(spec: Harness) -> str | None:
    """Where *spec*'s binary resolves on PATH, or None."""
    return shutil.which(spec.binary)


def missing_hint(name: str) -> str:
    """The line to print when a harness is wanted but absent.

    Exists so the absence is SELF-EXPLANATORY. A bare ``claude: command not found`` is true and
    points nowhere — the bh-pc2a.33 failure mode, where a correct message sent the operator toward
    the wrong fix. Anything that resolves a harness should route through here.

    bh-hsus.1 review: naming ``bh harness install <name>`` is only honest when ``install()``
    will actually attempt it. For a harness with ``cmd=None`` (codex), that command exits 1 —
    pointing there would be the bh-pc2a.33 failure mode reproduced by this very function, just
    one hop later. When there is no bh-driven install, the hint surfaces ``install.note``
    (the real remedy) instead of a command that refuses.
    """
    spec = HARNESSES.get(name)
    if spec is None:
        return f"✗ unknown harness {name!r}. Known: {', '.join(sorted(HARNESSES))}"

    # Worded to be true on a HOST as well as in the image: on a host it is simply not installed,
    # and asserting "this image does not ship it" there would be a confident falsehood.
    header = f"✗ harness {name!r} is not installed."

    if spec.install.cmd is None:
        wrapped = textwrap.wrap(spec.install.note, width=76)
        return header + "\n" + "\n".join(f"  {line}" for line in wrapped)

    lines = [header, f"  Install it with:  bh harness install {name}"]
    if spec.install.proprietary:
        lines.append(
            f"  License: {spec.license}."
            "\n  The Beadhive image deliberately does not ship it — you accept those terms by"
            "\n  installing it yourself."
        )
    return "\n".join(lines)


def ls() -> None:
    """CLI: ``bh harness list`` — what is installed, what is available, and on whose terms.

    bh-hsus.1 review: ``install.note`` is a 150-200 char remedy paragraph, not a table cell — a
    fixed-width REMEDY column blew past any real terminal width. The table stays to the fields
    that ARE short (status, licence); the notes print below it, one harness at a time, wrapped to
    80 columns.
    """
    typer.echo(f"{'HARNESS':<10} {'STATUS':<14} LICENSE")
    for name, spec in sorted(HARNESSES.items()):
        where = installed_path(spec)
        status = "installed" if where else "not installed"
        lic = "proprietary" if spec.install.proprietary else spec.license
        typer.echo(f"{name:<10} {status:<14} {lic}")

    for name, spec in sorted(HARNESSES.items()):
        typer.echo(f"\n{name}:")
        for line in textwrap.wrap(spec.install.note, width=78):
            typer.echo(f"  {line}")

    if any(s.install.proprietary for s in HARNESSES.values()):
        typer.echo(
            "\nProprietary harnesses are NOT shipped in the image — you install them yourself,"
            "\nwhich is what keeps this image redistributable. `bh harness install <name>`."
        )


def install(name: str, version: str = "", yes: bool = False) -> None:
    """CLI: ``bh harness install <name>`` — bootstrap a harness bh does not ship.

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

    typer.echo(f"bootstrapping {name}{f'@{wanted}' if wanted else ''} …")
    cmd = [*spec.install.cmd, wanted] if wanted else spec.install.cmd
    result = run(cmd, check=False, capture=False)
    if result.returncode != 0:
        typer.echo(f"✗ install failed (exit {result.returncode})", err=True)
        raise typer.Exit(result.returncode)

    where = installed_path(spec)
    typer.echo(f"✓ {name} installed{f' ({where})' if where else ''}")
    typer.echo(f"  {spec.install.note}")
