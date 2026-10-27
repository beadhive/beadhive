"""Integration: ``BdReportStore`` over a REAL ``bd`` store passes the ReportStore contract cases.

The same :data:`REPORT_STORE_CASES` that ``MemoryReportStore`` passes
(``tests/unit/testing/test_intake_report_store_kit.py``) run here against an embedded-Dolt scratch
hive, one fresh copy of an initialised template per case so no case sees another's beads. The
metadata cases then pin the documents ``intake.source`` rides on (external-intake-module-adr.md
section 4): a dotted key is one flat top-level key, ``--metadata`` merges top-level keys and
replaces a key's value whole, ``--has-metadata-key`` matches the exact dotted key, and metadata is
still writable on a closed bead.

Marked ``integration``; self-skips without ``bd`` on PATH. Nothing here touches a real hive: every
store lives in ``tmp_path``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from beadhive.adapters.intake import SOURCE_KEY, BdReportStore
from beadhive.modules.intake import IntakeStoreError
from beadhive.testing.intake import (
    REPORT_STORE_CASES,
    SCOPE,
    draft,
    run_report_store_case,
)
from beadhive_bd_cli import CliRoutes, SubprocessBd, err_line
from harness.beads import embedded_env, init_embedded, skip_if_no_bd
from harness.world import git

pytestmark = [pytest.mark.integration, skip_if_no_bd]

_BD = SubprocessBd(timeout=120, env=embedded_env())


@pytest.fixture(scope="module")
def template(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("intake-template")
    git("init", "-q", "-b", "main", cwd=root)
    init_embedded(root, SCOPE.operational_prefix)
    return root


@pytest.fixture
def fresh(template: Path, tmp_path: Path):
    """A factory of fresh, empty ``BdReportStore`` s: each call copies the initialised template."""
    made: list[Path] = []

    def factory() -> BdReportStore:
        target = tmp_path / f"store-{len(made)}"
        shutil.copytree(template, target)
        made.append(target)
        return BdReportStore(CliRoutes(_BD, target), SCOPE, error_line=err_line)

    return factory


@pytest.mark.parametrize("case", REPORT_STORE_CASES, ids=lambda case: case.case_id)
def test_bd_report_store_passes_the_report_store_contract(fresh, case) -> None:
    run_report_store_case(fresh, case, subject="BdReportStore over bd")


def test_dotted_metadata_keys_merge_replace_whole_and_filter_exactly(fresh) -> None:
    """ADR section 4's verification: ``intake.source`` / ``intake.thread`` / ``intake.outbox`` are
    flat keys. ``--metadata`` merges at the top level only and replaces a key's value whole;
    ``--has-metadata-key`` matches the dotted key exactly (never its ``intake`` prefix)."""
    store = fresh()
    routes = store._routes  # the same routes the store speaks
    bead = store.create(draft(bound=True))
    first = {"seq": 1, "nested": {"keep": True}, "drop": "me"}
    assert routes.issue_set_metadata_document(bead, "intake.thread", first).returncode == 0
    assert routes.issue_set_metadata_document(bead, "intake.outbox", {"seq": 1}).returncode == 0
    second = {"seq": 2, "nested": {"other": 1}}
    assert routes.issue_set_metadata_document(bead, "intake.thread", second).returncode == 0

    row = routes.issue_show_raw(bead)
    row = row[0] if isinstance(row, list) else row
    metadata = row["metadata"]
    assert metadata["intake.thread"] == second  # replaced whole: no "drop", no "keep"
    assert metadata["intake.outbox"] == {"seq": 1}  # another key survives
    assert SOURCE_KEY in metadata  # the create-time document survives both rewrites

    def holders(key: str) -> list[str]:
        return [r["id"] for r in routes.issue_list(has_metadata_key=key, all_=True, limit=0)]

    assert holders("intake.thread") == [bead]
    assert holders("intake.outbox") == [bead]
    assert holders("intake") == []  # a dot is part of the key, not a path
    assert holders("intake.thread.seq") == []
    assert holders("intake.missing") == []


def test_metadata_is_writable_on_a_closed_bead_and_a_big_document_travels_on_stdin(fresh) -> None:
    store = fresh()
    routes = store._routes
    bead = store.create(draft(bound=True))
    store.close(bead, reason="done", actor="dev/test")
    big = {"seq": 7, "comments": [{"body": "x" * 4000, "n": n} for n in range(200)]}  # ~800 KB
    assert routes.issue_set_metadata_document(bead, "intake.thread", big).returncode == 0
    row = routes.issue_show_raw(bead)
    row = row[0] if isinstance(row, list) else row
    assert row["status"] == "closed" and row["metadata"]["intake.thread"] == big
    assert [r["id"] for r in routes.issue_list(has_metadata_key="intake.thread", all_=True)] == [
        bead
    ]


def test_unknown_metadata_target_is_a_store_failure(fresh) -> None:
    store = fresh()
    with pytest.raises(IntakeStoreError):
        store.record_snapshot("acme-ghost", draft(bound=True).snapshot, actor="dev/test")
