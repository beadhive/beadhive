"""The declarative answers file for ``bh host provision`` (bh-q160.2).

``bh host provision`` takes only ``--role`` and derives or PROMPTS for everything else. That is
right at a terminal and useless from a script: an unattended install has nobody to answer, and
``--auto`` only says "take the derived value", which is a guess rather than a statement.

THE FILE IS SHORT ON PURPOSE. HQ already carries the fleet's truth — ``fleet.yaml`` holds orgs,
dimensions and managed_repos; ``workspace.toml`` holds the git-workspace providers. A host that
clones HQ inherits all of it. Only four things cannot be known until somebody decides them for
THIS host, and they are the only four keys here.

VALIDATION IS FAIL-FIRST AND FAIL-LOUD. An unknown key is an ERROR, not a warning, and it is
raised before step 1 runs. A typo'd key in a permissive parser is the worst possible outcome for
this file: `adopts:` instead of `adopt:` would silently provision a host that adopts nothing,
and the operator's actual goal — moving hives to be primary here — would vanish with no message
while every step reported success.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import hosts

#: Every key the file may carry. Anything else is a typo or a stale field, and both are errors.
KNOWN_KEYS: frozenset[str] = frozenset({"role", "hq.remote", "hives", "adopt"})

#: Keys with no default — the file is not a plan without them.
REQUIRED_KEYS: frozenset[str] = frozenset({"role"})


class AnswersInvalid(Exception):
    """The answers file cannot be used. Raised before provision touches anything."""


@dataclass(frozen=True)
class Answers:
    """One host's stated plan.

    ``hives is None`` means ALL registered hives, which is the sensible default for a first
    host. A subset is the point of a second host with less disk or a narrower scope, so an
    empty list is a legitimate answer meaning "clone none" and is NOT the same as omitting it.
    """

    role: str
    hq_remote: str = ""
    hives: list[str] | None = None
    adopt: list[str] = field(default_factory=list)

    @property
    def adopts_anything(self) -> bool:
        return bool(self.adopt)


def _as_str_list(value: Any, key: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise AnswersInvalid(f"`{key}` must be a list of hive prefixes — got {value!r}")
    return list(value)


def parse(raw: dict[str, Any]) -> Answers:
    """Validate *raw* and build :class:`Answers`, or raise :class:`AnswersInvalid`.

    Separated from file IO so the rules are testable without a tempfile, and so a caller that
    already has a mapping (a future ``bh host provision --answers -``) needs no round trip.
    """
    if not isinstance(raw, dict):
        raise AnswersInvalid(f"answers must be a mapping of keys — got {type(raw).__name__}")

    if unknown := sorted(set(raw) - KNOWN_KEYS):
        known = ", ".join(sorted(KNOWN_KEYS))
        raise AnswersInvalid(f"unknown key(s): {', '.join(unknown)} — allowed keys are: {known}")

    if missing := sorted(REQUIRED_KEYS - set(raw)):
        raise AnswersInvalid(f"missing required key(s): {', '.join(missing)}")

    role = raw["role"]
    if role not in hosts.HOST_ROLES:
        raise AnswersInvalid(f"`role` must be one of {list(hosts.HOST_ROLES)} — got {role!r}")

    hq_remote = raw.get("hq.remote", "")
    if not isinstance(hq_remote, str):
        raise AnswersInvalid(f"`hq.remote` must be a string — got {hq_remote!r}")

    hives = _as_str_list(raw["hives"], "hives") if "hives" in raw else None
    adopt = _as_str_list(raw["adopt"], "adopt") if "adopt" in raw else []

    # ADOPT IS NEVER DEFAULTED FROM HIVES. Cloning a hive is reversible and local; adopting one
    # CASes the hive's epoch fence and HQ's lease, which is fleet-visible and races other hosts.
    # An operator who lists a hive under `hives` has said "put a copy here", not "take primary
    # away from wherever it is now" — so the two lists stay independent even though `adopt`
    # being a subset of `hives` is the common case.
    if hives is not None and (orphans := sorted(set(adopt) - set(hives))):
        raise AnswersInvalid(
            f"`adopt` names hive(s) absent from `hives`: {', '.join(orphans)} — "
            "a host cannot hold primary for a hive it does not clone"
        )

    return Answers(role=role, hq_remote=hq_remote, hives=hives, adopt=adopt)


def load(path: Path) -> Answers:
    """Read and validate an answers file."""
    try:
        text = path.read_text()
    except OSError as exc:
        raise AnswersInvalid(f"cannot read answers file {path}: {exc}") from None
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise AnswersInvalid(f"answers file {path} is not valid YAML: {exc}") from None
    return parse(raw)
