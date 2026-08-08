"""Release-channel drift — has `latest`/`stable` stopped tracking what it promises?

The read half of the channel model in ``docs/design/release-channel-branches-adr.md``. That ADR
makes the two branches asymmetric on purpose (its Decision 2):

* **`latest` cannot rot.** The act that would leave it stale — publishing a release — is the same
  act that advances it. There is no state in which someone forgot.
* **`stable` can rot, silently,** the moment nobody promotes. A `stable` nobody has moved for four
  releases is *the hardcoded-pin problem relocated, not solved* — and it fails the same way:
  quietly, correctly-looking, with no signal.

This module measures both conditions so the second one is **monitored** rather than accepted.
:func:`scan` is the pure measurement; the policy (thresholds, wording, who is told) lives in
``doctor._channel_drift_warnings``.

**Two findings, and they are not equally cheap.**

``off_tag`` — a channel sitting on a commit that no release tag points at — needs no threshold and
is never ambiguous. Both workflows only ever fast-forward the branch *to a tag they just verified
is published*, so a channel off a release tag means something moved it out of band and the
automation's guarantee is void. That is what a hand-push or a bad promotion leaves behind, and it
is the more valuable half of this check.

``behind_releases`` / ``behind_days`` — how far `stable` trails `latest` — is the tunable half, and
is deliberately reported as raw numbers here with no verdict attached.

**Everything is read from already-fetched refs; nothing here touches the network.** Same rule as
every other ``bh doctor`` probe (see ``doctor._hq_ahead_warnings``: "no ls-remote, no fetch"). The
cost is that the answer is "as of the last fetch". The usual worry — that the branch is fetched but
its tag is not, manufacturing a false ``off_tag`` — does not apply: a channel branch always points
*at* a tagged commit, so the tag is reachable from the fetched branch and a plain ``git fetch``
brings both down together. (The ADR's "a moving tag is invisible to `git fetch`" table is about
*updating an existing* tag, not fetching a new one.)

**Which refs.** ``refs/remotes/<remote>/<channel>``, not local branches: the channel is a property
of the publishing remote, and a maintainer's local branch named `stable` is not the channel.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

from .run import run

#: The two channel branches the ADR defines. Deliberately NOT configurable: per-series channels
#: (`0.8`, `release-0.8`) are named in the ADR's "What this does not settle" as an open question,
#: and inventing a knob for channels nobody has asked for would be a second thing to keep correct.
CHANNELS = ("latest", "stable")

#: A release tag. Matches `pyproject.toml`'s `tag_format = "v$version"` under commitizen. Anything
#: else (pre-releases, `nightly`, a series marker) is not a release and is not counted — a channel
#: parked on one is off-tag, which is the correct reading.
_RELEASE_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")

#: Field separator. `%00` is git's own escape for it — a literal NUL cannot travel in argv (execve
#: takes NUL-terminated strings), so the format string carries the escape and the OUTPUT carries
#: the byte. NUL is the only separator no ref name can contain: `git check-ref-format` rejects
#: every ASCII control character, so this cannot be confused by a hostile tag name.
_SEP_ESCAPE = "%00"
_SEP = "\x00"


@dataclass(frozen=True)
class _Ref:
    """One resolved ref: its name, the commit it ultimately names, and when it was created."""

    name: str
    commit: str
    when: int


def _version(tag: str) -> tuple[int, int, int]:
    m = _RELEASE_TAG.match(tag)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else (-1, -1, -1)


def _read_refs(repo: Path, remote: str):
    """Every release tag + both channel refs, in ONE `for-each-ref` (no network, no per-ref fork).

    ``%(*objectname)`` is the *dereferenced* object and is empty for anything but an annotated tag,
    so ``peeled or objectname`` is the commit in both cases. This matters concretely: this repo's
    tags ARE annotated (`annotated_tag = true`), so `refs/tags/v0.8.4` names a tag *object* whose
    sha is not the commit `latest` points at. Comparing raw ref shas would report every channel as
    off-tag.

    ``%(creatordate)`` is the tagger date for an annotated tag and the commit date for a lightweight
    one — i.e. "when this release was cut" under either tagging style.

    Returns ``None`` when git cannot read the directory at all (not a checkout, no perms).
    """
    fmt = _SEP_ESCAPE.join(("%(refname)", "%(objectname)", "%(*objectname)", "%(creatordate:unix)"))
    res = run(
        [
            "git",
            "for-each-ref",
            f"--format={fmt}",
            "refs/tags/v*",
            *[f"refs/remotes/{remote}/{c}" for c in CHANNELS],
        ],
        cwd=str(repo),
        check=False,
        capture=True,
    )
    if res.returncode != 0:
        return None
    channels: dict[str, str] = {}
    releases: list[_Ref] = []
    for line in (res.stdout or "").splitlines():
        parts = line.split(_SEP)
        if len(parts) != 4:
            continue
        refname, objectname, peeled, when = parts
        commit = peeled or objectname
        if refname.startswith("refs/tags/"):
            tag = refname[len("refs/tags/") :]
            if _RELEASE_TAG.match(tag):
                releases.append(_Ref(tag, commit, int(when or 0)))
        else:
            channels[refname.rsplit("/", 1)[-1]] = commit
    return channels, releases


def scan(repo, *, remote: str = "origin", now: float | None = None) -> dict | None:
    """Measure both channels in ``repo``, or ``None`` when this repo does not use the convention.

    **Silent unless the convention is clearly in use** — ``None`` unless *both* channel branches
    exist on ``remote`` *and* the repo has at least one `v<major>.<minor>.<patch>` tag. `stable` is
    a common branch name in repos that have never heard of this ADR, and a check that warns about
    someone else's `stable` is a check people learn to skip. Requiring `latest` *as a branch* — rare
    — plus semver release tags is a specific enough signature, and the failure mode of the
    discriminator is silence, not a false alarm.

    Returns a JSON-able dict:

    ``channels``
        ``{name: {"sha", "tag"}}``; ``tag`` is ``""`` when the commit carries no release tag.
    ``off_tag``
        channel names sitting on a commit no release tag points at. **Unambiguously wrong.**
    ``behind_releases``
        releases published after `stable`'s, up to and including `latest`'s. ``None`` when either
        channel is off-tag (there is then no position in the release order to measure from).
    ``behind_days``
        age of the **oldest** unpromoted release — how long the *first* missed promotion has been
        sitting, not how new the newest release is. That is the clock that actually measures "has
        anyone promoted lately"; the newest release's age resets on every publish and would read
        as fresh no matter how long `stable` had been frozen. ``0.0`` when nothing is unpromoted.
    ``oldest_unpromoted``
        the tag that clock is measured from, so a report can name it.

    Lag is measured against **`latest`, not the newest tag in the repo**: a tag exists before the
    publish gate clears it, so the newest tag may name a version that never reached PyPI. The ADR's
    Consequence 2 warns in as many words that "anything that asserts equality between them will
    flap"; `latest` is the newest *published* release by construction.
    """
    read = _read_refs(Path(repo), remote)
    if read is None:
        return None
    channels, releases = read
    if not all(c in channels for c in CHANNELS) or not releases:
        return None

    at_commit: dict[str, list[_Ref]] = {}
    for r in releases:
        at_commit.setdefault(r.commit, []).append(r)

    def _tag_at(commit: str) -> _Ref | None:
        # Several release tags on one commit is legal (a re-tag, or a version bump with no code
        # change). The highest version is the one the channel is meaningfully "at".
        found = at_commit.get(commit, [])
        return max(found, key=lambda r: _version(r.name)) if found else None

    tags = {c: _tag_at(channels[c]) for c in CHANNELS}
    out = {
        "remote": remote,
        "channels": {
            c: {"sha": channels[c], "tag": tags[c].name if tags[c] else ""} for c in CHANNELS
        },
        "off_tag": [c for c in CHANNELS if tags[c] is None],
        "behind_releases": None,
        "behind_days": None,
        "oldest_unpromoted": None,
    }
    if out["off_tag"]:
        return out

    low, high = _version(tags["stable"].name), _version(tags["latest"].name)
    unpromoted = sorted(
        (r for r in releases if low < _version(r.name) <= high), key=lambda r: _version(r.name)
    )
    out["behind_releases"] = len(unpromoted)
    if not unpromoted:
        out["behind_days"] = 0.0
        return out
    out["oldest_unpromoted"] = unpromoted[0].name
    clock = time.time() if now is None else now
    out["behind_days"] = max(0.0, (clock - unpromoted[0].when) / 86400.0)
    return out
