"""Static release-workflow proofs for PyPI artifact provenance."""

from __future__ import annotations

import re
from pathlib import Path

from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "release.yml"
CONTRIBUTING = (ROOT / "CONTRIBUTING.md").read_text()


def _workflow() -> dict:
    return YAML(typ="safe").load(WORKFLOW_PATH)


def _uses(step: dict, action: str) -> bool:
    return str(step.get("uses", "")).startswith(f"{action}@")


def test_build_has_no_oidc_or_publish_capability() -> None:
    build = _workflow()["jobs"]["build"]

    assert build["permissions"] == {"contents": "read"}
    assert "environment" not in build
    assert not any(_uses(step, "pypa/gh-action-pypi-publish") for step in build["steps"])
    assert all("publish" not in str(step.get("run", "")) for step in build["steps"])


def test_build_checks_and_transfers_exactly_one_wheel_and_sdist() -> None:
    build_steps = _workflow()["jobs"]["build"]["steps"]
    build_commands = "\n".join(str(step.get("run", "")) for step in build_steps)
    transfer = next(step for step in build_steps if _uses(step, "actions/upload-artifact"))

    assert "uv build" in build_commands
    assert "twine check dist/*" in build_commands
    assert 'python -c "import beadhive"' in build_commands
    assert "bh --version" in build_commands
    assert "${#wheels[@]} != 1" in build_commands
    assert "${#sdists[@]} != 1" in build_commands
    assert 'sha256sum "${wheels[@]}" "${sdists[@]}"' in build_commands
    assert transfer["with"] == {
        "name": "python-distributions",
        "path": "dist/*.whl\ndist/*.tar.gz\n",
        "if-no-files-found": "error",
        "retention-days": 1,
    }


def test_publish_receives_only_checked_files_and_does_not_rebuild() -> None:
    publish = _workflow()["jobs"]["publish"]
    receive, upload = publish["steps"]

    assert publish["needs"] == "build"
    assert receive["with"] == {"name": "python-distributions", "path": "dist"}
    assert _uses(receive, "actions/download-artifact")
    assert not any("run" in step for step in publish["steps"])
    assert upload["with"]["packages-dir"] == "dist"


def test_publish_is_pinned_trusted_publishing_with_explicit_attestations() -> None:
    publish = _workflow()["jobs"]["publish"]
    upload = publish["steps"][-1]
    action = upload["uses"]

    assert publish["environment"] == "pypi-prod"
    assert publish["permissions"] == {"id-token": "write"}
    assert re.fullmatch(r"pypa/gh-action-pypi-publish@[0-9a-f]{40}", action)
    assert upload["with"]["attestations"] is True
    assert "password" not in upload["with"]
    assert "user" not in upload["with"]


def test_downstream_release_jobs_wait_for_attested_publish() -> None:
    jobs = _workflow()["jobs"]

    assert jobs["homebrew-tap"]["needs"] == "publish"
    assert jobs["latest"]["needs"] == "publish"


def test_release_docs_distinguish_git_and_distribution_signatures() -> None:
    assert "Signed commits and signed\nrelease tags" in CONTRIBUTING
    assert "PEP 740 attestations authenticate the `.whl` and `.tar.gz` files" in CONTRIBUTING
    assert (
        CONTRIBUTING.count(
            "uvx pypi-attestations verify pypi \\\n"
            "  --repository https://github.com/beadhive/beadhive"
        )
        == 2
    )
    assert "pypi:beadhive-0.16.2-py3-none-any.whl" in CONTRIBUTING
    assert "pypi:beadhive-0.16.2.tar.gz" in CONTRIBUTING
    assert "PyPI's Integrity API" in CONTRIBUTING
