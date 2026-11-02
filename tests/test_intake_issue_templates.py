"""The versioned GitHub issue forms and docs/INTAKE-TEMPLATES.md agree (bh-ncb0d).

Local structural check of the issue-forms schema (no network): required top-level keys, known
element types, unique ids, dropdown options, and the `template_version` marker. The doc's per-type
tables are the checklist triage reads, so field ids and required-ness must match the forms.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[1]
FORMS = ROOT / ".github" / "ISSUE_TEMPLATE"
DOC = ROOT / "docs" / "INTAKE-TEMPLATES.md"

# type -> (form file, current version)
TEMPLATES = {"bug": ("bug_report.yml", 1), "feature": ("feature_request.yml", 1)}

_ELEMENT_TYPES = {"markdown", "textarea", "input", "dropdown", "checkboxes"}
_MARKER = re.compile(r"^bh-intake/(?P<type>[a-z]+)/v(?P<version>\d+)$")


def _load(name: str) -> dict:
    return YAML(typ="safe").load((FORMS / name).read_text(encoding="utf-8"))


def _fields(form: dict) -> dict[str, dict]:
    return {el["id"]: el for el in form["body"] if "id" in el}


def _doc_rows(kind: str, version: int) -> dict[str, str]:
    """Map field id -> 'Required'/'Optional' from the doc table for one type and version."""
    text = DOC.read_text(encoding="utf-8")
    heading = {"bug": "Bug report", "feature": "Feature request"}[kind]
    match = re.search(
        rf"^## {heading}, version {version}\n(?P<body>.*?)(?=^## |\Z)", text, re.M | re.S
    )
    assert match, f"INTAKE-TEMPLATES.md has no '{heading}, version {version}' section"
    rows = re.findall(
        r"^\| `(?P<id>\w+)` \|[^|]*\| (?P<need>Required|Optional) \|", match["body"], re.M
    )
    return dict(rows)


@pytest.mark.parametrize("kind", sorted(TEMPLATES))
def test_form_is_a_valid_issue_form(kind: str) -> None:
    form = _load(TEMPLATES[kind][0])
    for key in ("name", "description", "body"):
        assert form.get(key), f"{kind}: missing top-level {key!r}"
    assert isinstance(form["body"], list) and form["body"]
    assert any(el["type"] != "markdown" for el in form["body"]), "a form needs an input element"
    ids = []
    for el in form["body"]:
        assert el["type"] in _ELEMENT_TYPES
        if el["type"] == "markdown":
            assert el["attributes"]["value"]
            continue
        assert el["attributes"]["label"]
        ids.append(el["id"])
        if el["type"] == "dropdown":
            options = el["attributes"]["options"]
            assert options and len(options) == len(set(options))
    assert len(ids) == len(set(ids)), f"{kind}: duplicate element ids"


@pytest.mark.parametrize("kind", sorted(TEMPLATES))
def test_form_carries_a_required_version_marker(kind: str) -> None:
    file, version = TEMPLATES[kind]
    marker = _fields(_load(file))["template_version"]
    assert marker["validations"]["required"] is True
    parsed = _MARKER.match(marker["attributes"]["value"])
    assert parsed, "marker must look like bh-intake/<type>/v<N>"
    assert (parsed["type"], int(parsed["version"])) == (kind, version)
    header = (FORMS / file).read_text(encoding="utf-8").splitlines()[0]
    assert header == f"# Template: {kind}  ·  Version: {version}"


@pytest.mark.parametrize("kind", sorted(TEMPLATES))
def test_doc_checklist_matches_the_form(kind: str) -> None:
    file, version = TEMPLATES[kind]
    expected = {
        fid: "Required" if el["validations"]["required"] else "Optional"
        for fid, el in _fields(_load(file)).items()
    }
    assert _doc_rows(kind, version) == expected


def test_config_is_valid_and_keeps_blank_issues_allowed() -> None:
    config = _load("config.yml")
    assert config["blank_issues_enabled"] is True
    for link in config["contact_links"]:
        assert {"name", "url", "about"} <= set(link)
