"""`bh setup guide` — export the bundled setup Guide, hand it off, or walk it here (bh-0olv9.6).

The verb that makes the Guide reachable. `src/beadhive/assets/guides/setup/` ships in the wheel
(setup-guide ADR, Decision 1), but a Guide that ships with no way to invoke it is a file, not a
feature. Three behaviours, in order:

1. **Export** it to ``~/.beadhive/guides/setup/`` so the user owns a copy their harness can read
   and their agent can cite by path.
2. **Hand off** — print the walk instruction naming the exported ``GUIDE.md``.
3. **Fall back** to an interactive CLI walk over the same ``steps/``, for a harness that is not
   Guide-aware, or a user with no harness at all.

WHY THIS IS `bh setup` AND NOT `bh dep`, restated because it is the same call `bh setup
toolchain` made (bh-vmdq.7) and the same one that will be re-litigated: `setup` already owns the
probe that REPORTS the gap (`bh setup check`), so it owns the thing that closes it. `bh dep` is a
table surface over individual tools; a guided walk is not a dep row.

GUIDE-AWARENESS IS NOT PROBED, AND THAT IS A DECISION. The agentguides.io 0.1 family has no
capability handshake — nothing bh can read tells it whether the harness on the other end of this
process will load a `GUIDE.md`. Guessing is costly in both directions: guess "aware" for a plain
harness and the user gets a path to a file nobody opens; guess "not aware" for a Guide-aware one
and bh hijacks a run the harness was about to drive better. So the fork is taken on the one
signal that IS observable and IS the right question anyway — whether a human is sitting at a
terminal (:func:`_interactive`). Non-interactive (a harness is capturing this output) prints the
handoff, which names what a Guide-aware reader should do AND what a plain-Skill reader should do.
Interactive additionally OFFERS the wizard, defaulting to no. ``--wizard`` / ``--handoff`` force
either branch. That is "offer, never insist" — the Guide's own tenet — applied to its own entry.

THE WIZARD IS THE PART THAT WILL ROT and the bead says so: it duplicates the Guide's control flow
in Python, so every step added to ``steps/`` is a step it can silently lack. Both mitigations are
taken. It is driven by :func:`discover_steps`, which reads whatever ``steps/*.md`` files are
present — there is no step list in this module, not even a fallback one — and
`tests/test_setup_guide_cli.py` asserts the walk covers every step file in the exported guide.
A step file whose frontmatter is missing or unparseable still becomes a step (named from its
filename), because dropping it silently is the precise failure the derivation exists to prevent.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import typer
from ruamel.yaml import YAML

from . import config

#: THE post-install sentence, said verbatim by every channel that speaks to a user just
#: after install (bh-0olv9.10). A user who installed without reading ``INSTALL.md`` —
#: ``brew install``, ``pip install``, a colleague's copy-pasted command — lands on a
#: configured-nothing ``bh``; this is the one thing each of those routes now says.
#:
#: THREE COPIES EXIST AND CANNOT BE SINGLE-SOURCED, because two of them are consumed
#: outside this process. Recorded together here so a future reword finds all three:
#:
#: 1. **Here** — ``bh``'s own setup-gate hint, :func:`beadhive.cli._enforce_setup_gate`.
#:    This is the copy that matters most: it reaches users who installed by a route
#:    nobody anticipated. It EXTENDS the existing "run ``bh setup check``" nudge rather
#:    than adding a second one — two hints firing at once reads as broken.
#: 2. ``README.md``, under "First run — rung 1". ``pyproject.toml``'s
#:    ``readme = "README.md"`` makes that file the wheel's long description, so this is
#:    also the PyPI project page a ``pip install beadhive`` user lands on.
#: 3. ``beadhive/homebrew-tap`` → ``Formula/beadhive.rb``'s ``caveats`` block — what
#:    ``brew install beadhive/tap/beadhive`` prints when it finishes. Another repo, so
#:    nothing in this one can gate it; that is exactly why it is written down here.
#:
#: ``tests/test_setup_guide_cli.py`` pins (1) against (2). (3) is out of reach of any
#: test here and is guarded only by this note — keep the sentence short enough that
#: drift between the three is obvious on sight.
POST_INSTALL_POINTER = (
    f"Run `{config.BINARY_ALIAS} setup guide` to finish setup — a guided, probe-first "
    "walk from a bare install to a configured workspace."
)

#: Statuses :func:`export` reports per file. ``local-edit`` is the one that matters: a
#: destination whose bytes differ from the bundled copy is NOT overwritten — the user is told.
CREATED = "created"
UNCHANGED = "unchanged"
UPDATED = "updated"
LOCAL_EDIT = "local-edit"
ORPHANED = "orphaned"


@dataclass(frozen=True)
class Exported:
    """One file's outcome from an export run."""

    rel: str
    path: Path
    status: str


@dataclass(frozen=True)
class Step:
    """One ``steps/*.md`` file, as the wizard walks it.

    Every field except ``file`` degrades to something usable when the frontmatter is absent or
    unparseable, so an unreadable step is still WALKED (with its filename as its title) rather
    than skipped. The alternative — parse strictly, drop what fails — makes a malformed step
    invisible, which is worse than an ugly one.
    """

    file: Path
    id: str
    title: str
    performer: str = ""
    action: dict[str, Any] = field(default_factory=dict)
    verify: dict[str, Any] = field(default_factory=dict)
    interactions: list[dict[str, Any]] = field(default_factory=list)
    body: str = ""


# ---- locations ---------------------------------------------------------------


def bundled_root() -> Path:
    """The Guide as it ships inside the package (ADR Decision 1 — the existing assets path)."""
    return config.asset("guides") / "setup"


def export_root() -> Path:
    """Where an exported copy lives: ``~/.beadhive/guides/setup`` (``$BH_HOME`` relocates it)."""
    return config.home() / "guides" / "setup"


# ---- export ------------------------------------------------------------------


def _bundled_files(root: Path) -> list[Path]:
    """Every shippable file under *root*, sorted. ``.gitkeep`` is excluded: it exists to keep an
    empty `steps/` in git and is not part of the Guide (the packaging test excludes it too)."""
    return sorted(p for p in root.rglob("*") if p.is_file() and p.name != ".gitkeep")


#: The execute bits. `scripts/*.sh` ship 0755 and the steps invoke them DIRECTLY
#: (`010-preflight`'s `action: {type: script}`; 050/060/090/092 say "run: `scripts/check-*.sh`"),
#: so an exported copy at 0644 exits 126 — a code no step's handler contracts for.
_EXEC = 0o111


def _mirror_exec_bit(src: Path, dst: Path) -> None:
    """Give *dst* the execute bits *src* has, if it is missing them.

    THE EXECUTE BIT IS STRUCTURE, NOT CONTENT — the same reason the empty `steps/` dir is
    mirrored below. So this runs against a destination in ANY state, not just one we just wrote:
    a copy exported before this was fixed has bytes IDENTICAL to the bundle (status
    ``unchanged``), so nothing would ever rewrite it and the user's export would stay broken
    across every upgrade. Repairing the mode is not a clobber — no byte of a local edit changes,
    and a script the user edited is one they still mean to run.
    """
    src_exec = src.stat().st_mode & _EXEC
    dst_mode = dst.stat().st_mode
    if src_exec and not dst_mode & _EXEC:
        dst.chmod(dst_mode | src_exec)


def export(*, force: bool = False, dry_run: bool = False) -> list[Exported]:
    """Copy the bundled Guide to :func:`export_root`, reporting every file's outcome.

    IDEMPOTENT, AND NEVER A SILENT CLOBBER. Re-running an unchanged export writes nothing and
    reports ``unchanged`` for every file. A destination whose bytes DIFFER from the bundled copy
    is left exactly as the user left it and reported ``local-edit``; ``force`` is what overwrites
    it, and then the report says ``updated``. Distinguishing "you edited this" from "we shipped a
    new version" is not possible from the bytes and is not attempted — either way the honest
    report is the same one: this file differs, bh did not touch it, here is how to take the new
    copy.

    A file present in the export dir but absent from the bundle is reported ``orphaned`` and left
    alone — a step removed upstream must not delete something the user may have written.

    THE FILE MODE TRAVELS WITH THE BYTES (:func:`_mirror_exec_bit`). The export's product is a
    tree the user RUNS, not one they only read, so a `scripts/*.sh` that arrives without its
    execute bit is a broken export even though every byte is right.

    ``dry_run`` computes every status with zero mutation.
    """
    src_root, dst_root = bundled_root(), export_root()
    results: list[Exported] = []
    expected: set[Path] = set()

    for src in _bundled_files(src_root):
        rel = src.relative_to(src_root)
        dst = dst_root / rel
        expected.add(dst)
        if not dst.exists():
            status = CREATED
        elif dst.read_bytes() == src.read_bytes():
            status = UNCHANGED
        else:
            status = UPDATED if force else LOCAL_EDIT
        if not dry_run and status in (CREATED, UPDATED):
            dst.parent.mkdir(parents=True, exist_ok=True)
            # copy2, NOT copyfile: copyfile carries content only, so a 0755 script landed 0644
            # and every `scripts/*.sh` the Guide tells you to run died with "Permission denied".
            shutil.copy2(src, dst)
        elif not dry_run and dst.exists():
            _mirror_exec_bit(src, dst)
        results.append(Exported(rel.as_posix(), dst, status))

    # The bundle's DIRECTORIES are mirrored too, even the ones that are empty today. `steps/`
    # ships empty while bh-0olv9.4/.5/.8 are in flight, and the handoff text tells the reader to
    # walk `steps/` — pointing at a directory that does not exist would read as a broken export
    # rather than as an unfinished Guide. Not reported as files: an empty dir is structure.
    if not dry_run:
        dst_root.mkdir(parents=True, exist_ok=True)
        for src_dir in sorted(p for p in src_root.rglob("*") if p.is_dir()):
            (dst_root / src_dir.relative_to(src_root)).mkdir(parents=True, exist_ok=True)

    if dst_root.is_dir():
        for stray in sorted(dst_root.rglob("*")):
            if stray.is_file() and stray not in expected:
                results.append(Exported(stray.relative_to(dst_root).as_posix(), stray, ORPHANED))
    return results


def summarize(results: list[Exported]) -> str:
    """``3 created, 2 unchanged`` — counts in a stable order, omitting the zeroes."""
    order = (CREATED, UPDATED, UNCHANGED, LOCAL_EDIT, ORPHANED)
    counts = {s: sum(1 for r in results if r.status == s) for s in order}
    return ", ".join(f"{n} {s}" for s, n in counts.items() if n) or "nothing to export"


# ---- step discovery ----------------------------------------------------------

_yaml = YAML(typ="safe")


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """``(frontmatter, body)``. A file with no ``---`` fence yields ``({}, text)`` rather than
    raising — see :class:`Step` on why an unparseable step is still a step."""
    if not text.startswith("---\n"):
        return {}, text
    parts = text.split("---\n", 2)
    if len(parts) < 3:
        return {}, text
    try:
        loaded = _yaml.load(parts[1])
    except Exception:
        return {}, parts[2]
    return (loaded if isinstance(loaded, dict) else {}), parts[2]


def _title_from_filename(path: Path) -> str:
    """``040-verify-install.md`` → ``verify install``. The fallback title, used only when the
    frontmatter cannot supply one."""
    stem = path.stem
    _, _, rest = stem.partition("-")
    return (rest or stem).replace("-", " ")


def discover_steps(root: Path | None = None) -> list[Step]:
    """Every ``steps/*.md`` under *root* (default: the exported guide), in filename order.

    THE WIZARD'S STEP LIST, AND THE ONLY ONE. Numbering is what orders a Guide's steps (the
    envelope's ``## Structure`` says so, and leaves gaps precisely so steps can be inserted), so
    sorting by filename is reading the Guide's own ordering rather than imposing a second one.
    Nothing here knows any step's name: a step added to ``steps/`` is walked with no edit to
    this module, and a step that fails to parse is walked anyway.

    Returns ``[]`` when there is no ``steps/`` dir or it is empty — the honest answer while
    bh-0olv9.4/.5/.8 are still being written, and the reason the wizard reports "no steps" rather
    than crashing.
    """
    steps_dir = (root or export_root()) / "steps"
    if not steps_dir.is_dir():
        return []
    out: list[Step] = []
    for path in sorted(steps_dir.glob("*.md")):
        try:
            front, body = _split_frontmatter(path.read_text(encoding="utf-8"))
        except OSError:
            front, body = {}, ""
        block = front.get("step") if isinstance(front.get("step"), dict) else {}
        out.append(
            Step(
                file=path,
                id=str(block.get("id") or path.stem),
                title=str(block.get("title") or _title_from_filename(path)),
                performer=str(block.get("performer") or ""),
                action=block.get("action") if isinstance(block.get("action"), dict) else {},
                verify=block.get("verify") if isinstance(block.get("verify"), dict) else {},
                interactions=[i for i in (block.get("interactions") or []) if isinstance(i, dict)],
                body=body.strip(),
            )
        )
    return out


# ---- the wizard --------------------------------------------------------------


def _interactive() -> bool:
    """Whether a human is plausibly at the other end — see the module docstring on why this,
    and not a guess about the harness, is the signal the default fork is taken on."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def step_command(step: Step, root: Path) -> list[str] | None:
    """The argv this step would run, or ``None`` when it has no runnable action.

    Reads the 0.1 step schema's two runnable shapes and nothing else: ``action.type: command``
    with a ``command`` string, and ``action.type: script`` (or a ``verify.type: script``) naming
    a path relative to the guide root. ``prompt`` and ``manual`` steps are for a human or an
    agent to carry out and deliberately have no argv — the wizard prints them and waits.
    """
    action = step.action
    kind = str(action.get("type") or "")
    if kind == "command" and action.get("command"):
        return ["sh", "-c", str(action["command"])]
    if kind == "script" and action.get("script"):
        return ["sh", str(root / str(action["script"]))]
    return None


def _prompt_text(step: Step) -> str:
    """What the step asks of the person running it: its ``action.prompt`` when it has one, else
    its ``before`` interaction prompts, else the step body."""
    if step.action.get("prompt"):
        return str(step.action["prompt"]).strip()
    before = [
        str(i["prompt"]).strip()
        for i in step.interactions
        if i.get("when") != "after" and i.get("prompt")
    ]
    return "\n\n".join(before) if before else step.body


def wizard(
    root: Path | None = None,
    *,
    ask: Callable[[str, str], str] | None = None,
    echo: Callable[[str], None] = typer.echo,
) -> int:
    """Walk the exported guide's steps interactively. Returns a process-style exit code.

    Deliberately not a re-implementation of the Guide's semantics: it has no
    ``on_failure`` strategies, no scoring, and no judgment — a CLI cannot do ``verify.type:
    agent_judgment``, and it SAYS so rather than pretending a step passed. What it does is walk
    the real steps in the real order, show each one's instruction, and offer to run the commands
    the step declares. That is the fallback's whole job; anything more would be a second Guide
    engine to keep in sync with the first.

    ``ask`` is injected so the walk is testable without a tty (it takes ``(prompt, default)`` and
    returns the answer).
    """
    root = root or export_root()
    ask = ask or (lambda prompt, default: typer.prompt(prompt, default=default))
    steps = discover_steps(root)
    if not steps:
        echo(
            f"No steps found in {root / 'steps'} — the bundled guide ships none yet.\n"
            f"  Read {root / 'GUIDE.md'} for the framing and the install route fork."
        )
        return 0

    total = len(steps)
    for index, step in enumerate(steps, start=1):
        echo(f"\n── [{index}/{total}] {step.title}")
        if step.performer:
            echo(f"   performer: {step.performer}")
        instruction = _prompt_text(step)
        if instruction:
            echo("\n" + instruction)

        cmd = step_command(step, root)
        if cmd:
            echo(f"\n   $ {cmd[-1]}")
            answer = ask("   run it? [y]es / [s]kip / [q]uit", "y").strip().lower()[:1]
            if answer == "q":
                echo(f"\nStopped at step {index}/{total}. Re-run to pick up from the top.")
                return 1
            if answer == "y":
                rc = subprocess.run(cmd).returncode  # noqa: S603 — argv from the guide asset
                if rc != 0:
                    echo(f"   ✗ exited {rc}")
                    if ask("   continue anyway? [y/N]", "n").strip().lower()[:1] != "y":
                        return 1
        else:
            if str(step.verify.get("type") or "") == "agent_judgment":
                echo("\n   (this step is verified by judgment — a CLI walk cannot score it)")
            answer = ask("   done? [y]es / [s]kip / [q]uit", "y").strip().lower()[:1]
            if answer == "q":
                echo(f"\nStopped at step {index}/{total}. Re-run to pick up from the top.")
                return 1

    echo(f"\n✓ walked {total} step(s). `{config.BINARY_ALIAS} setup check` reports where you are.")
    return 0


# ---- handoff -----------------------------------------------------------------


def handoff_text(root: Path | None = None) -> str:
    """The walk instruction, naming the exported ``GUIDE.md`` by path.

    Names all three readers rather than picking one, because bh cannot tell which is reading it
    (module docstring). The plain-Skill line is the degradation contract ``SKILL.md`` declares,
    restated where the reader who needs it will actually be looking.
    """
    root = root or export_root()
    alias = config.BINARY_ALIAS
    return (
        "Walk it:\n"
        f"  Guide-aware harness  load {root / 'GUIDE.md'} and walk steps/ in order\n"
        f"  plain-Skill harness  read {root / 'SKILL.md'}, then GUIDE.md for framing, then\n"
        "                       steps/ in order — linear apart from the install-route fork\n"
        f"  no harness           {alias} setup guide --wizard"
    )


def run_guide(
    *,
    wizard_mode: bool = False,
    handoff_mode: bool = False,
    force: bool = False,
    dry_run: bool = False,
) -> None:
    """Implement ``bh setup guide``: export, report, then hand off or walk."""
    results = export(force=force, dry_run=dry_run)
    root = export_root()
    prefix = "would export" if dry_run else "exported"
    typer.echo(f"✓ guide {prefix} → {root}  ({summarize(results)})")

    edited = [r for r in results if r.status == LOCAL_EDIT]
    if edited:
        # Told, never surprised: the whole point of not overwriting is undone if bh stays quiet.
        typer.echo(
            f"\n⚠ {len(edited)} file(s) differ from the bundled copy and were NOT overwritten:"
        )
        for r in edited:
            typer.echo(f"    {r.rel}")
        typer.echo(
            f"  Your edits are intact. `{config.BINARY_ALIAS} setup guide --force` takes the "
            "bundled copy instead."
        )
    orphans = [r for r in results if r.status == ORPHANED]
    if orphans:
        typer.echo(
            f"\n• {len(orphans)} file(s) in {root} are not part of the bundled guide and were "
            "left alone: " + ", ".join(r.rel for r in orphans)
        )

    if dry_run:
        return

    if wizard_mode:
        raise typer.Exit(wizard(root))

    typer.echo("\n" + handoff_text(root))
    if handoff_mode or not _interactive():
        return

    # Interactive and unforced: OFFER the walk, default no. A user who ran this to export a copy
    # answers with one keypress; a user with no harness gets the fallback without knowing a flag.
    if typer.confirm("\nWalk it here now instead?", default=False):
        raise typer.Exit(wizard(root))
