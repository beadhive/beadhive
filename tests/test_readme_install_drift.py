"""README must not drift from INSTALL.md, and its anchors must not 404 their linkers (bh-r59o1.6).

This epic exists because three surfaces each kept their own copy of the install command and
two of them rotted for a full minor release: INSTALL.md moved to the managed path at v0.8.0
and the README did not follow. Fixing the copies without gating them just resets the clock.

WHAT GREEN HERE MEANS — READ THIS BEFORE TRUSTING IT. Green means "README.md and INSTALL.md
AGREE". It does NOT mean "the pinned version is current": this gate compares the two files
against each other, so it stays green while BOTH are stale. Keeping the `v0.8.0` pin in step
with the released version is deliberately a separate concern (bh-wp6h, being retired by epic
bh-7daa6). This is a drift gate, not a freshness gate.

Three assertions:

1. The README's install block carries INSTALL.md's `methods[0].command` — parsed from the
   schema'd YAML frontmatter, never regexed out of the prose body. Compared PART BY PART:
   INSTALL.md carries the command `&&`-joined on one line, while the README (correctly, for a
   human reader) splits it into two separately-commented lines inside a fence. A naive
   `assert command in readme_text` is FALSE even though the two agree perfectly — and the
   wrong fix is to mangle the README back into a one-liner.

2. The README still contains the heading beadhive.ai deep-links to. The site adds a `url#section`
   link from its collapsed "Manual install" disclosure into this README; GitHub derives heading
   anchors from heading TEXT, so a rename 404s that link to the top of the README with no build
   error, no test failure, and from a repo the site's own CI cannot see. This is the only place
   that cross-repo link can be guarded, which is why it lives here.

3. Every anchor ANY in-repo file links to still resolves — derived by grep, not by list. This
   is the assertion that earns its keep: an earlier revision of this very batch replaced
   `## Develop` with a `<summary>`, silently breaking CONTRIBUTING.md:24's `README.md#develop`.
   Nothing caught it. `just lint-md`'s MD051 is same-document only, and CONTRIBUTING.md is not
   even in the lint globs — the failure landed one file outside where anyone was watching.

ANCHOR METHOD, settled — do not re-derive it. GitHub's slug rule is implemented LOCALLY below:
deterministic, hermetic, offline. Two dead ends, both already walked:
  * `api.github.com/markdown` with `mode: gfm` emits ZERO `user-content-*` ids for ANY heading,
    so an id-grep there reads identically on a healthy file and a broken one. Never use it.
  * Scraping live github.com only ever renders `main`, so it structurally CANNOT fail on an
    unmerged branch — which defeats the entire purpose of a pre-merge gate — and it makes
    `just test` network-dependent and rate-limited.
The local slugger was validated against the DEFAULT-mode `/markdown` oracle on three revisions
of this README (a1d960d: 5 headings, 1a4f2a9: 8, d19c794: 9) — exact match on all three,
including `questions--feedback`, `first-run--rung-1`, `pypi-route-fallback-not-recommended`
and `beadhive-bh`. If GitHub changes its renderer, re-confirm with that default-mode endpoint
as a ONE-OFF oracle — never `mode: gfm`, and never from inside this test.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
README = REPO / "README.md"
INSTALL_MD = REPO / "INSTALL.md"

# Anchors this README is PINNED to publish, and who consumes each. Cross-repo consumers cannot
# be discovered by grepping this repo, so they have to be named here; in-repo consumers are
# derived by grep instead (see test_every_in_repo_link_to_a_readme_anchor_resolves) so the set
# cannot silently fall behind. Renaming one of these headings breaks the listed consumer with
# NO error anywhere — that is why it is pinned.
GUARDED_ANCHORS: dict[str, str] = {
    # beadhive.ai's collapsed "Manual install" disclosure deep-links here (EPIC C). The site's
    # own CI cannot see this repo, so this test is the only guard on that link.
    "install": "beadhive.ai — the 'Manual install' disclosure deep-links README.md#install",
}

# The README's install block must keep this on `uv tool install`: unforced it no-ops on a
# machine that already has `bh` and STILL EXITS 0, leaving it version-skewed (bh-6x5xj).
UPGRADE_FLAG = "--force"

_HEADING = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")
_FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})(.*)$")
# A `#` that opens a trailing shell comment is preceded by whitespace or starts the line. The
# `#` in `github:beadhive/beadhive/v0.8.0#default` is NOT — it is part of the flake ref.
_TRAILING_COMMENT = re.compile(r"(?:^|\s)#.*$")


def _lines() -> list[str]:
    return README.read_text().splitlines()


def _fence_mask(lines: list[str]) -> list[bool]:
    """True for every line inside (or delimiting) a fenced code block.

    THE PARSER MUST BE FENCE-AWARE. A naive `^#` regex over this README produces a phantom
    heading from `# On a NEW machine you do not have `just` yet` inside the bootstrap fence —
    this file is the regression case for that bug.
    """
    mask = [False] * len(lines)
    opener: str | None = None
    for i, line in enumerate(lines):
        m = _FENCE.match(line)
        if opener is None:
            # A backtick fence's info string may not itself contain a backtick (CommonMark),
            # which is what keeps inline code like ``a ``` b`` from opening one.
            if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
                opener = m.group(1)
                mask[i] = True
            continue
        mask[i] = True
        # A closing fence is the same character, at least as long, with no info string.
        if not m or m.group(1)[0] != opener[0] or len(m.group(1)) < len(opener):
            continue
        if not m.group(2).strip():
            opener = None
    return mask


def _slug(text: str) -> str:
    """GitHub's heading-anchor rule, implemented locally (see module docstring for provenance)."""
    text = re.sub(r"`([^`]*)`", r"\1", text)  # inline code -> its contents
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)  # [text](url) -> text
    text = re.sub(r"</?[^>]+>", "", text)  # inline HTML tags contribute no text
    text = re.sub(r"(\*\*|__|\*|_)", "", text)  # emphasis markers are not rendered text
    text = text.lower()
    text = re.sub(r"[^\w\- ]", "", text)  # keep word chars, hyphens, spaces
    return text.replace(" ", "-")


def _readme_anchors() -> dict[str, str]:
    """anchor -> heading text, in document order, fence-aware and duplicate-suffixed like GitHub."""
    lines = _lines()
    mask = _fence_mask(lines)
    anchors: dict[str, str] = {}
    seen: dict[str, int] = {}
    for line, fenced in zip(lines, mask, strict=True):
        if fenced:
            continue
        m = _HEADING.match(line)
        if not m:
            continue
        base = _slug(m.group(2))
        n = seen.get(base, 0)
        seen[base] = n + 1
        anchors[base if n == 0 else f"{base}-{n}"] = m.group(2)
    return anchors


def _install_section() -> str:
    """The README's `## Install` section: its heading through the next same-or-higher heading."""
    lines = _lines()
    mask = _fence_mask(lines)
    start = None
    for i, (line, fenced) in enumerate(zip(lines, mask, strict=True)):
        if fenced:
            continue
        m = _HEADING.match(line)
        if not m:
            continue
        if start is None:
            if _slug(m.group(2)) == "install":
                start = i
        elif len(m.group(1)) <= 2:
            return "\n".join(lines[start:i])
    assert start is not None, (
        "README.md has no `## Install` heading — INSTALL.md and beadhive.ai link README.md#install"
    )
    return "\n".join(lines[start:])


def _install_section_commands() -> list[str]:
    """Every shell line in the install section's fences, trailing comments stripped."""
    cmds = []
    for block in re.findall(r"```sh\n(.*?)```", _install_section(), re.S):
        for line in block.splitlines():
            stripped = _TRAILING_COMMENT.sub("", line).strip()
            if stripped:
                cmds.append(" ".join(stripped.split()))
    return cmds


def _recommended_command() -> str:
    """INSTALL.md's `methods[0].command`, from the schema'd frontmatter — never from the prose."""
    text = INSTALL_MD.read_text()
    assert text.startswith("---\n"), "INSTALL.md must open with YAML frontmatter"
    methods = yaml.safe_load(text.split("---\n", 2)[1])["install"]["methods"]
    assert methods, "INSTALL.md frontmatter declares no install methods"
    return str(methods[0]["command"])


def _in_repo_readme_anchor_links() -> list[tuple[str, str]]:
    """(source file, anchor) for every in-repo markdown link into a README heading."""
    found = []
    for path in sorted(REPO.glob("*.md")) + sorted(REPO.glob("docs/**/*.md")):
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):  # pragma: no cover - defensive
            continue
        for anchor in re.findall(r"README\.md#([\w\-]+)", text):
            found.append((str(path.relative_to(REPO)), anchor))
    return found


@pytest.mark.parametrize("part", [p.strip() for p in _recommended_command().split("&&")])
def test_readme_install_block_carries_every_part_of_installs_recommended_command(part):
    """Part by part, not the joined string — the README splits the `&&` across commented lines."""
    assert part in _install_section_commands(), (
        f"README.md's install block does not carry INSTALL.md's recommended command "
        f"`{part}`. FIX: update the README's Managed path fence (or INSTALL.md's "
        f"`install.methods[0].command`) so the two agree — do NOT collapse the README's fence "
        f"back into one `&&`-joined line to satisfy this test. README has: "
        f"{_install_section_commands()}"
    )


def test_readme_install_block_keeps_the_load_bearing_upgrade_flag():
    """`uv tool install` unforced no-ops on an existing install and still exits 0 (bh-6x5xj)."""
    offenders = [
        c for c in _install_section_commands() if "uv tool install" in c and UPGRADE_FLAG not in c
    ]
    assert not offenders, (
        f"README.md's install block runs `uv tool install` without `{UPGRADE_FLAG}`, which "
        f"no-ops on a machine that already has `bh` and still exits 0, leaving it "
        f"version-skewed (bh-6x5xj). FIX: add `{UPGRADE_FLAG}`: {offenders}"
    )


@pytest.mark.parametrize("anchor,consumer", sorted(GUARDED_ANCHORS.items()))
def test_readme_still_publishes_the_anchors_out_of_repo_consumers_deep_link(anchor, consumer):
    """A rename 404s the link to the top of the README with no error on either side."""
    anchors = _readme_anchors()
    assert anchor in anchors, (
        f"README.md no longer publishes the anchor `#{anchor}`, which is deep-linked by: "
        f"{consumer}. GitHub derives anchors from heading TEXT, so renaming the heading breaks "
        f"that link silently — no build error, no 404 the linking repo's CI can see. FIX: "
        f"restore a heading that slugs to `{anchor}`, or update the consumer AND this constant "
        f"together. README currently publishes: {sorted(anchors)}"
    )


@pytest.mark.parametrize(
    "heading,slug",
    [
        # Every one of these was confirmed against the DEFAULT-mode api.github.com/markdown
        # oracle on three revisions of this README. Consecutive spaces and em dashes yield
        # DOUBLED hyphens — the awkward cases are the point of pinning them here.
        ("Beadhive (`bh`)", "beadhive-bh"),
        ("Questions / feedback", "questions--feedback"),
        ("First run — rung 1", "first-run--rung-1"),
        ("PyPI route (fallback, not recommended)", "pypi-route-fallback-not-recommended"),
        ("Managed path (recommended)", "managed-path-recommended"),
        ("**Bold** and _em_", "bold-and-em"),
        ("See [the docs](docs/OVERVIEW.md)", "see-the-docs"),
    ],
)
def test_the_local_slugger_matches_githubs_rule(heading, slug):
    """Pinned oracle values. If GitHub changes its renderer, re-confirm with the DEFAULT-mode
    `/markdown` endpoint as a one-off — never `mode: gfm`, which emits no ids at all."""
    assert _slug(heading) == slug


def test_the_heading_parser_is_fence_aware():
    """A naive `^#` regex over this README invents a heading from `# On a NEW machine ...`
    inside the bootstrap fence. That comment is real, and it is the regression case."""
    assert "on-a-new-machine-you-do-not-have-just-yet" not in _readme_anchors(), (
        "the heading parser picked up a shell comment inside a code fence — it must skip "
        "fenced blocks, or it will 'find' anchors GitHub never publishes"
    )
    sample = ["# Real", "```sh", "# Not one", "```", "~~~", "## Nor this", "~~~", "## Real too"]
    mask = _fence_mask(sample)
    kept = [line for line, fenced in zip(sample, mask, strict=True) if not fenced]
    assert kept == ["# Real", "## Real too"]


def test_every_in_repo_link_to_a_readme_anchor_resolves():
    """Derived by grep, not by list — `just lint-md`'s MD051 is SAME-DOCUMENT only, and
    CONTRIBUTING.md is not even in the lint globs, so nothing else covers cross-file anchors.

    An earlier revision of this batch replaced `## Develop` with a `<summary>` element (which
    GitHub gives no anchor) and broke CONTRIBUTING.md:24's `README.md#develop`. This is the
    assertion that catches that.
    """
    links = _in_repo_readme_anchor_links()
    assert links, "no in-repo `README.md#anchor` links found — has the grep broken?"
    anchors = _readme_anchors()
    broken = [(src, a) for src, a in links if a not in anchors]
    assert not broken, (
        "in-repo links point at README.md headings that no longer exist: "
        + "; ".join(f"{src} -> README.md#{a}" for src, a in broken)
        + ". A `<summary>`/HTML element gets NO GitHub anchor — only a real `#` heading does. "
        f"FIX: restore the heading, or update the linking file. README publishes: {sorted(anchors)}"
    )
