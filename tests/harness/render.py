"""Succinct renderer + comparator for test-harness git histories.

A `Timeline` is a normalized view of an integration branch — the base commit, then a
(merge, dev) pair per landed bead — built either from REAL git (`Timeline.from_actual`) or
from the fixture's EXPECTED result (`Timeline.from_expected`). It renders compactly, and
`diff_report()` compares expected vs actual and prints per the AGF_RENDER mode.

Node identity for COMPARISON excludes the commit subject/body — we validate authors, ssh
signatures, branch names and merge structure, never message content. The subject is kept for
display only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from . import history
from .world import git

# signature glyphs: G good · U good/unknown · B bad · N none
_SIG_GLYPH = {"G": "✔", "U": "~", "B": "✗", "N": "·"}
_KIND_GLYPH = {"base": "○", "dev": "●", "merge": "◆"}


@dataclass(frozen=True)
class Node:
    kind: str  # base | dev | merge
    subject: str  # display only — NOT compared
    author: str
    email: str
    sig: str  # G/U/B/N
    signer: str
    branch: str  # "main" for base/merge; "wt/bead/issue/<id>" for dev

    def key(self) -> tuple:
        """The compared identity of a node (subject deliberately excluded)."""
        return (self.kind, self.author, self.email, self.sig, self.signer, self.branch)

    def line(self) -> str:
        glyph = _KIND_GLYPH.get(self.kind, "?")
        sig = _SIG_GLYPH.get(self.sig, "?")
        who = f"{self.author} {sig}{self.signer}".rstrip()
        loc = "" if self.branch == "main" else f"  [{self.branch}]"
        lead = "╰╴" if self.kind == "dev" else "  "
        return f"{lead}{glyph} {self.subject:<20} {who}{loc}"


def _sig(identity) -> tuple[str, str]:
    return ("G", identity.email) if identity.key else ("N", "")


def _node(kind: str, c: dict, branch: str) -> Node:
    return Node(kind, c["subject"], c["author"], c["email"], c["sig"], c["signer"], branch)


def _wt_branch(main, sha: str) -> str:
    # --points-at (tip == sha), NOT --contains: in a chain a dev commit is an ancestor of the
    # later beads' branches, so --contains would ambiguously match all of them.
    out = git(
        "-C", str(main), "branch", "--format=%(refname:short)", "--points-at", sha, check=False
    ).stdout
    return next((b for b in out.split() if b.startswith("wt/bead/")), "?")


@dataclass
class Timeline:
    label: str
    nodes: list[Node]  # newest-first (matches `git log`)

    @classmethod
    def from_expected(cls, label, world, modality, order) -> Timeline:
        """The fixture's intended history for `order` (the beads' landing order)."""
        dev = modality.developer()
        ds = _sig(dev)
        rs = _sig(world.refiner)
        hs = _sig(world.human)
        nodes: list[Node] = []
        for bead in reversed(order):
            nodes.append(
                Node(
                    "merge",
                    f"chore(merge): bead {bead}",
                    world.refiner.name,
                    world.refiner.email,
                    *rs,
                    "main",
                )
            )
            nodes.append(
                Node("dev", f"implement {bead}", dev.name, dev.email, *ds, f"wt/bead/issue/{bead}")
            )
        nodes.append(Node("base", "init", world.human.name, world.human.email, *hs, "main"))
        return cls(label, nodes)

    @classmethod
    def from_actual(cls, label, rig) -> Timeline:
        """The real integration history: first-parent chain (base + --no-ff merges), each
        merge expanded with the dev commit it brought in (its second parent)."""
        main = rig.main
        full = {c["sha"]: c for c in history.commits(main, "main")}
        fp = git("-C", str(main), "log", "--first-parent", "--format=%H", "main").stdout.split()
        nodes: list[Node] = []
        for sha in fp:
            c = full[sha]
            if len(c["parents"]) > 1:
                nodes.append(_node("merge", c, "main"))
                dev = full.get(c["parents"][1])
                if dev:
                    nodes.append(_node("dev", dev, _wt_branch(main, dev["sha"])))
            else:
                nodes.append(_node("base", c, "main"))
        return cls(label, nodes)

    def keys(self) -> list[tuple]:
        return [n.key() for n in self.nodes]

    def render(self) -> str:
        return "\n".join(n.line() for n in self.nodes)


def diff_report(expected: Timeline, actual: Timeline, *, mode=None, out=print) -> bool:
    """Compare expected vs actual; render per AGF_RENDER (`all` always, `diff` only on
    mismatch, unset → silent). Returns True when the histories match."""
    mode = mode if mode is not None else os.environ.get("AGF_RENDER", "")
    ek, ak = expected.keys(), actual.keys()
    equal = ek == ak
    if mode == "all" or (mode == "diff" and not equal):
        status = "PASS" if equal else "DIFF"
        out(f"\n┌─ {expected.label}  [{status}] " + "─" * max(4, 30 - len(expected.label)))
        _render_block("expected", expected, ek, ak, out)
        _render_block("actual  ", actual, ak, ek, out)
        out("└" + "─" * 44)
    return equal


def _render_block(title, timeline, mine, theirs, out):
    out(f"  {title}:")
    for i, n in enumerate(timeline.nodes):
        differs = i >= len(theirs) or mine[i] != theirs[i]
        out(("  ✗ " if differs else "    ") + n.line())
