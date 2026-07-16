"""Tests for ws report-target (bead).

Validates that:
- ``report_target.self_document()`` returns a descriptor with the expected shape
  (``kind: beads-rig``, a non-empty ``target`` triplet, a ``verb`` containing the triplet).
- The returned document validates against the .2 schema shipped at
  ``docs/schemas/report-channel.schema.json`` (reuses the same validation approach as
  ``tests/test_report_channel.py``).
- The ``emit()`` function returns 0 on success and produces sensible output.

The triplet resolution is exercised by patching ``report_target._resolve_self_triplet``
so the tests never require a real git workspace or managed-rig config.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from beadhive import report_target

jsonschema = pytest.importorskip("jsonschema")

_SCHEMA_PATH = (
    Path(__file__).resolve().parents[1] / "docs" / "schemas" / "report-channel.schema.json"
)
_TRIPLET = ("github", "briancripe", "workspace")
_TRIPLET_STR = "github/briancripe/workspace"


def _load_schema() -> dict:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


# ---- self_document -----------------------------------------------------------


def test_self_document_returns_valid_discovery_document():
    """ACCEPTANCE: ``self_document()`` returns a document that validates against the .2 schema."""
    with patch.object(report_target, "_resolve_self_triplet", return_value=_TRIPLET):
        doc = report_target.self_document()

    assert doc is not None
    schema = _load_schema()
    jsonschema.Draft202012Validator(schema).validate(doc)


def test_self_document_has_beads_hive_channel():
    """The primary channel is ``kind: beads-rig`` pointing at the ws rig triplet."""
    with patch.object(report_target, "_resolve_self_triplet", return_value=_TRIPLET):
        doc = report_target.self_document()

    assert doc is not None
    channels = doc["channels"]
    assert len(channels) >= 1
    ch = channels[0]
    assert ch["kind"] == "beads-rig"
    assert ch["target"] == _TRIPLET_STR


def test_self_document_verb_contains_triplet():
    """The ``verb`` advisory hint includes the rig triplet so a consumer can copy-paste it."""
    with patch.object(report_target, "_resolve_self_triplet", return_value=_TRIPLET):
        doc = report_target.self_document()

    assert doc is not None
    verb = doc["channels"][0].get("verb", "")
    assert _TRIPLET_STR in verb


def test_self_document_returns_none_when_identity_unresolvable():
    """When the triplet cannot be resolved (outside a managed workspace), ``None`` is returned."""
    with patch.object(report_target, "_resolve_self_triplet", return_value=None):
        assert report_target.self_document() is None


# ---- emit --------------------------------------------------------------------


def test_emit_json_is_valid_discovery_document(capsys):
    """``emit(as_json=True)`` writes a schema-valid JSON document to stdout."""
    with patch.object(report_target, "_resolve_self_triplet", return_value=_TRIPLET):
        code = report_target.emit(as_json=True)

    assert code == 0
    captured = capsys.readouterr()
    doc = json.loads(captured.out)
    schema = _load_schema()
    jsonschema.Draft202012Validator(schema).validate(doc)


def test_emit_human_readable_contains_triplet(capsys):
    """The default (human-readable) output includes the rig triplet."""
    with patch.object(report_target, "_resolve_self_triplet", return_value=_TRIPLET):
        code = report_target.emit(as_json=False)

    assert code == 0
    captured = capsys.readouterr()
    assert _TRIPLET_STR in captured.out


def test_emit_returns_error_when_identity_unresolvable(capsys):
    """``emit()`` returns exit code 1 when the triplet cannot be resolved."""
    with patch.object(report_target, "_resolve_self_triplet", return_value=None):
        code = report_target.emit()

    assert code == 1
    captured = capsys.readouterr()
    assert captured.err  # error message goes to stderr


def test_emit_warns_prereq_when_hive_unregistered(capsys, monkeypatch):
    """bh-pfgx: when the self triplet isn't locally registered, emit() prints the exact
    `rig add ... --prefix=...` prerequisite alongside the verb."""
    from beadhive import config

    monkeypatch.setattr(config, "load", lambda: {"managed_repos": [], "orgs": {}})
    with patch.object(report_target, "_resolve_self_triplet", return_value=_TRIPLET):
        code = report_target.emit(as_json=False)

    assert code == 0
    out = capsys.readouterr().out
    assert f"prereq: {config.BINARY_ALIAS} rig add {_TRIPLET_STR} --prefix=" in out


def test_emit_no_prereq_when_hive_registered(capsys, monkeypatch):
    """bh-pfgx: an already-registered rig gets no prerequisite line."""
    from beadhive import config

    provider, org, repo = _TRIPLET
    cfg = {
        "managed_repos": [
            {"provider": provider, "org": org, "repo": repo, "prefix": "bc-workspace"}
        ],
        "orgs": {},
    }
    monkeypatch.setattr(config, "load", lambda: cfg)
    with patch.object(report_target, "_resolve_self_triplet", return_value=_TRIPLET):
        code = report_target.emit(as_json=False)

    assert code == 0
    out = capsys.readouterr().out
    assert "prereq:" not in out
