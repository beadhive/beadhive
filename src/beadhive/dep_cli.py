"""`bh dep` — the one user-facing surface over `deps.DEPS` (bh-hsus.6).

    bh dep list [--kind harness|infra] [--missing]
    bh dep show <name>
    bh dep install <name>
    bh dep auth [<name>] [--check]

WHY THE NAME IS AN ARGUMENT, NOT A NAMESPACE. `bh plugin <name>` is a MOUNT POINT for a tool's
own sub-app, and it keeps its three genuinely-optional integrations (orca, observaloop, hitch)
unchanged. Verbs that operate ACROSS the table take the name as an argument instead: giving every
row a `bh plugin <name>` namespace would make `bh plugin gh` an empty one, and an empty namespace
invites exactly the wrapper `credentials`' own docstring forbids ("this must not grow into a
wrapper around them").

WHY "HARNESS" IS A FILTER, NOT A TOP-LEVEL NOUN. `bh harness auth` had to probe `gh`, which runs
no seat and is not a harness — a verb named after one kind of member of the set it operates on.
`bh harness list|install|auth` survive here as thin ALIASES (bh-q160.3's acceptance and the
documented adoption sequences name them), each one calling the same function `bh dep` does with
the harness filter applied.

`bh setup check` is untouched by this module: it is still the PRESENCE gate, stage 1 only, still
contractually zero-subprocess on the in-image manifest path. Nothing here folds an auth probe into
it — the two stages stay separate and the TABLE is what they share.
"""

from __future__ import annotations

import textwrap

import typer

from . import deps as deps_mod

app = typer.Typer(
    no_args_is_help=True,
    help="External tools bh depends on: list the table, install a row, authenticate a row.",
)

#: Column widths for `bh dep list`. `required` is the wide one — it prints the group NAME plus
#: whether config currently selects that row, which is the fact an operator actually needs.
_NAME_W, _KIND_W, _REQ_W = 16, 8, 22


def _lookup(name: str) -> deps_mod.Dep:
    """The row called *name*, or a friendly exit-2. Unlike `deps.by_name`'s KeyError, an unknown
    name HERE is user input, so it gets the known list rather than a traceback."""
    try:
        return deps_mod.by_name(name)
    except KeyError:
        known = ", ".join(d.name for d in deps_mod.DEPS)
        typer.echo(f"✗ unknown dep {name!r} — one of: {known}", err=True)
        raise typer.Exit(2) from None


def _kinds() -> list[str]:
    """The kinds actually present in the table — derived, so a new kind needs no edit here."""
    return sorted({d.kind for d in deps_mod.DEPS})


def _select(kind: str) -> list[deps_mod.Dep]:
    if not kind:
        return list(deps_mod.DEPS)
    if kind not in _kinds():
        typer.echo(f"✗ unknown kind {kind!r} — one of: {', '.join(_kinds())}", err=True)
        raise typer.Exit(2)
    return [d for d in deps_mod.DEPS if d.kind == kind]


def _required_cell(dep: deps_mod.Dep, cfg: dict | None) -> str:
    """``always`` / ``never`` / ``<group> (selected)``.

    A group row says which group it is in AND whether config currently picks it, because those
    are different questions and the operator is usually asking the second one."""
    if dep.required in (deps_mod.ALWAYS, deps_mod.NEVER):
        return dep.required
    return f"{dep.group} (selected)" if deps_mod.is_required(dep, cfg) else dep.group


def _install_cell(dep: deps_mod.Dep) -> str:
    """``bh dep install`` / ``manual`` / ``—``.

    THE TWO PREDICATES ARE DIFFERENT SETS and conflating them is the bug this epic has already
    produced three times: ``has_install_route()`` is {claude, codex} (bh knows how it arrives),
    ``installable()`` is {claude} (bh will actually run it). This column reads the narrower one
    before it promises a command, so it never names `bh dep install codex` — which exits 1."""
    if dep.install is None:
        return "—"
    return "bh dep install" if dep.install.cmd else "manual"


def _auth_state(dep: deps_mod.Dep, present: bool) -> str:
    """``✓`` / ``✗`` / ``—`` for a row's stage-2 gate.

    Never probes a row that is absent (there is nothing to authenticate) or that has no
    credential at all — so the only shell-out a bare `bh dep list` can make is for a row that is
    installed AND gated, which today is at most gh, claude and codex."""
    if dep.auth is None:
        return "—"
    if not present:
        return "✗"
    from . import credentials

    return "✓" if credentials.probe(dep).authenticated else "✗"


@app.command("list", help="the whole dependency table: kind, requirement, presence, credential.")
def ls(
    kind: str = typer.Option(
        "", "--kind", help="only rows of this kind (harness|infra)", metavar="KIND"
    ),
    missing: bool = typer.Option(
        False,
        "--missing",
        help="only rows this host does not satisfy — absent, or present without a credential.",
    ),
) -> None:
    """Presence is `setup.probe_one` (stage 1, the ONE detection mechanism); the credential
    column is the stage-2 probe, run only for rows that are both present and gated."""
    rows = _select(kind)
    typer.echo(
        f"{'NAME':<{_NAME_W}} {'KIND':<{_KIND_W}} {'REQUIRED':<{_REQ_W}} "
        f"{'PRESENT':<8} {'AUTH':<5} INSTALL"
    )
    shown: list[deps_mod.Dep] = []
    for dep in rows:
        present = deps_mod.present(dep)
        auth = _auth_state(dep, present)
        if missing and present and auth != "✗":
            continue
        shown.append(dep)
        typer.echo(
            f"{dep.name:<{_NAME_W}} {dep.kind:<{_KIND_W}} {_required_cell(dep, None):<{_REQ_W}} "
            f"{'✓' if present else '✗':<8} {auth:<5} {_install_cell(dep)}"
        )

    if missing and not shown:
        typer.echo("\n✓ every row in the table is satisfied on this host.")
        return

    # bh-hsus.1 review: `install.note` is a 150-200 char remedy paragraph, not a table cell — a
    # fixed-width REMEDY column blew past any real terminal width. The notes print below the
    # table, one row at a time, wrapped.
    for dep in shown:
        if dep.install is not None and dep.install.note:
            typer.echo(f"\n{dep.name}:")
            if dep.license:
                typer.echo(f"  license: {dep.license}")
            for line in textwrap.wrap(dep.install.note, width=78):
                typer.echo(f"  {line}")

    if any(d.install is not None and d.install.proprietary for d in shown):
        typer.echo(
            "\nProprietary tools are NOT shipped in the image — you install them yourself,"
            "\nwhich is what keeps this image redistributable. `bh dep install <name>`."
        )


@app.command("show", help="everything the table knows about one dep.")
def show(name: str = typer.Argument(..., help="dep name (see `bh dep list`)")) -> None:
    dep = _lookup(name)
    present = deps_mod.present(dep)

    typer.echo(f"{dep.name}")
    typer.echo(f"  kind       {dep.kind}")
    typer.echo(f"  binary     {dep.binary}")
    typer.echo(f"  version    {' '.join(dep.version_cmd)}")
    typer.echo(f"  required   {_required_cell(dep, None)}")
    if dep.group:
        typer.echo(f"             selector: {deps_mod.GROUPS[dep.group].selector}")
    typer.echo(f"  runs seats {'yes' if dep.runs_seats else 'no'}")
    if dep.license:
        typer.echo(f"  license    {dep.license}")
    typer.echo(f"  present    {'yes' if present else 'no'}")

    if dep.install is None:
        typer.echo("  install    bh has no known install route — install it yourself.")
    else:
        route = "bh dep install " + dep.name if dep.install.cmd else "not driven by bh"
        typer.echo(f"  install    {route}")
        for line in textwrap.wrap(dep.install.note, width=70):
            typer.echo(f"             {line}")

    if dep.auth is None:
        typer.echo("  auth       none — nothing to authenticate.")
        return

    from . import credentials

    typer.echo(f"  auth       env: {', '.join(dep.auth.env_vars) or '—'}")
    typer.echo(f"             login: {' '.join(dep.auth.login) or '—'}")
    for line in credentials.render([credentials.probe(dep)])[1:]:
        typer.echo(f"  {line}")


@app.command("install", help="install a dep bh knows how to place (see the INSTALL column).")
def install(
    name: str = typer.Argument(..., help="dep to install (see `bh dep list`)"),
    version: str = typer.Option(
        "",
        "--version",
        help=(
            "install target (stable|latest|X.Y.Z); defaults to the row's version_env if set. "
            "Pins ONLY this initial bootstrap — once the tool is on PATH it owns its own version "
            "and this flag has no further effect. It is never consulted for an already-installed "
            "tool, so it cannot fight that tool's auto-update."
        ),
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="skip the proprietary-licence confirmation (for headless runs)."
    ),
) -> None:
    """Rows with an install COMMAND are installed; every other row prints its note and exits 1.

    Reads `deps.installable()` (what bh will run), not `deps.has_install_route()` (what bh knows
    about) — the narrower of the two predicates. Naming a command that exits 1 is the bh-pc2a.33
    failure mode reproduced one hop later, by the verb that exists to prevent it.
    """
    dep = _lookup(name)
    if dep.install is None:
        typer.echo(
            f"✗ bh has no known install route for {name!r}.\n"
            "  Install it yourself and make sure it is on PATH.",
            err=True,
        )
        raise typer.Exit(1)

    from . import harness as harness_mod

    harness_mod.install(name, version=version, yes=yes)


@app.command("auth", help="probe a dep's credential and name the exact command to fix a gap.")
def auth(
    name: str = typer.Argument("", help="probe one row only (see `bh dep list`'s AUTH column)"),
    check: bool = typer.Option(
        False, "--check", help="exit non-zero when the host is not usable (CI/headless gate)."
    ),
) -> None:
    """Report, never log in (bh-q160.3).

    `--check` is the gate form: it makes the same report and turns "this host cannot work" into
    a non-zero exit, which is what an unattended install needs. Without it the report is
    informational, because an operator running this by hand is diagnosing, not gating.
    """
    from . import credentials

    gated = deps_mod.authenticated_deps()
    if name and name not in {d.name for d in gated}:
        known = ", ".join(d.name for d in gated)
        typer.echo(f"✗ {name!r} has no credential bh can probe — one of: {known}", err=True)
        raise typer.Exit(2)

    reports = [credentials.probe(deps_mod.by_name(name))] if name else credentials.probe_all()

    # A NAMED row that is installed-but-unauthenticated is a request to fix it, not just to hear
    # about it. `--check` never does this: it is the unattended gate, and a gate that opens an
    # interactive login is not a gate.
    if name and not check and reports[0].installed and not reports[0].authenticated:
        typer.echo(f"{name} is not authenticated — starting its login flow.\n")
        reports = [credentials.run_login(name)]

    for line in credentials.render(reports):
        typer.echo(line)

    # Requirements are a property of the WHOLE host, so a single-row probe reports and stops
    # rather than pretending one row can answer "is this host usable".
    if not check or name:
        return
    failures = credentials.unmet(reports)
    if failures:
        typer.echo("", err=True)
        for failure in failures:
            typer.echo(f"✗ {failure}", err=True)
        raise typer.Exit(1)
    typer.echo("\n✓ host has the credentials it needs.")
