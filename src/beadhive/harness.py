"""Runtime install of the agent harnesses (bh-pc2a.36).

The image deliberately ships the RUNTIME (node) and the MEANS, not every harness. Claude Code's
package declares ``SEE LICENSE IN README.md`` rather than an SPDX identifier — it is proprietary,
and baking it in would make anyone who publishes the image a redistributor of it under Anthropic's
commercial terms. So the user installs it themselves, and accepts those terms as their own choice.
That is the same category as repowise (AGPL) in bh-pc2a.21, with a stronger reason.

Codex is NOT in that category — it declares Apache-2.0 and stays baked. It appears here anyway so
a user who wants a version other than the image's pin is not forced to accept ours.

Deliberately thin: this shells out to ``npm install -g``, the same command the image build used.
It is not a package manager, and it must not become one — no vendoring, no mirroring, no caching
of the proprietary package anywhere in this repo or its images. The point is not to move where it
is stored; it is to not distribute it at all.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass

import typer

from .run import run


@dataclass(frozen=True)
class Harness:
    """One installable harness: the binary to look for, and the npm package behind it."""

    name: str
    binary: str
    package: str
    license: str
    #: Environment variable carrying the image's validated pin, when there is one. The image
    #: still names ONE version even for what it does not ship, so `latest` cannot drift silently.
    version_env: str = ""
    #: True when the licence is not an SPDX identifier, i.e. the user is accepting bespoke terms.
    proprietary: bool = False


HARNESSES: dict[str, Harness] = {
    "claude": Harness(
        name="claude",
        binary="claude",
        package="@anthropic-ai/claude-code",
        license="SEE LICENSE IN README.md (proprietary — Anthropic's commercial terms)",
        version_env="BH_CLAUDE_CODE_VERSION",
        proprietary=True,
    ),
    "codex": Harness(
        name="codex",
        binary="codex",
        package="@openai/codex",
        license="Apache-2.0",
    ),
}


def _pinned_version(spec: Harness) -> str:
    """The image's validated version for *spec*, or "" when unknown (not running in the image)."""
    return os.environ.get(spec.version_env, "").strip() if spec.version_env else ""


def installed_path(spec: Harness) -> str | None:
    """Where *spec*'s binary resolves on PATH, or None."""
    return shutil.which(spec.binary)


def missing_hint(name: str) -> str:
    """The line to print when a harness is wanted but absent.

    Exists so the absence is SELF-EXPLANATORY. A bare ``claude: command not found`` is true and
    points nowhere — the bh-pc2a.33 failure mode, where a correct message sent the operator toward
    the wrong fix. Anything that resolves a harness should route through here.
    """
    spec = HARNESSES.get(name)
    if spec is None:
        return f"✗ unknown harness {name!r}. Known: {', '.join(sorted(HARNESSES))}"
    # Worded to be true on a HOST as well as in the image: on a host it is simply not installed,
    # and asserting "this image does not ship it" there would be a confident falsehood.
    return (
        f"✗ harness {name!r} is not installed.\n"
        f"  Install it with:  bh harness install {name}"
        + (
            f"\n  ({spec.package} is proprietary, so the Beadhive image deliberately does not"
            f"\n   ship it — you accept its terms by installing it yourself.)"
            if spec.proprietary
            else ""
        )
    )


def ls() -> None:
    """CLI: ``bh harness list`` — what is installed, what is available, and on whose terms."""
    typer.echo(f"{'HARNESS':<10} {'STATUS':<14} {'LICENSE':<12} PACKAGE")
    for name, spec in sorted(HARNESSES.items()):
        where = installed_path(spec)
        status = "installed" if where else "not installed"
        lic = "proprietary" if spec.proprietary else spec.license
        typer.echo(f"{name:<10} {status:<14} {lic:<12} {spec.package}")

    if any(s.proprietary for s in HARNESSES.values()):
        typer.echo(
            "\nProprietary harnesses are NOT shipped in the image — you install them yourself,"
            "\nwhich is what keeps this image redistributable. `bh harness install <name>`."
        )


def install(name: str, version: str = "", yes: bool = False) -> None:
    """CLI: ``bh harness install <name>`` — npm-install a harness at runtime.

    Idempotent: a harness already on PATH is reported and left alone, because re-installing would
    silently move a pinned version. Pass an explicit ``--version`` to change one.

    Names the licence BEFORE acting when the harness is proprietary. The whole reason this verb
    exists is that installing it is the USER's choice rather than ours, so it has to read as one —
    a silent install would recreate exactly the situation this was built to remove.
    """
    spec = HARNESSES.get(name)
    if spec is None:
        typer.echo(f"✗ unknown harness {name!r}. Known: {', '.join(sorted(HARNESSES))}", err=True)
        raise typer.Exit(1)

    if not version:
        where = installed_path(spec)
        if where:
            typer.echo(f"✓ {name} already installed ({where}) — nothing to do.")
            typer.echo(f"  Reinstall or change version with: bh harness install {name} --version X")
            return

    if shutil.which("npm") is None:
        typer.echo(
            "✗ npm is not available, so no harness can be installed here.\n"
            "  The agent image ships node; the core image deliberately does not.",
            err=True,
        )
        raise typer.Exit(1)

    wanted = version or _pinned_version(spec) or "latest"

    if spec.proprietary and not yes:
        typer.echo(
            f"{spec.package} is PROPRIETARY software, not open source.\n"
            f"  License: {spec.license}\n"
            f"  It is not shipped in this image; installing it means accepting those terms\n"
            f"  yourself. Nothing about this is done on your behalf."
        )
        typer.confirm(f"Install {spec.package}@{wanted}?", abort=True)

    typer.echo(f"installing {spec.package}@{wanted} …")
    result = run(
        ["npm", "install", "-g", "--no-fund", "--no-audit", f"{spec.package}@{wanted}"],
        check=False,
        capture=False,
    )
    if result.returncode != 0:
        typer.echo(f"✗ npm install failed (exit {result.returncode})", err=True)
        raise typer.Exit(result.returncode)

    where = installed_path(spec)
    typer.echo(f"✓ {name} installed{f' ({where})' if where else ''}")
