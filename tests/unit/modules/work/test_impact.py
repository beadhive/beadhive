"""Impact resolution core (Attested Green ADR, Amendment 1; bh-1j3ei.3).

The fail-closed rules are exercised through a fake backend so every rule is proven against the
shared post-processor, not against any one build system.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field

import pytest

from beadhive.adapters.impact_git import GitTreeDiff
from beadhive.bootstrap.impact import attest_keys, impact_resolver
from beadhive.modules.config.contracts import AttestConfig
from beadhive.modules.work.application.impact import (
    FailClosedResolver,
    NativeFullResolver,
    select_resolver,
)
from beadhive.modules.work.contracts.impact import ImpactBackend, ImpactResolver, TreeDiffPort
from beadhive.modules.work.domain.impact import (
    NATIVE_FULL,
    AttestKey,
    BackendImpact,
    ChangedPath,
    ImpactReason,
    ImpactReceipt,
    ImpactRequest,
    KeyEvidence,
    ReceiptIntegrityError,
    apply_fail_closed,
    matches_global_input,
)

BASE, HEAD = "base-tree", "head-tree"
LINT = AttestKey("lint", "just lint", selectors={"fake": "attest:lint"})
UNIT = AttestKey("unit", "just test", selectors={"fake": "attest:unit"})
DOCS = AttestKey("docs", "just lint-md", policy="optional", selectors={"fake": "attest:docs"})
GITMETA = AttestKey("release-pin", "pytest -m always_run")  # no selector: rule 5 via rule 4
KEYS = (LINT, UNIT, DOCS, GITMETA)


class FakeTrees:
    def __init__(self, changed=()):
        self.changed = tuple(changed)

    def tree_of(self, repo, rev):
        return {"b": BASE, "h": HEAD}.get(rev, rev)

    def changed_paths(self, repo, base_tree, head_tree):
        return () if base_tree == head_tree else self.changed


@dataclass
class FakeBackend:
    name: str = "fake"
    version: str = "1.0"
    answer: BackendImpact | None = None
    error: Exception | None = None
    calls: list = field(default_factory=list)

    def analyze(self, request: ImpactRequest) -> BackendImpact:
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        return self.answer


def graph(**overrides) -> BackendImpact:
    """A well-behaved answer: src/a.py owned by //src:a, which only the lint key selects."""
    base = {
        "backend_version": "1.0",
        "owners": {"src/a.py": ("//src:a",), "docs/x.md": ("//docs:x",)},
        "affected_units": frozenset({"//src:a"}),
        "key_units": {"lint": ("//src:a",), "unit": ("//tests:unit",), "docs": ("//docs:x",)},
        "proven_keys": frozenset({"lint", "unit", "docs"}),
        "global_inputs": ("pants.toml", "BUILD", "3rdparty/**"),
    }
    base.update(overrides)
    return BackendImpact(**base)


def resolve(changed, answer=None, error=None, clock=None, keys=KEYS):
    backend = FakeBackend(answer=answer if answer is not None else graph(), error=error)
    kwargs = {"clock": clock} if clock else {}
    resolver = FailClosedResolver(backend, FakeTrees(changed), timeout_seconds=10, **kwargs)
    return resolver.resolve("/repo", "b", "h", keys), backend


def all_invalidated(receipt: ImpactReceipt) -> bool:
    return set(receipt.invalidated_keys) == {k.name for k in KEYS} and not receipt.unaffected_keys


# --- the ports ------------------------------------------------------------------------------


def test_ports_are_runtime_checkable():
    assert isinstance(FakeBackend(), ImpactBackend)
    assert isinstance(FakeTrees(), TreeDiffPort)
    assert isinstance(GitTreeDiff(), TreeDiffPort)
    assert isinstance(NativeFullResolver(FakeTrees()), ImpactResolver)
    assert isinstance(FailClosedResolver(FakeBackend(), FakeTrees()), ImpactResolver)


# --- the happy path: only a proven, unaffected key is unaffected ----------------------------


def test_graph_answer_invalidates_only_affected_and_unproven_keys():
    receipt, backend = resolve([ChangedPath("src/a.py")])
    assert receipt.fallback_reason == ""
    assert receipt.backend == "fake" and receipt.backend_version == "1.0"
    assert receipt.unaffected_keys == ("docs", "unit")
    assert receipt.invalidated_keys == ("lint", "release-pin")
    assert receipt.evidence["lint"] == KeyEvidence(ImpactReason.AFFECTED, ("//src:a",))
    assert receipt.evidence["unit"] == KeyEvidence(ImpactReason.UNAFFECTED, ("//tests:unit",))
    assert receipt.evidence["release-pin"].reason == ImpactReason.NO_SELECTOR
    assert receipt.is_unaffected("unit") and not receipt.is_unaffected("lint")
    assert backend.calls[0].changed == (ChangedPath("src/a.py"),)


def test_owner_counts_as_affected_even_if_backend_omits_it_from_dependents():
    receipt, _ = resolve([ChangedPath("docs/x.md")], graph(affected_units=frozenset()))
    assert "docs" in receipt.invalidated_keys
    assert receipt.evidence["docs"] == KeyEvidence(ImpactReason.AFFECTED, ("//docs:x",))


# --- rules 1-4: each invalidates every key --------------------------------------------------


def test_rule1_unowned_path_invalidates_every_key():
    receipt, _ = resolve([ChangedPath("src/a.py"), ChangedPath("orphan.txt", "A")])
    assert all_invalidated(receipt)
    assert receipt.unowned_paths == ("orphan.txt",)
    assert {e.reason for e in receipt.evidence.values()} == {ImpactReason.UNOWNED_PATH}


def test_rule1_owner_mapped_to_nothing_is_unowned():
    answer = graph(owners={"src/a.py": ()})
    receipt, _ = resolve([ChangedPath("src/a.py")], answer)
    assert all_invalidated(receipt) and receipt.unowned_paths == ("src/a.py",)


def test_rule1_deleted_path_the_backend_cannot_attribute_invalidates_every_key():
    receipt, backend = resolve([ChangedPath("src/gone.py", "D")])
    assert backend.calls[0].changed[0].deleted
    assert all_invalidated(receipt) and receipt.unowned_paths == ("src/gone.py",)


def test_rule1_deleted_path_attributed_through_base_is_owned():
    answer = graph(owners={"src/gone.py": ("//src:a",)})
    receipt, _ = resolve([ChangedPath("src/gone.py", "D")], answer)
    assert receipt.unowned_paths == () and "unit" in receipt.unaffected_keys


@pytest.mark.parametrize("path", ["pants.toml", "src/BUILD", "3rdparty/python/x.lock"])
def test_rule2_global_input_invalidates_every_key(path):
    answer = graph(owners={"src/a.py": ("//src:a",), path: ("//:root",)})
    receipt, _ = resolve([ChangedPath("src/a.py"), ChangedPath(path)], answer)
    assert all_invalidated(receipt)
    assert receipt.global_inputs_hit == (path,)
    assert {e.reason for e in receipt.evidence.values()} == {ImpactReason.GLOBAL_INPUT}


def test_rule3_backend_error_falls_back_to_native_full():
    receipt, _ = resolve([ChangedPath("src/a.py")], error=RuntimeError("UnownedDependencyError"))
    assert all_invalidated(receipt)
    assert receipt.backend == NATIVE_FULL
    assert receipt.fallback_reason.startswith("fake: error: RuntimeError")
    assert not receipt.is_unaffected("unit")


def test_rule3_timeout_discards_the_answer():
    ticks = iter([0.0, 11.0, 11.0, 11.0])
    receipt, _ = resolve([ChangedPath("src/a.py")], clock=lambda: next(ticks))
    assert all_invalidated(receipt)
    assert receipt.fallback_reason.startswith("fake: timeout")


def test_rule3_backend_version_mismatch_falls_back():
    receipt, _ = resolve([ChangedPath("src/a.py")], graph(backend_version="2.0"))
    assert all_invalidated(receipt)
    assert "backend-version-mismatch" in receipt.fallback_reason


@pytest.mark.parametrize(
    "answer",
    [
        graph(proven_keys=frozenset({"lint", "docs"})),  # unit not proven
        graph(key_units={"lint": ("//src:a",), "docs": ("//docs:x",)}),  # unit selects nothing
    ],
)
def test_rule4_unproven_target_invalidates_its_key_on_any_change(answer):
    receipt, _ = resolve([ChangedPath("src/a.py")], answer)
    assert "unit" in receipt.invalidated_keys
    assert receipt.evidence["unit"].reason == ImpactReason.UNPROVEN


def test_rule4_every_key_unproven_invalidates_every_key():
    receipt, _ = resolve([ChangedPath("src/a.py")], graph(proven_keys=frozenset()))
    assert all_invalidated(receipt)


def test_rule5_key_without_selector_never_unaffected_on_change():
    receipt, _ = resolve([ChangedPath("docs/x.md")])
    assert receipt.evidence["release-pin"].reason == ImpactReason.NO_SELECTOR
    assert "release-pin" in receipt.invalidated_keys


def test_identical_trees_need_no_backend_and_invalidate_nothing():
    receipt, backend = resolve([])
    assert backend.calls == []
    assert receipt.invalidated_keys == () and len(receipt.unaffected_keys) == len(KEYS)


def test_duplicate_key_names_are_refused():
    with pytest.raises(ValueError, match="duplicate"):
        resolve([ChangedPath("src/a.py")], keys=(LINT, LINT))


def test_apply_fail_closed_is_the_only_path_and_checks_version_itself():
    request = ImpactRequest("/r", BASE, HEAD, (ChangedPath("src/a.py"),), KEYS, 10)
    receipt = apply_fail_closed(
        backend="fake", backend_version="1.0", request=request, raw=graph(backend_version="0.9")
    )
    assert receipt.is_fallback and all_invalidated(receipt)


def test_global_input_patterns_only_add_invalidation():
    assert matches_global_input("a/b/BUILD", ["BUILD"])
    assert matches_global_input("uv.lock", ["uv.lock"])
    assert not matches_global_input("src/a.py", ["BUILD", "*.lock"])


# --- native-full: the default ---------------------------------------------------------------


def test_native_full_invalidates_every_key_on_any_change():
    receipt = NativeFullResolver(FakeTrees([ChangedPath("README.md")])).resolve(
        "/r", "b", "h", KEYS
    )
    assert receipt.backend == NATIVE_FULL and receipt.fallback_reason == ""
    assert all_invalidated(receipt)
    assert {e.reason for e in receipt.evidence.values()} == {ImpactReason.NATIVE_FULL}


def test_select_resolver_defaults_and_degrades():
    trees = FakeTrees([ChangedPath("src/a.py")])
    assert isinstance(select_resolver(NATIVE_FULL, tree_diff=trees), NativeFullResolver)
    missing = select_resolver("pants", tree_diff=trees).resolve("/r", "b", "h", KEYS)
    assert missing.fallback_reason == "pants: backend not available" and all_invalidated(missing)
    wrong = select_resolver("fake", tree_diff=trees, backends={"fake": object()})
    assert "does not implement" in wrong.resolve("/r", "b", "h", KEYS).fallback_reason
    bound = select_resolver("fake", tree_diff=trees, backends={"fake": FakeBackend()})
    assert isinstance(bound, FailClosedResolver)


def test_bootstrap_defaults_to_native_full_and_maps_typed_config():
    assert isinstance(impact_resolver(tree_diff=FakeTrees()), NativeFullResolver)
    configured = AttestConfig.model_validate(
        {
            "keys": [
                {
                    "name": "unit",
                    "cmd": "just test",
                    "policy": "optional",
                    "selectors": {"fake": "attest:unit"},
                }
            ],
            "impact": {"backend": "fake", "timeout_seconds": 12},
        }
    )
    keys = attest_keys(configured)
    assert keys == (
        AttestKey("unit", "just test", policy="optional", selectors={"fake": "attest:unit"}),
    )
    assert isinstance(
        impact_resolver(configured, backends={"fake": FakeBackend()}, tree_diff=FakeTrees()),
        FailClosedResolver,
    )


def test_bootstrap_maps_disabled_execution_state_orthogonally_to_policy():
    configured = AttestConfig.model_validate(
        {
            "keys": [
                {
                    "name": "unit",
                    "cmd": "just test",
                    "policy": "optional",
                    "enabled": False,
                    "disabled_reason": "bounded maintenance",
                    "disabled_until": "2999-01-01T00:00:00Z",
                }
            ]
        }
    )

    [key] = attest_keys(configured)
    assert key.policy == "optional"
    assert key.is_disabled()
    assert key.disabled_reason == "bounded maintenance"


@pytest.mark.parametrize(
    "raw,match",
    [
        ({"enabled": False}, "disabled_reason"),
        ({"enabled": False, "disabled_reason": "   "}, "disabled_reason"),
        (
            {
                "enabled": False,
                "disabled_reason": "maintenance",
                "disabled_until": "2999-01-01T00:00:00",
            },
            "timezone",
        ),
    ],
)
def test_disabled_config_requires_reason_and_timezone(raw, match):
    with pytest.raises(ValueError, match=match):
        AttestConfig.model_validate({"keys": [{"name": "unit", "cmd": "just test", **raw}]})


@pytest.mark.parametrize(
    "raw",
    [
        {"unknown": True},
        {"keys": [{"name": "unit", "cmd": "just test", "unknown": True}]},
        {"impact": {"backend": "native-full", "unknown": True}},
    ],
)
def test_attest_config_forbids_unknown_fields_at_every_level(raw):
    with pytest.raises(ValueError, match="extra_forbidden"):
        AttestConfig.model_validate(raw)


# --- receipts -------------------------------------------------------------------------------


def test_receipt_round_trips_through_json_and_verifies_its_digest():
    receipt, _ = resolve([ChangedPath("src/a.py")])
    text = receipt.to_json()
    data = json.loads(text)
    assert data["digest"] == receipt.digest and data["digest"].startswith("sha256:")
    again = ImpactReceipt.from_json(text)
    assert again == receipt and again.digest == receipt.digest

    data["unaffected_keys"] = ["docs", "lint", "unit"]
    data["invalidated_keys"] = ["release-pin"]
    with pytest.raises(ReceiptIntegrityError):
        ImpactReceipt.from_dict(data)


def test_receipt_digest_is_stable_across_runs_and_ignores_elapsed():
    first, _ = resolve([ChangedPath("src/a.py")])
    shuffled = tuple(reversed(KEYS))
    ticks = iter([0.0, 3.0, 3.0, 3.0, 3.0])
    second, _ = resolve([ChangedPath("src/a.py")], clock=lambda: next(ticks), keys=shuffled)
    assert second.elapsed_ms != first.elapsed_ms
    assert second.digest == first.digest


def test_receipt_digest_is_pinned():
    """A change to this value is a receipt-format change: bump RECEIPT_SCHEMA instead."""
    receipt = ImpactReceipt(
        backend=NATIVE_FULL,
        backend_version="1",
        base_tree="a" * 40,
        head_tree="b" * 40,
        changed_paths=("README.md",),
        unowned_paths=(),
        global_inputs_hit=(),
        invalidated_keys=("lint",),
        unaffected_keys=(),
        evidence={"lint": KeyEvidence(ImpactReason.NATIVE_FULL)},
    )
    canonical = json.dumps(receipt.to_dict() | {"digest": None, "elapsed_ms": None})
    assert "elapsed_ms" in canonical
    assert receipt.digest == (
        "sha256:" + __import__("hashlib").sha256(_canonical(receipt).encode()).hexdigest()
    )


def _canonical(receipt: ImpactReceipt) -> str:
    content = {k: v for k, v in receipt.to_dict().items() if k not in ("digest", "elapsed_ms")}
    return json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def test_receipt_rejects_contradiction_and_unknown_schema():
    with pytest.raises(ValueError, match="both"):
        ImpactReceipt(NATIVE_FULL, "1", BASE, HEAD, (), (), (), ("a",), ("a",), {})
    with pytest.raises(ValueError, match="schema"):
        ImpactReceipt.from_dict({"schema": "other/v9"})


def test_attest_key_validation():
    with pytest.raises(ValueError):
        AttestKey("", "x")
    with pytest.raises(ValueError):
        AttestKey("k", "")
    with pytest.raises(ValueError):
        AttestKey("k", "x", policy="sometimes")  # type: ignore[arg-type]
    assert AttestKey("k", "x").selector("pants") is None


# --- the git adapter ------------------------------------------------------------------------


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.mark.usefixtures("runtime_test_scope")
def test_git_tree_diff_lists_every_change_with_renames_split(tmp_path):
    repo = tmp_path
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "keep.py").write_text("a\n")
    (repo / "old.py").write_text("move me\n" * 20)
    (repo / "gone.md").write_text("bye\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    (repo / "keep.py").write_text("b\n")
    (repo / "old.py").rename(repo / "new.py")
    (repo / "gone.md").unlink()
    (repo / "dir with space").mkdir()
    (repo / "dir with space" / "added.txt").write_text("x\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "head")

    diff = GitTreeDiff()
    base = diff.tree_of(str(repo), "HEAD~1")
    head = diff.tree_of(str(repo), "HEAD")
    assert base == _git(repo, "rev-parse", "HEAD~1^{tree}")
    assert diff.changed_paths(str(repo), base, base) == ()
    assert diff.changed_paths(str(repo), base, head) == (
        ChangedPath("dir with space/added.txt", "A"),
        ChangedPath("gone.md", "D"),
        ChangedPath("keep.py", "M"),
        ChangedPath("new.py", "A"),
        ChangedPath("old.py", "D"),
    )
    with pytest.raises(RuntimeError):
        diff.tree_of(str(repo), "no-such-rev")
