"""bead↔commit linkage — the ``git.commits`` metadata contract (bh-1b0rc.1).

See `docs/design/bead-commit-linkage-contract.md` for the full decision record; this module is a
pure implementation of it, not a place to re-decide it. Summary: a **flat** metadata key
literally named ``git.commits`` (the dot is part of the key's string, NOT a nested
``git`` → ``commits`` JSON path) holds a JSON array of full 40-character commit SHAs, itself
serialized as a JSON string — the only shape ``bd update --set-metadata`` can hold, since its
value is always a raw string, never parsed.

This module is the ONE place the accumulate/idempotent-write algorithm lives: read current →
diff against what's new → skip the write entirely when nothing is new → else append (existing
order preserved, new ones at the end) and write back. `bh work submit` / `bh work merge`
(bh-1b0rc.2) and the full-history backfill (bh-1b0rc.3) all call this rather than each
reimplementing the algorithm and risking drift from the contract.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import bd

#: The flat metadata key name — literally "git.commits", never a nested {"git": {"commits": …}}.
METADATA_KEY = "git.commits"


def read_commits(bead_id: str, main: Path) -> list[str]:
    """Current `git.commits` for `bead_id`, JSON-parsed.

    Per the contract, anything short of a clean JSON array of strings is treated as `[]` rather
    than an error: a missing bead, a missing key, an unparseable value, or a value that parses to
    something other than a list of strings. Treating unparseable linkage as fatal would let one
    malformed bead break a whole caller (a submit, a merge, the backfill)."""
    data = bd.show(bead_id, main)
    raw = (data or {}).get("metadata", {}).get(METADATA_KEY)
    if not isinstance(raw, str):
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list) or not all(isinstance(sha, str) for sha in parsed):
        return []
    return parsed


def record_commits(bead_id: str, main: Path, shas: list[str]) -> bool:
    """Append `shas` (full 40-char SHAs) onto `bead_id`'s `git.commits`, idempotently.

    Follows the contract's accumulate-never-overwrite algorithm: read the current list, diff
    which of `shas` aren't already present (also de-duping within `shas` itself), and — the
    load-bearing step — skip the `bd update` call ENTIRELY when nothing is new, rather than
    relying on `bd` detecting a byte-identical write as a no-op (an undocumented implementation
    detail per the contract doc, not something to depend on). Existing order is preserved; new
    SHAs are appended at the end.

    Returns True if a write happened, False if every sha was already present. Raises on a
    genuine `bd` failure — callers decide whether that's fatal or merely a warning."""
    existing = read_commits(bead_id, main)
    seen = set(existing)
    new: list[str] = []
    for sha in shas:
        if sha in seen:
            continue
        seen.add(sha)
        new.append(sha)
    if not new:
        return False
    merged = existing + new
    value = f"{METADATA_KEY}={json.dumps(merged)}"
    res = bd.run(["update", bead_id, "--set-metadata", value], main)
    if res.returncode != 0:
        raise RuntimeError(f"bd update --set-metadata failed for {bead_id}: {bd.err_line(res)}")
    return True
