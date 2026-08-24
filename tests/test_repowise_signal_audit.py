"""Completeness contract for the RepoWise signal adjudication ledger."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUDIT = ROOT / "docs" / "design" / "repowise-signal-audit.md"


def test_audit_classifies_every_emitted_dead_code_candidate_once():
    text = AUDIT.read_text()
    ids = re.findall(r"^\| (D\d{2}) \|", text, flags=re.MULTILINE)

    assert ids == [f"D{i:02}" for i in range(1, 44)]
    assert "42 persisted" in text
    assert "one `zombie_package`" in text


def test_audit_records_required_dynamic_and_security_boundaries():
    text = AUDIT.read_text()

    for evidence in (
        "_chk_valid_triplet",
        "_stable_versioning",
        "preexec_fn=_die_with_parent",
        "sanitize_database_name",
        "falls back to per-hive `bd show`",
        "create_subprocess_exec(*argv",
        "bh-bhsqp",
    ):
        assert evidence in text

    for verdict in (
        "false positive",
        "compatibility surface",
        "owned elsewhere",
        "proven removable",
    ):
        assert f"**{verdict}**" in text
