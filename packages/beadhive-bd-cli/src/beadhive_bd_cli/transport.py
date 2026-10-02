"""The one seam every ``bd`` argv route in this package runs through, plus ``bd``'s output parsing.

A route here only SHAPES a ``bd`` call (its argv) and INTERPRETS the result; it never spawns a
process itself. The process is the caller's :class:`BdTransport`: the installed ``beadhive``
compatibility shell supplies its own ``bd`` invocation seam (``-C <hive>`` scoping, the audit
``--actor``, the configured engine, strict-read narration), and :class:`SubprocessBd` is the plain
``subprocess`` transport for everything else — this package's real-``bd`` tests, and any caller
without a shell of its own.

``err_line`` and ``names_bead`` are pure parsers of ``bd``'s own output and gate descriptions. They
live here, next to the routes that depend on them, and the shell re-exports them by name.
"""

from __future__ import annotations

import json as _json
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

__all__ = [
    "BdResult",
    "BdTransport",
    "SubprocessBd",
    "err_line",
    "names_bead",
    "parse_json_tail",
]


class BdResult(Protocol):
    """A completed ``bd`` process — ``subprocess.CompletedProcess`` satisfies it."""

    returncode: int
    stdout: Any
    stderr: Any


class BdTransport(Protocol):
    """Run one ``bd`` subcommand against the hive at ``cwd``.

    ``run`` prepends the hive scoping and ``--actor`` itself: callers pass the bare subcommand.
    ``json`` appends ``--json`` and returns the parsed payload, or ``None`` when ``bd`` failed or
    printed no valid JSON (the None-on-failure contract every read route here relies on);
    ``strict=True`` asks the transport to raise instead of returning ``None`` when the ``bd``
    binary itself is absent, if the transport can tell.
    """

    def run(
        self,
        args: list[str],
        cwd: Any,
        actor: str = "",
        capture: bool = False,
        text_input: str | None = None,
        *,
        timeout: float | None = None,
    ) -> BdResult: ...

    def json(self, args: list[str], cwd: Any, *, strict: bool = False) -> Any: ...


class SubprocessBd:
    """A plain ``subprocess`` :class:`BdTransport`: ``bd -C <cwd> [--actor A] <args>``.

    ``env`` replaces the child's environment when given (``None`` inherits the caller's)."""

    def __init__(
        self,
        binary: str = "bd",
        *,
        timeout: float = 120.0,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._binary = binary
        self._timeout = timeout
        self._env = dict(env) if env is not None else None

    def run(
        self,
        args: list[str],
        cwd: Any,
        actor: str = "",
        capture: bool = False,
        text_input: str | None = None,
        *,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        argv = [self._binary, "-C", str(Path(cwd)), *(["--actor", actor] if actor else []), *args]
        return subprocess.run(
            argv,
            capture_output=capture,
            text=True,
            input=text_input,
            timeout=self._timeout if timeout is None else timeout,
            env=self._env,
            check=False,
        )

    def json(self, args: list[str], cwd: Any, *, strict: bool = False) -> Any:
        result = self.run([*args, "--json"], cwd, capture=True)
        if result.returncode != 0:
            return None
        try:
            return _json.loads(result.stdout or "null")
        except _json.JSONDecodeError:
            return None


_ADVISORY_LINE_PREFIXES = ("Notice:", "Hint:")


def err_line(res: Any) -> str:
    """The significant bd failure line, skipping leading informational advisory blocks."""
    first = ""
    significant: list[str] = []
    in_advisory_block = False
    for line in ((res.stdout or "") + (res.stderr or "")).splitlines():
        stripped = line.strip()
        if not stripped:
            in_advisory_block = False
            continue
        if not first:
            first = stripped
        if in_advisory_block and line[:1].isspace():
            continue
        in_advisory_block = stripped.startswith(_ADVISORY_LINE_PREFIXES)
        if not in_advisory_block:
            significant.append(stripped)
    for line in significant:
        if line.startswith("Error:"):
            return line
    if significant:
        return significant[0]
    if first:
        return first
    return f"exit {res.returncode}"


# Characters that may continue a bead id (`bh-baml-m76.10`, `bh-1vvdp`). Used to anchor id
# matching so an id is only ever matched as a WHOLE token.
_ID_CHARS = r"A-Za-z0-9._-"


def names_bead(desc: Any, bead: Any) -> bool:
    """True iff `desc` names `bead` as a whole id rather than as a prefix of a longer sibling.

    Gate identity is description-based (`bd gate create --blocks <bead>` writes the id into the
    text), so the match must be anchored: a plain substring test makes every `.1` the owner of
    `.10`/`.11`/`.12` (bh-1vvdp), which is deterministic for any molecule with 10+ children and
    silently resolves a sibling's human review gate — an integrity boundary — as a side effect of
    an ordinary submit. Anchoring both sides is what makes the id a token rather than a prefix.
    Case-insensitive, matching the callers' previous `.lower()` behaviour."""
    return bool(
        re.search(
            rf"(?<![{_ID_CHARS}]){re.escape(str(bead))}(?![{_ID_CHARS}])",
            str(desc or ""),
            re.IGNORECASE,
        )
    )


def parse_json_tail(stdout: Any) -> Any:
    """Parse the trailing JSON object off `stdout`, tolerating the human-readable progress
    lines bd emits alongside `--json` on some verbs. Verified live: `bd gate check --json`
    still prints one `✓ <id>: resolved - ...` line per gate it closes, THEN a pretty-printed
    JSON object, even with `--json` set — `--json` changes the SUMMARY's shape, not whether the
    per-item lines are suppressed. The JSON block always starts at a line that is exactly `{`;
    take the last one so multiple per-item confirmations before it are never mistaken for the
    payload. Returns None if no such block parses."""
    text = stdout or ""
    try:
        return _json.loads(text)  # the common case: stdout IS the JSON, nothing prefixed
    except ValueError:
        pass
    lines = text.splitlines()
    starts = [i for i, ln in enumerate(lines) if ln.strip() == "{"]
    if not starts:
        return None
    try:
        return _json.loads("\n".join(lines[starts[-1] :]))
    except ValueError:
        return None
