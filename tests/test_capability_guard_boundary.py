"""Architecture: every write verb asks the guard for exactly one capability (bh-b3ebg).

Trust-profiles ADR section 1: "a new ``guard_capability(name)`` sits in front of every write
verb". Asserted on the source, so a new write verb cannot reach frame eligibility around the
capability model:

* ``guard_primary`` and ``require_intake`` are called only inside the guard layer
  (``guard.py``, and ``frame_eligibility.py`` where intake is defined);
* every ``guard_capability`` / ``require_capability`` call names one write capability as a
  literal (or ``place`` for ``guard_capability``: ``bh host lease adopt``, bh-vl0r3), so the
  capability a verb asks for is reviewable at the call site;
* the call sites and the capability each asks for are pinned below: adding a write verb means
  classifying it here.
"""

from __future__ import annotations

import ast
from functools import cache
from pathlib import Path

from beadhive import capabilities

ROOT = Path(__file__).resolve().parents[1]
SOURCES = sorted(
    [
        *(ROOT / "src" / "beadhive").rglob("*.py"),
        *(ROOT / "packages").glob("*/src/**/*.py"),
    ]
)

GUARD_LAYER = {"src/beadhive/guard.py", "src/beadhive/frame_eligibility.py"}
FORBIDDEN = {"guard_primary", "require_intake"}
CAPABILITY_GUARDS = {"guard_capability", "require_capability"}

#: module -> (guard, capability) for every capability-guarded write site.
EXPECTED = {
    "src/beadhive/cli.py": {("guard_capability", "author")},  # hive policy set/unset
    "src/beadhive/host_cli.py": {("guard_capability", "place")},  # host lease adopt (bh-vl0r3)
    "src/beadhive/localloop.py": {("require_capability", "execute")},  # dispatch lease keeper
    "src/beadhive/plan.py": {("guard_capability", "author")},  # plan file, plan approve
    "src/beadhive/work.py": {("require_capability", "claim")},  # work start (epic claim)
    "src/beadhive/work_dispatch.py": {("guard_capability", "execute")},  # work next, loop
    "src/beadhive/work_group.py": {("require_capability", "claim")},  # group claim
    "src/beadhive/work_lifecycle.py": {
        ("guard_capability", "claim"),  # work assign, work claim
        ("require_capability", "claim"),  # the claim-adjacent recheck (claim, resume)
    },
    "src/beadhive/work_merge.py": {("guard_capability", "publish")},
    "src/beadhive/work_submission.py": {("guard_capability", "publish")},
}


@cache
def _calls(path: Path) -> tuple[tuple[str, ast.Call], ...]:
    """Every named call in `path`; only files that mention a guard name are parsed."""
    source = path.read_text(encoding="utf-8")
    if not any(name in source for name in FORBIDDEN | CAPABILITY_GUARDS):
        return ()
    calls = []
    for node in ast.walk(ast.parse(source, filename=str(path))):
        if isinstance(node, ast.Call):
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else ""
            )
            if name:
                calls.append((name, node))
    return tuple(calls)


def _relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def test_sources_were_found():
    assert any(_relative(path) == "src/beadhive/guard.py" for path in SOURCES)


def test_no_direct_guard_primary_or_require_intake_outside_the_guard_layer():
    offenders = [
        f"{_relative(path)}:{node.lineno} {name}"
        for path in SOURCES
        if _relative(path) not in GUARD_LAYER
        for name, node in _calls(path)
        if name in FORBIDDEN
    ]
    assert offenders == [], "call guard.guard_capability / guard.require_capability instead"


def test_every_capability_guard_call_names_one_write_capability_literally():
    found: dict[str, set[tuple[str, str]]] = {}
    for path in SOURCES:
        relative = _relative(path)
        for name, node in _calls(path):
            if name not in CAPABILITY_GUARDS:
                continue
            first = node.args[0] if node.args else None
            assert isinstance(first, ast.Constant) and isinstance(first.value, str), (
                f"{relative}:{node.lineno}: {name} needs a literal capability"
            )
            # ``place`` is the one non-write capability guard_capability takes: it delegates to
            # the self-placement check (bh-vl0r3); require_capability stays write-only.
            allowed = capabilities.WRITE_CAPABILITIES + (
                (capabilities.PLACE,) if name == "guard_capability" else ()
            )
            assert first.value in allowed, (
                f"{relative}:{node.lineno}: {first.value!r} is not a write capability"
            )
            found.setdefault(relative, set()).add((name, first.value))
    assert found == EXPECTED
