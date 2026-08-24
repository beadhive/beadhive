"""scripts/image-release-drift.sh — the guard on image-vs-RELEASE skew (bh-pee6m).

`just image-drift` already guards core-vs-agent skew and, deliberately, does not fail on
"behind HEAD" mid-session. This is the OTHER skew: a published `:dev` tag, already pulled by
consumers (beadhive-app, briancripe/qm), sat at bh 0.7.1 against a released 0.11.3 with nothing
looking. That case wants the opposite verdict — FAIL.

Building an image needs a docker daemon (per tests/test_image_build.py's convention), so this
covers the DECISION LOGIC with `docker` stubbed on PATH — runs anywhere, including CI with no
daemon. `release-pin.sh` reads a real pyproject.toml, so each test gets its own fixture
checkout (a copied scripts/ dir + a fake pyproject.toml) rather than the real repo's version,
so the "checked-out version" is pinned by the test, not by whatever this tree happens to be at.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "image-release-drift.sh"
RELEASE_PIN = REPO_ROOT / "scripts" / "release-pin.sh"


def make_checkout(tmp_path, version):
    """A fixture checkout: fake pyproject.toml + real scripts/, so release-pin.sh (which
    image-release-drift.sh calls as a sibling script) reads a version the test controls."""
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    shutil.copy(SCRIPT, scripts_dir / "image-release-drift.sh")
    shutil.copy(RELEASE_PIN, scripts_dir / "release-pin.sh")
    os.chmod(scripts_dir / "image-release-drift.sh", 0o755)
    os.chmod(scripts_dir / "release-pin.sh", 0o755)
    (tmp_path / "pyproject.toml").write_text(f'[project]\nversion = "{version}"\n')
    return scripts_dir / "image-release-drift.sh"


def stub_docker(tmp_path, *, present=True, bh_version="0.11.3", source="pypi:beadhive[otel]"):
    """Stub `docker image inspect` (present/absent) and the manifest-reading `docker run`."""
    stub = tmp_path / "docker"
    if not present:
        body = '#!/usr/bin/env bash\n[ "$1" = "image" ] && exit 1\nexit 1\n'
    else:
        body = f"""#!/usr/bin/env bash
if [ "$1" = "image" ]; then exit 0; fi
if [ "$1" = "run" ]; then printf '%s\\t%s\\n' "{bh_version}" "{source}"; exit 0; fi
exit 1
"""
    stub.write_text(body)
    stub.chmod(0o755)
    return stub


def run_script(script, tmp_path, image="beadhive/core:dev"):
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    return subprocess.run([str(script), image], capture_output=True, text=True, env=env)


def test_image_behind_release_fails(tmp_path):
    """THE bug: a :dev image at 0.7.1 against a checkout released as 0.11.3."""
    script = make_checkout(tmp_path, "0.11.3")
    stub_docker(tmp_path, bh_version="0.7.1")
    result = run_script(script, tmp_path)
    assert result.returncode == 1
    assert "0.7.1" in result.stderr
    assert "0.11.3" in result.stderr
    assert "behind" in result.stderr


def test_version_ordering_is_numeric_not_lexical(tmp_path):
    """ "0.7.1" > "0.11.3" as strings (7 > 1) — must not fool the comparison."""
    script = make_checkout(tmp_path, "0.11.3")
    stub_docker(tmp_path, bh_version="0.9.0")
    result = run_script(script, tmp_path)
    assert result.returncode == 1, "0.9.0 is behind 0.11.3 numerically; lexical is backwards"


def test_image_matching_release_passes(tmp_path):
    script = make_checkout(tmp_path, "0.11.3")
    stub_docker(tmp_path, bh_version="0.11.3")
    result = run_script(script, tmp_path)
    assert result.returncode == 0
    assert "✓" in result.stdout


def test_image_ahead_of_checkout_passes(tmp_path):
    """A checkout mid-cycle behind an image someone already rebuilt is not the failure mode."""
    script = make_checkout(tmp_path, "0.11.0")
    stub_docker(tmp_path, bh_version="0.11.3")
    result = run_script(script, tmp_path)
    assert result.returncode == 0


def test_local_wheel_source_is_not_this_checks_job(tmp_path):
    """A local-wheel build is image-drift's job (vs git SHA), not a release comparison."""
    script = make_checkout(tmp_path, "0.11.3")
    stub_docker(tmp_path, bh_version="0.7.1", source="local-wheel:beadhive-0.7.1-py3-none-any.whl")
    result = run_script(script, tmp_path)
    assert result.returncode == 0
    assert "not this check" in result.stdout


def test_no_local_image_is_not_a_failure(tmp_path):
    script = make_checkout(tmp_path, "0.11.3")
    stub_docker(tmp_path, present=False)
    result = run_script(script, tmp_path)
    assert result.returncode == 2
    assert "nothing to compare" in result.stdout
