"""Negative controls for spike bh-gj1hb: each mutation must make at least one case fail.

Copies the artifacts into a scratch directory per mutation, applies one textual change, runs
the suite with the current interpreter, and prints the failing case ids. A mutation that
leaves the suite green is a gap in the conformance cases.

    usage: python mutation_controls.py <scratch-dir>
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

MUTATIONS = [
    (
        "github: state=open",
        "github_adapter.py",
        '"state": "all" if query.include_closed else "open"',
        '"state": "open"',
    ),
    ("github: keep PRs", "github_adapter.py", 'if "pull_request" not in r)', "if True)"),
    (
        "github: GitHub's default issue ordering",
        "github_adapter.py",
        '"sort": "updated",\n            "direction": "asc",',
        '"sort": "created",\n            "direction": "desc",',
    ),
    (
        "github: keep PR comments",
        "github_adapter.py",
        'if "/pull/" not in r["html_url"])',
        "if True)",
    ),
    (
        "github: retry an ambiguous write",
        "github_adapter.py",
        'return None, WriteOutcomeUnknown(f"GitHub answered {status} to a write")',
        'return 1.0, WriteOutcomeUnknown("retried")',
    ),
    (
        "github: ignore the enabled set",
        "github_adapter.py",
        "if op not in self._enabled:",
        "if False:",
    ),
    (
        "github: no single-wait cap",
        "github_adapter.py",
        "or wait > budget.max_single_wait.total_seconds()",
        "or False",
    ),
    (
        "github: no label ownership",
        "github_adapter.py",
        "if not ownership.owns(label))",
        "if False)",
    ),
    (
        "github: checkpoint mid-pull on an unordered listing",
        "github_adapter.py",
        'PageCursor(json.dumps({"next": nxt, "hw": top})), None)',
        'PageCursor(json.dumps({"next": nxt, "hw": top})), Checkpoint(_PREFIX + top))',
    ),
    (
        "memory: reusable grants",
        "memory_tracker.py",
        "if grant.grant_id in self._used_grants:",
        "if False:",
    ),
    (
        "memory: exclusive since",
        "memory_tracker.py",
        "and (since is None or r.updated >= since)",
        "and (since is None or r.updated > since)",
    ),
    (
        "memory: keep change-request comments",
        "memory_tracker.py",
        "and not self.world.issues[c.issue.identifier].change_request",
        "and True",
    ),
    (
        "snapshot: a failed listing counts as complete",
        "tracker_contracts.py",
        "return IdSnapshot(frozenset(ids), complete=False)",
        "return IdSnapshot(frozenset(ids), complete=True)",
    ),
]


def main(scratch: Path) -> int:
    survivors = 0
    for label, name, old, new in MUTATIONS:
        work = scratch / "mutant"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        for path in HERE.glob("*.py"):
            shutil.copy(path, work / path.name)
        target = work / name
        text = target.read_text(encoding="utf-8")
        assert old in text, f"{label}: anchor not found"
        target.write_text(text.replace(old, new, 1), encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-c", "/dev/null", f"--rootdir={work}"]
            + ["-p", "no:cacheprovider", "-q", str(work)],
            capture_output=True,
            text=True,
            check=False,
        )
        failed = sorted(set(re.findall(r"FAILED \S+::(\S+)", result.stdout)))
        survivors += not failed
        print(f"{label}: {', '.join(failed) if failed else 'SURVIVED (no case failed)'}")
    return 1 if survivors else 0


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1])))
