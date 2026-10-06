"""Dolt/bd trigger-semantics canary for the hive write guard (bh-p07dv). Test-only.

ADR ``docs/design/hive-writer-partitioning-adr.md`` binding **condition 9**: the guard keeps the
``bh-sieai`` shape and table set, adopt checks for 44 triggers, and *the Dolt/bd trigger-semantics
tests re-run on every Dolt or bd pin bump*. The guard's shape is not a free choice -- it is
dictated by engine behaviour measured on Dolt 2.3.5 (``bh-sieai`` Evidence 1 / Recommendation 5,
``bh-vje85``, ``bh-jbb6r`` E6, ``bh-wtsrc`` E3). A pin bump that changes any of it can turn the
guard fail-open without one existing test going red. This module holds the canary's shared
pieces; ``tests/test_fence_trigger_canary_int.py`` drives the engines and
``tests/test_fence_trigger_canary_pin.py`` is the fast-gate tripwire that forces a re-run.

How it is selected when the pin moves (documented beside the pins in ``flake.nix``):

* :data:`CANARY_PINS` records the Dolt and bd versions the canary last passed on. The fast gate
  (``just check``) compares it against the pins in ``flake.nix`` and the generated
  ``docker/toolchain-metadata.json``; a bump that does not also move :data:`CANARY_PINS` fails
  there, naming ``just fence-canary``.
* The canary tests are ``integration`` tests carrying the ``fence_canary`` marker, so the land
  gate's integration pass runs them on every land, and ``docker/**`` (where every pin bump
  regenerates ``toolchain-metadata.json``) selects the ``integration`` attest key.
* The canary asserts the *installed* ``dolt`` / ``bd`` equal :data:`CANARY_PINS`, so updating the
  constant without installing and running the bumped binaries also fails.

Every assumption failure raises :class:`AssumptionBroken` with a message naming condition 9 and
the specific assumption, so a red canary says which guard property the new engine broke.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from harness import composed_fence as cf
from harness import write_guard as wg

__all__ = [
    "CANARY_PINS",
    "COMPOSED_TRIGGER_COUNT",
    "CONDITION",
    "AssumptionBroken",
    "DoltRepo",
    "check_guard_marks",
    "expect",
    "guard_shape_violations",
    "installed_versions",
    "pinned_versions",
]

REPO_ROOT = Path(__file__).resolve().parents[2]
CONDITION = "ADR condition 9 (bh-sieai R5, bh-jbb6r E6)"

#: The Dolt and bd versions this canary last passed against. Move it ONLY after
#: ``just fence-canary`` is green on the newly pinned binaries.
CANARY_PINS: dict[str, str] = {"dolt": "2.3.5", "bd": "1.3.0"}

#: bh-sieai's 42 guard triggers (14 tables x 3 events) + bh-vje85's two monotonic fence triggers.
#: Literal on purpose: adopt checks for exactly this number (condition 9), so a change to the
#: table set must be a deliberate edit here too, not a silent consequence of ``GUARDED_TABLES``.
COMPOSED_TRIGGER_COUNT = 44

ISSUE_INSERT = (
    "insert into issues (id, title, description, design, acceptance_criteria, notes) "
    "values ('{id}', '{id}', '', '', '', '')"
)
ISSUES_DDL = (
    "create table issues (id varchar(32) primary key, title text, description text, "
    "design text, acceptance_criteria text, notes text);"
)


class AssumptionBroken(AssertionError):
    """A Dolt/bd trigger behaviour the write guard is built on no longer holds."""


def expect(ok: bool, assumption: str, detail: str = "") -> None:
    """Fail with a message naming condition 9 and ``assumption`` unless ``ok``."""
    if ok:
        return
    raise AssumptionBroken(
        f"{CONDITION} assumption broken: {assumption}"
        + (f" -- {detail}" if detail else "")
        + ". The write guard's shape (tests/harness/write_guard.py) depends on it: re-evaluate"
        " the guard against docs/spikes/bh-sieai-write-guard-and-nonprimary-writes.md before"
        " moving the Dolt/bd pin."
    )


# --------------------------------------------------------------------------------------------
# Pins


def pinned_versions(root: Path = REPO_ROOT) -> dict[str, dict[str, str | None]]:
    """The Dolt and bd versions each pin source names: ``{source: {"dolt": v, "bd": v}}``.

    ``flake.nix`` is the source of truth (release archive URLs); ``docker/toolchain-metadata.json``
    is generated from it and committed like a lockfile."""
    flake = (root / "flake.nix").read_text()

    def from_url(project: str) -> str | None:
        found = set(re.findall(rf"{project}/releases/download/v([0-9][^/\"]*)/", flake))
        return found.pop() if len(found) == 1 else None

    metadata = json.loads((root / "docker" / "toolchain-metadata.json").read_text())
    by_name = {row["name"]: row["version"] for row in metadata}
    return {
        "flake.nix": {"dolt": from_url("dolthub/dolt"), "bd": from_url("beads")},
        "docker/toolchain-metadata.json": {"dolt": by_name.get("dolt"), "bd": by_name.get("bd")},
    }


def installed_versions() -> dict[str, str | None]:
    """The ``dolt`` / ``bd`` on ``PATH`` (what the canary actually measures)."""

    def probe(argv: list[str], pattern: str) -> str | None:
        try:
            out = subprocess.run(argv, capture_output=True, text=True, timeout=60).stdout
        except (OSError, subprocess.TimeoutExpired):
            return None
        match = re.search(pattern, out)
        return match.group(1) if match else None

    return {
        "dolt": probe(["dolt", "version"], r"dolt version (\S+)"),
        "bd": probe(["bd", "version"], r"bd version (\S+)"),
    }


# --------------------------------------------------------------------------------------------
# Static guard shape


_TRIGGER = re.compile(r"create trigger (\w+) .*?\nend//", re.DOTALL | re.IGNORECASE)


def guard_shape_violations(ddl: str) -> list[str]:
    """Static check of a guard script: in every ``bh_guard_*`` trigger the mark is inserted
    inline, BEFORE the ``CALL``, the ``CALL`` is the last statement, and no ``@`` variable is
    used (each a measured Dolt 2.3.5 trigger behaviour, bh-sieai Evidence 1)."""
    problems: list[str] = []
    seen = 0
    for match in _TRIGGER.finditer(ddl):
        name, body = match.group(1), match.group(0).lower()
        if not name.startswith("bh_guard_"):
            continue
        seen += 1
        mark = body.find("insert into bh_write_mark")
        call = body.find("call bh_guard_check()")
        if mark < 0:
            problems.append(f"{name}: no inline bh_write_mark insert in the trigger body")
        if call < 0:
            problems.append(f"{name}: no CALL bh_guard_check()")
        if mark >= 0 and call >= 0 and mark > call:
            problems.append(f"{name}: mark insert comes after the CALL (Dolt skips it)")
        if call >= 0:
            tail = re.sub(r"\s+", " ", body[call + len("call bh_guard_check();") :]).strip()
            if tail != "end if; end//":
                problems.append(f"{name}: statements follow the CALL ({tail!r})")
        if "@" in body:
            problems.append(f"{name}: @user variable in a trigger body (fails open)")
    if not seen:
        problems.append("no bh_guard_* triggers in the script")
    return problems


def composed_trigger_total() -> int:
    """``create trigger`` statements in the composed fence + guard script."""
    return len(re.findall(r"^create trigger ", cf.fence_ddl("x", 1), re.MULTILINE | re.I))


# --------------------------------------------------------------------------------------------
# Dolt CLI repo


class DoltRepo:
    """A Dolt CLI repo with its own HOME / DOLT_ROOT_PATH (never the operator's)."""

    def __init__(self, root: Path, name: str = "canary"):
        self.dir = root / name
        self.home = root / f"{name}-home"
        (self.home / ".dolt").mkdir(parents=True)
        (self.home / ".dolt" / "config_global.json").write_text(
            json.dumps(
                {
                    "user.name": name,
                    "user.email": f"{name}@canary.invalid",
                    "metrics.disabled": "true",
                    "versioncheck.disabled": "true",
                }
            )
        )
        self.env = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.home),
            "DOLT_ROOT_PATH": str(self.home),
        }
        self.dir.mkdir()
        self.ok("init", "-b", "main")

    def run(self, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["dolt", *args],
            cwd=self.dir,
            env=self.env,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def ok(self, *args: str, stdin: str | None = None) -> str:
        res = self.run(*args, stdin=stdin)
        assert res.returncode == 0, f"dolt {' '.join(args)}: {res.stdout}{res.stderr}"
        return res.stdout

    def sql(self, script: str) -> subprocess.CompletedProcess:
        return self.run("sql", stdin=script)

    def rows(self, query: str) -> list[dict]:
        out = self.ok("sql", "-r", "json", "-q", query).strip()
        return json.loads(out).get("rows", []) if out else []

    def count(self, query: str) -> int:
        return int(next(iter(self.rows(query)[0].values())))


def refusal(res: subprocess.CompletedProcess) -> str | None:
    """The error text of a refused statement, or ``None`` if it was accepted."""
    return None if res.returncode == 0 else res.stdout + res.stderr


# --------------------------------------------------------------------------------------------
# Guard semantics on the Dolt engine


def check_guard_marks(repo: DoltRepo, ddl: str, epoch: int = 7) -> None:
    """Install ``ddl`` (a guard script for ``issues``) and pin what the guard relies on: every
    writer statement -- autocommit or inside ``BEGIN ... COMMIT`` (bd's ``withRetryTx``),
    single- or multi-row -- leaves exactly one mark at the writer's epoch, and a refusal rolls
    the statement's mark back."""
    repo.ok("sql", stdin=ISSUES_DDL)
    repo.ok("sql", stdin=ddl)
    repo.ok("sql", "-q", f"insert into bh_writer values (1, 'A', {epoch})")
    repo.ok("sql", "-q", "call dolt_commit('-Am', 'guard')")
    repo.ok(
        "sql",
        "-q",
        "create table bh_local_ident (id int primary key, frame varchar(64), role varchar(16)); "
        "insert into bh_local_ident values (1, 'A', 'replica')",
    )
    multi = ISSUE_INSERT.format(id="{a}") + ", ('{b}', '{b}', '', '', '', '')"
    statements = {
        "autocommit single-row": ISSUE_INSERT.format(id="a1"),
        "explicit-transaction single-row": "begin;\n" + ISSUE_INSERT.format(id="a2") + ";\ncommit;",
        "autocommit multi-row": multi.format(a="a3", b="a4"),
        "explicit-transaction multi-row": "begin;\n" + multi.format(a="a5", b="a6") + ";\ncommit;",
    }
    for label, stmt in statements.items():
        before = repo.count("select count(*) from bh_write_mark")
        res = repo.sql(stmt)
        expect(res.returncode == 0, "the writer's own guarded write is accepted", label)
        after = repo.count("select count(*) from bh_write_mark")
        expect(
            after - before == 1,
            "BEFORE-trigger inline mark insert with the CALL last leaves one mark per statement",
            f"{label}: {after - before} marks",
        )
    marks = repo.rows("select epoch, tbl, count(*) n from bh_write_mark group by epoch, tbl")
    expect(
        marks == [{"epoch": epoch, "tbl": "issues", "n": len(statements)}],
        "every mark carries the writer's epoch",
        repr(marks),
    )
    repo.ok("sql", "-q", "update bh_local_ident set frame = 'B'")
    refused = refusal(repo.sql("begin;\n" + ISSUE_INSERT.format(id="b1") + ";\ncommit;"))
    expect(
        refused is not None and wg.GUARD_REFUSAL in refused,
        "the guard's SIGNAL from the CALLed procedure refuses a non-writer's write",
        refused or "write accepted",
    )
    expect(
        repo.count("select count(*) from bh_write_mark") == len(statements),
        "a refusal rolls the inline mark back with the statement",
    )
