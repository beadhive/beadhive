"""bh-1j3ei.2: a new tracked file with no owning Pants target must fail closed, no allowlist."""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts import check_pants_ownership as ownership  # noqa: E402


def test_fully_owned_tree_reports_nothing_unowned() -> None:
    tracked = {"README.md", "src/beadhive/BUILD", "src/beadhive/x.py"}
    owned = {"README.md", "src/beadhive/BUILD", "src/beadhive/x.py", "src/beadhive/y.py"}

    assert ownership.unowned(tracked, owned) == set()


def test_new_tracked_file_with_no_build_entry_is_reported_unowned() -> None:
    tracked = {"README.md", "docs/NEW-DOC.md"}
    owned = {"README.md"}

    assert ownership.unowned(tracked, owned) == {"docs/NEW-DOC.md"}


def test_report_lists_every_unowned_path_and_has_no_allowlist_escape_hatch() -> None:
    text = ownership.report({"scripts/new_tool.py", "docs/new.md"})

    assert "scripts/new_tool.py" in text
    assert "docs/new.md" in text
    assert "2 unowned" in text
    assert "No allowlist" in text


def test_main_fails_closed_when_a_file_is_unowned(monkeypatch) -> None:
    monkeypatch.setattr(ownership, "tracked_files", lambda: {"a", "b"})
    monkeypatch.setattr(ownership, "pants_owned_files", lambda: {"a"})

    assert ownership.main() == 1


def test_main_succeeds_when_every_tracked_file_is_owned(monkeypatch) -> None:
    monkeypatch.setattr(ownership, "tracked_files", lambda: {"a", "b"})
    monkeypatch.setattr(ownership, "pants_owned_files", lambda: {"a", "b", "BUILD"})

    assert ownership.main() == 0


def test_main_fails_closed_on_a_query_error(monkeypatch) -> None:
    def _raise() -> set[str]:
        raise ownership.OwnershipCheckError("pants unavailable")

    monkeypatch.setattr(ownership, "tracked_files", _raise)

    assert ownership.main() == 1
