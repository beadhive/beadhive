"""``scripts/repair_duplicate_delivery.py`` (bh-5kpwc): a dry-run listing by default, a clear only
with ``--apply``, and never a write to GitHub or the outbox. The sweep itself is covered over the
memory store in ``tests/unit/modules/intake/test_intake_delivery_sweep.py``; here only the script's
wiring is, with ``intake_services.sweep_delivery`` stubbed so no hive is read."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from beadhive import identity, intake_services
from beadhive.modules.intake import DeliverySweep, StrayDelivery

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "repair_duplicate_delivery", ROOT / "scripts/repair_duplicate_delivery.py"
)
assert SPEC is not None and SPEC.loader is not None
script = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(script)

STRAY = StrayDelivery("bh-0d5th", "pending")


@pytest.fixture
def calls(monkeypatch) -> list[dict]:
    seen: list[dict] = []

    def sweep(directory, **kwargs):
        seen.append({"directory": directory, **kwargs})
        found = (STRAY,)
        if kwargs["repair"]:
            return DeliverySweep(found, cleared=found)
        return DeliverySweep(found)

    monkeypatch.setattr(intake_services, "sweep_delivery", sweep)
    monkeypatch.setattr(identity, "resolve_actor", lambda explicit="", **_kw: explicit or "dev/me")
    return seen


def test_without_apply_it_only_lists_the_reports(calls, capsys) -> None:
    assert script.main(["--repo", "/hive"]) == 0

    assert calls == [{"directory": Path("/hive"), "repair": False, "actor": "", "cli_only": True}]
    out = capsys.readouterr().out
    assert "bh-0d5th\tdelivery:pending" in out and "dry run: 1 report(s)" in out
    assert "cleared" not in out


def test_apply_clears_and_names_the_label_sync_that_drafts_the_removal(calls, capsys) -> None:
    assert script.main(["--repo", "/hive", "--apply", "--as", "dev/ops"]) == 0

    assert calls[0]["repair"] is True and calls[0]["actor"] == "dev/ops"
    out = capsys.readouterr().out
    assert "cleared delivery:pending from bh-0d5th" in out
    assert "bh work labels sync --include-closed" in out


def test_json_reports_the_findings_and_what_was_cleared(calls, capsys) -> None:
    assert script.main(["--repo", "/hive", "--apply", "--json"]) == 0

    assert json.loads(capsys.readouterr().out) == {
        "applied": True,
        "findings": [{"report": "bh-0d5th", "delivery": "pending"}],
        "cleared": ["bh-0d5th"],
        "failed": [],
    }


def test_a_failed_clear_exits_non_zero(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        intake_services,
        "sweep_delivery",
        lambda directory, **kw: DeliverySweep((STRAY,), failed=((STRAY, "label store down"),)),
    )

    assert script.main(["--repo", "/hive", "--apply", "--as", "dev/ops"]) == 1
    assert "bh-0d5th: label store down" in capsys.readouterr().err
