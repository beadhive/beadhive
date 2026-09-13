"""Current product/documentation claims must not resurrect the retired atomic-hook model.

Spike, proof, release, and upstream records are immutable evidence and deliberately excluded.
The live claim surface is product Python, test descriptions, top-level operator docs, and current
design Markdown.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable, Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

STALE_CLAIMS = {
    "atomic hook-era fence presented as current": re.compile(
        r"(?:real|atomic)\s+`?--force-with-lease`?\s+epoch fence", re.IGNORECASE
    ),
    "atomic data coupling presented as the module contract": re.compile(
        r"epoch fence\s*\+\s*atomic data push", re.IGNORECASE
    ),
    "resolved defect still described as inoperable": re.compile(
        r"epoch fence (?:is )?inoperable", re.IGNORECASE
    ),
    "hook/fence distinction collapsed into a false absolute": re.compile(
        r"fence was never the enforcement", re.IGNORECASE
    ),
    "retired forge fallback presented as production": re.compile(
        r"fence degrades to a documented per-push epoch-bump", re.IGNORECASE
    ),
    "retired receive-pack requirement presented as production": re.compile(
        r"forge-dependent\s+`?--atomic`?\s+receive-pack requirement", re.IGNORECASE
    ),
}


IMMUTABLE_DOC_CATEGORIES = frozenset({"proof", "releases", "spikes", "upstream"})
DATED_DESIGN_RECORD = re.compile(r"(?:^|-)20\d{2}-\d{2}-\d{2}(?:-|\.md$)")
RELEASE_READINESS_RECORD = re.compile(r"^\d+\.\d+\.\d+-release-readiness\.md$")


def _is_immutable_evidence(path: Path) -> bool:
    """Whether a Markdown path is evidence/history rather than a shipped current claim."""
    relative = path.resolve().relative_to(ROOT)
    if relative.parts[:1] != ("docs",):
        return False
    if len(relative.parts) >= 2 and relative.parts[1] in IMMUTABLE_DOC_CATEGORIES:
        return True
    return relative.parent == Path("docs/design") and bool(
        DATED_DESIGN_RECORD.search(relative.name)
        or RELEASE_READINESS_RECORD.fullmatch(relative.name)
    )


def _live_claim_files() -> list[Path]:
    product_and_tests = [
        *(ROOT / "src" / "beadhive").rglob("*.py"),
        *(ROOT / "tests").rglob("*.py"),
    ]
    shipped_claims = [
        *(ROOT / "src" / "beadhive" / "assets").rglob("*.md"),
        *(path for path in (ROOT / "docs").rglob("*.md") if not _is_immutable_evidence(path)),
    ]
    return sorted({*product_and_tests, *shipped_claims} - {Path(__file__).resolve()})


PYTHON_CLAIM_SITES = (
    ("src/beadhive/prepush.py", "install_for_hive"),
    ("tests/test_prepush.py", None),
    ("tests/test_onboard_dag.py", "test_onboard_installs_no_git_hook_at_all"),
    ("tests/test_localloop.py", "test_losing_the_lease_mid_flight_stops_dispatch_and_escalates"),
    ("tests/test_host_fence.py", None),
)

MARKDOWN_CLAIM_SITES = (
    ("docs/design/multi-host-model-adr.md", "### Consequences of Amendment 1"),
    ("docs/BEADS-SYNC.md", "## The epoch fence beside the data (multi-host)"),
)

REQUIRED_CURRENT_TRUTH = {
    "managed ordering": re.compile(r"reserve-before-bd", re.IGNORECASE),
    "postflight identity": re.compile(r"exact\s*-?\s*postflight", re.IGNORECASE),
    "atomicity limitation": re.compile(r"(?:non[- ]|not\s+)atomic", re.IGNORECASE),
    "uninterceptable bypass": re.compile(r"raw\s+(?:OS-level\s+)?bd", re.IGNORECASE),
}


def _stale_claim_violations(
    files: Iterable[Path], reader: Callable[[Path], str] = Path.read_text
) -> list[str]:
    violations: list[str] = []
    for path in files:
        text = reader(path)
        for claim, pattern in STALE_CLAIMS.items():
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                violations.append(f"{path.relative_to(ROOT)}:{line}: {claim}: {match.group(0)!r}")
    return violations


def _python_docstring(path: Path, function: str | None) -> str:
    tree = ast.parse(path.read_text(), filename=str(path))
    if function is None:
        return ast.get_docstring(tree, clean=False) or ""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function:
            return ast.get_docstring(node, clean=False) or ""
    return ""


def _markdown_section(path: Path, heading: str) -> str:
    lines = path.read_text().splitlines()
    try:
        start = lines.index(heading)
    except ValueError:
        return ""
    level = len(heading) - len(heading.lstrip("#"))
    end = len(lines)
    for index in range(start + 1, len(lines)):
        match = re.match(r"^(#+)\s", lines[index])
        if match and len(match.group(1)) <= level:
            end = index
            break
    return "\n".join(lines[start:end])


def _missing_current_truth(text: str) -> list[str]:
    return [claim for claim, pattern in REQUIRED_CURRENT_TRUTH.items() if not pattern.search(text)]


def test_live_epoch_fence_claims_name_the_managed_non_atomic_boundary():
    violations = _stale_claim_violations(_live_claim_files())
    assert not violations, (
        "stale epoch-fence claims found; current truth is reserve-before-bd plus exact "
        "postflight verification, with a non-atomic CAS→push window and raw-bd bypass:\n"
        + "\n".join(violations)
    )


def test_reviewed_epoch_fence_claim_sites_state_the_whole_current_boundary():
    missing: list[str] = []
    for relative, function in PYTHON_CLAIM_SITES:
        site = "module docstring" if function is None else f"{function} docstring"
        for claim in _missing_current_truth(_python_docstring(ROOT / relative, function)):
            missing.append(f"{relative}::{site}: missing {claim}")
    for relative, heading in MARKDOWN_CLAIM_SITES:
        for claim in _missing_current_truth(_markdown_section(ROOT / relative, heading)):
            missing.append(f"{relative}::{heading}: missing {claim}")

    assert not missing, "incomplete epoch-fence boundary claims:\n" + "\n".join(missing)


def test_shipped_operator_asset_is_scanned_and_a_stale_claim_fails():
    asset = ROOT / "src/beadhive/assets/guides/setup/steps/092-rung4-second-host.md"
    assert asset in _live_claim_files()

    original = asset.read_text()
    injected = original + "\nThe atomic --force-with-lease epoch fence protects every push.\n"
    violations = _stale_claim_violations(
        [asset], reader=lambda path: injected if path == asset else path.read_text()
    )

    assert any(str(asset.relative_to(ROOT)) in violation for violation in violations)


def test_immutable_evidence_is_narrowly_excluded_and_untouched():
    immutable = {
        ROOT / "docs/design/0.7.0-release-readiness.md",
        ROOT / "docs/design/gsex3-bug-triage-2026-08-21.md",
        ROOT / "docs/proof/operator-sse-ui-conformance.md",
        ROOT / "docs/releases/official-contracts-v1.0.0.md",
        ROOT / "docs/spikes/bh-ykyi.1-name-registry.md",
        ROOT / "docs/upstream/bv-custom-id-pattern.md",
    }
    before = {path: path.read_bytes() for path in immutable}

    assert all(_is_immutable_evidence(path) for path in immutable)
    assert not _is_immutable_evidence(ROOT / "docs/design/multi-host-model-adr.md")
    assert not _is_immutable_evidence(
        ROOT / "src/beadhive/assets/guides/setup/steps/092-rung4-second-host.md"
    )
    assert immutable.isdisjoint(_live_claim_files())
    _stale_claim_violations(_live_claim_files())

    assert {path: path.read_bytes() for path in immutable} == before


def test_unrelated_truth_cannot_mask_a_stale_named_section(tmp_path):
    document = tmp_path / "claims.md"
    document.write_text(
        "# Current truth elsewhere\n\n"
        "Managed reserve-before-bd plus exact postflight verification has a non-atomic "
        "window and raw bd bypass.\n\n"
        "## Reviewed site\n\n"
        "The atomic --force-with-lease epoch fence protects every push.\n\n"
        "## Next site\n"
    )

    assert not _missing_current_truth(document.read_text())
    reviewed = _markdown_section(document, "## Reviewed site")
    assert set(_missing_current_truth(reviewed)) == set(REQUIRED_CURRENT_TRUTH)
