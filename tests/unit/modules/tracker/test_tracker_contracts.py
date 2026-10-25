"""Static and value properties of the bead tracker adapter port (tracker-adapter-port-adr.md 3).

The contract is standard-library-only, carries no tracker-product vocabulary in any field or
Protocol parameter, requires a keyword-only grant on every write, keeps the optional Protocols
out of ``TrackerAdapter``, and nothing under ``src/`` imports a spike prototype.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import sys
import warnings
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import pytest

from beadhive.modules.tracker import contracts
from beadhive.modules.tracker.contracts import (
    PORT_API_VERSION,
    READ_OPERATIONS,
    WRITE_OPERATIONS,
    CapabilityState,
    ChangeRequestUrl,
    CommentWriter,
    IssueKey,
    IssueResolver,
    LabelNotOwned,
    LabelOwnership,
    LabelWriter,
    Operation,
    OperationNotEnabled,
    OtherProject,
    RateLimited,
    Revision,
    TrackerAdapter,
    TrackerError,
    TrackerIdentity,
    TrackerReader,
    TransientFailure,
    Tri,
    UrlNotRecognized,
    WriteOutcomeUnknown,
)

_SRC = Path(__file__).resolve().parents[4] / "src"
_MODULE_ROOT = _SRC / "beadhive" / "modules" / "tracker"
#: Module names of the spike bh-gj1hb prototypes (docs/spikes/artifacts/bh-gj1hb/*.py).
_SPIKE_PROTOTYPES = {
    "conftest",
    "fake_github_rest",
    "github_adapter",
    "memory_tracker",
    "mutation_controls",
    "probe_boundary_rules",
    "test_contract_shape",
    "test_port_conformance",
    "tracker_contracts",
}

#: Words that would mean a tracker product's concept leaked into the neutral contract.
TRACKER_PRODUCT_VOCABULARY = {
    "number",
    "node_id",
    "html_url",
    "login",
    "pull_request",
    "repository",
    "owner",
    "repo",
    "github",
    "gh",
    "scopes",
    "oauth",
    "app_id",
    "installation",
    "link",
    "etag",
}


def _imports(path: Path) -> set[str]:
    with warnings.catch_warnings():  # a product file's escape warnings are not ours to report
        warnings.simplefilter("ignore", SyntaxWarning)
        warnings.simplefilter("ignore", DeprecationWarning)
        tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def test_port_api_version_is_one() -> None:
    assert PORT_API_VERSION == 1


def test_tracker_module_is_standard_library_only() -> None:
    files = sorted(_MODULE_ROOT.rglob("*.py"))
    assert len(files) >= 7
    for path in files:
        imported = _imports(path)
        assert imported <= set(sys.stdlib_module_names), (path.name, imported)


def _vocabulary_offenders(namespace: dict[str, object]) -> list[str]:
    offenders = []
    for name, obj in namespace.items():
        if dataclasses.is_dataclass(obj):
            for item in dataclasses.fields(obj):
                words = set(item.name.split("_"))
                if words & TRACKER_PRODUCT_VOCABULARY or item.name in TRACKER_PRODUCT_VOCABULARY:
                    offenders.append(f"{name}.{item.name}")
        if inspect.isclass(obj) and getattr(obj, "_is_protocol", False):
            for method_name, method in vars(obj).items():
                if callable(method) and not method_name.startswith("_"):
                    for param in inspect.signature(method).parameters:
                        if param in TRACKER_PRODUCT_VOCABULARY:
                            offenders.append(f"{name}.{method_name}({param})")
    return offenders


def test_contract_has_no_tracker_product_field_or_parameter() -> None:
    public = {name: getattr(contracts, name) for name in contracts.__all__}
    assert _vocabulary_offenders(public) == []


def test_vocabulary_guard_is_not_vacuous() -> None:
    """Mutation control: a product-shaped field or parameter is caught."""

    @dataclasses.dataclass(frozen=True)
    class Leaky:
        html_url: str

    class LeakyReader(Protocol):
        def list_issues(self, repo: str) -> None: ...

    assert _vocabulary_offenders({"Leaky": Leaky, "LeakyReader": LeakyReader}) == [
        "Leaky.html_url",
        "LeakyReader.list_issues(repo)",
    ]


def test_public_surface_is_complete_and_resolvable() -> None:
    assert len(contracts.__all__) == len(set(contracts.__all__))
    for name in contracts.__all__:
        assert getattr(contracts, name) is not None
    for module in ("records", "errors", "ports"):
        submodule = sys.modules[f"beadhive.modules.tracker.contracts.{module}"]
        assert set(submodule.__all__) <= set(contracts.__all__), module


def test_writes_require_a_keyword_only_grant_and_reads_do_not() -> None:
    for protocol in (CommentWriter, LabelWriter):
        for method_name, method in vars(protocol).items():
            if callable(method) and not method_name.startswith("_"):
                param = inspect.signature(method).parameters["grant"]
                assert param.kind is inspect.Parameter.KEYWORD_ONLY
    for protocol in (TrackerReader, TrackerIdentity, IssueResolver):
        for method_name, method in vars(protocol).items():
            if callable(method) and not method_name.startswith("_"):
                assert "grant" not in inspect.signature(method).parameters


class _ReaderOnly:
    def describe(self): ...

    def list_issues(self, query): ...

    def list_comments(self, query): ...

    def list_ids(self, query): ...

    def whoami(self): ...

    def probe_capabilities(self): ...


class _Resolving(_ReaderOnly):
    def resolve(self, url): ...

    def get(self, key): ...


def test_optional_protocols_are_detected_by_isinstance_only() -> None:
    plain, resolving = _ReaderOnly(), _Resolving()

    assert isinstance(plain, TrackerAdapter) and isinstance(resolving, TrackerAdapter)
    assert not isinstance(plain, IssueResolver) and isinstance(resolving, IssueResolver)
    assert not isinstance(plain, CommentWriter) and not isinstance(plain, LabelWriter)
    assert "resolve" not in dir(TrackerAdapter)  # A1 never widens the required port


def test_error_taxonomy() -> None:
    for error in (UrlNotRecognized, OtherProject, ChangeRequestUrl, WriteOutcomeUnknown):
        assert issubclass(error, TrackerError) and not error.retryable
    assert issubclass(LabelNotOwned, OperationNotEnabled)
    assert TransientFailure.retryable
    limited = RateLimited("slow down", retry_after=None, kind="quota")
    assert limited.retryable and limited.kind == "quota" and str(limited) == "slow down"


def test_operation_sets_partition_the_operations() -> None:
    assert READ_OPERATIONS | WRITE_OPERATIONS == frozenset(Operation)
    assert not READ_OPERATIONS & WRITE_OPERATIONS


@pytest.mark.parametrize(
    ("supported", "enabled", "probe", "usable"),
    [
        (True, True, Tri.YES, True),
        (True, True, Tri.UNKNOWN, True),
        (True, True, Tri.NO, False),
        (True, False, Tri.YES, False),
        (False, True, Tri.YES, False),
    ],
)
def test_capability_state_usable(supported: bool, enabled: bool, probe: Tri, usable: bool) -> None:
    state = CapabilityState(Operation.COMMENT_CREATE, supported, enabled, probe)
    assert state.usable is usable


def test_issue_key_equality_ignores_optional_ids_and_revision_orders_by_time() -> None:
    assert IssueKey("#1", "u", internal_id="1") == IssueKey("#1", "u", global_id="g")
    assert IssueKey("#1", "u") != IssueKey("#1", "v")
    early = Revision(datetime(2026, 1, 1, tzinfo=UTC), version="b")
    late = Revision(datetime(2026, 1, 2, tzinfo=UTC), version="a")
    assert early < late and Revision(early.updated_at, version="z") == early


def test_label_ownership_is_by_prefix() -> None:
    owned = LabelOwnership(prefixes=("bh:", "triage/"))
    assert owned.owns("bh:accepted") and owned.owns("triage/new")
    assert not owned.owns("bug") and not LabelOwnership(prefixes=()).owns("bh:x")


def test_tracker_packages_import_no_spike_prototype() -> None:
    """The bh-gj1hb prototypes were ported, never imported."""
    roots = (_MODULE_ROOT, _SRC / "beadhive" / "testing" / "tracker")
    for path in sorted(p for root in roots for p in root.rglob("*.py")):
        assert not _imports(path) & _SPIKE_PROTOTYPES, path
