#!/usr/bin/env python3
"""Trusted read-only verification boundary for production shadow evidence.

The shadow policy remains a pure evaluator. This module alone consults repository and local
receipt authorities and returns the production route without exporting a capability.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

# Bare sibling import for `python scripts/test_closure_shadow_verifier.py` (scripts/ on
# sys.path, not repo root); Pants can't infer it since the module lives under the `scripts.`
# namespace. The real edge is declared explicitly in scripts/BUILD.
import test_closure_shadow_policy as shadow  # pants: no-infer-dep

TRUSTED_GIT_EXECUTABLE = Path("/usr/bin/git")
TRUSTED_COMMAND_PATH = "/usr/bin:/bin"

ROUTE_BINDING_FIELDS = frozenset(
    {
        "schema_version",
        "checkout_ref",
        "checkout_head",
        "plan_digest",
        "base",
        "head",
        "merge_base",
        "tree",
        "closure_id",
        "closure_input_digest",
        "source_revision",
        "candidate_decision_digest",
        "authoritative_evidence_digest",
        "evidence_digest",
    }
)


@dataclass(frozen=True)
class _CheckoutSnapshot:
    """One internally derived symbolic checkout identity."""

    ref: str
    head: str
    tree: str


class _GitEvidencePort(Protocol):
    """Read-only repository facts trusted by the production verifier."""

    def checkout_snapshot(self) -> _CheckoutSnapshot | None: ...

    def commit_tree(self, commit: str) -> str | None: ...

    def is_ancestor(self, commit: str, descendant: str) -> bool: ...

    def merge_base(self, left: str, right: str) -> str | None: ...


class _ReceiptPort(Protocol):
    """Read-only completed validation receipts trusted by the production verifier."""

    def load(self, receipt_id: str) -> Mapping[str, Any] | None: ...


class _SubprocessGitEvidence:
    """Read-only Git adapter anchored to the live symbolic checkout."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        command = (
            str(TRUSTED_GIT_EXECUTABLE),
            "--no-optional-locks",
            "--no-replace-objects",
            "-c",
            "core.useReplaceRefs=false",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
            "-C",
            str(self.root),
            *args,
        )
        env = {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
            "PATH": TRUSTED_COMMAND_PATH,
        }
        try:
            metadata = TRUSTED_GIT_EXECUTABLE.stat(follow_symlinks=False)
            if not stat.S_ISREG(metadata.st_mode) or not os.access(TRUSTED_GIT_EXECUTABLE, os.X_OK):
                raise OSError("trusted Git executable is not an executable regular file")
            return subprocess.run(
                command,
                check=False,
                capture_output=True,
                env=env,
                text=True,
            )
        except OSError as error:
            return subprocess.CompletedProcess(command, 127, "", str(error))

    @staticmethod
    def _influence_file_is_absent_or_empty(path: Path) -> bool:
        """Accept only an absent or empty regular repository influence file."""
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return stat.S_ISREG(metadata.st_mode) and metadata.st_size == 0

    def _object_store_is_trusted(self) -> bool:
        common_dir = self.common_dir()
        if common_dir is None:
            return False
        return all(
            self._influence_file_is_absent_or_empty(common_dir / relative)
            for relative in (Path("info/grafts"), Path("objects/info/alternates"))
        )

    def checkout_snapshot(self) -> _CheckoutSnapshot | None:
        if not self._object_store_is_trusted():
            return None
        ref_result = self._run("symbolic-ref", "--quiet", "HEAD")
        ref = ref_result.stdout.strip()
        if ref_result.returncode != 0 or not ref.startswith("refs/heads/"):
            return None
        head_result = self._run("rev-parse", "--verify", f"{ref}^{{commit}}")
        head = head_result.stdout.strip()
        if head_result.returncode != 0 or not shadow._is_sha(head):
            return None
        tree = self.commit_tree(head)
        if not shadow._is_sha(tree):
            return None
        status = self._run("status", "--porcelain=v1", "--untracked-files=all")
        if status.returncode != 0 or status.stdout:
            return None
        confirmed_ref = self._run("symbolic-ref", "--quiet", "HEAD")
        confirmed_head = self._run("rev-parse", "--verify", f"{ref}^{{commit}}")
        if (
            confirmed_ref.returncode != 0
            or confirmed_ref.stdout.strip() != ref
            or confirmed_head.returncode != 0
            or confirmed_head.stdout.strip() != head
            or not self._object_store_is_trusted()
        ):
            return None
        return _CheckoutSnapshot(ref=ref, head=head, tree=tree)

    def commit_tree(self, commit: str) -> str | None:
        verified = self._run("rev-parse", "--verify", f"{commit}^{{commit}}")
        if verified.returncode != 0 or verified.stdout.strip() != commit:
            return None
        tree = self._run("rev-parse", "--verify", f"{commit}^{{tree}}")
        return tree.stdout.strip() if tree.returncode == 0 else None

    def is_ancestor(self, commit: str, descendant: str) -> bool:
        completed = self._run("merge-base", "--is-ancestor", commit, descendant)
        if completed.returncode not in {0, 1}:
            raise RuntimeError(completed.stderr.strip() or "Git ancestry verification failed")
        return completed.returncode == 0

    def merge_base(self, left: str, right: str) -> str | None:
        completed = self._run("merge-base", left, right)
        return completed.stdout.strip() if completed.returncode == 0 else None

    def common_dir(self) -> Path | None:
        completed = self._run("rev-parse", "--git-common-dir")
        if completed.returncode != 0 or not completed.stdout.strip():
            return None
        path = Path(completed.stdout.strip())
        return path if path.is_absolute() else self.root / path


class _LocalReceiptDirectory:
    """Read exact JSON receipts from one preconfigured local directory."""

    def __init__(self, root: Path) -> None:
        self._directory_fd = self._open_directory_chain(root)

    @staticmethod
    def _open_directory_chain(root: Path) -> int:
        """Open an absolute directory one no-symlink component at a time."""
        if not root.is_absolute():
            raise OSError("receipt directory must be absolute")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
        descriptor = os.open(os.sep, flags)
        try:
            for component in root.parts[1:]:
                if component in {"", ".", ".."}:
                    raise OSError("invalid receipt directory component")
                next_descriptor = os.open(component, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = next_descriptor
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    def __del__(self) -> None:
        directory_fd = getattr(self, "_directory_fd", None)
        if directory_fd is not None:
            try:
                os.close(directory_fd)
            except OSError:
                pass
            self._directory_fd = None

    @staticmethod
    def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate receipt key: {key}")
            value[key] = item
        return value

    def load(self, receipt_id: str) -> Mapping[str, Any] | None:
        if not receipt_id or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
            for character in receipt_id
        ):
            return None
        descriptor: int | None = None
        try:
            descriptor = os.open(
                f"{receipt_id}.json",
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=self._directory_fd,
            )
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 1_000_000:
                return None
            with os.fdopen(descriptor, encoding="utf-8") as receipt_file:
                descriptor = None
                value = json.load(
                    receipt_file,
                    object_pairs_hook=self._object_without_duplicate_keys,
                )
        except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError, ValueError):
            return None
        finally:
            if descriptor is not None:
                os.close(descriptor)
        return value if isinstance(value, dict) else None


@dataclass(frozen=True)
class _VerificationResult:
    """Fail-closed verifier result; attestation is absent whenever any fact is unproved."""

    route_binding: Mapping[str, Any] | None
    errors: tuple[str, ...]


def _candidate_row(plan: Mapping[str, Any]) -> Mapping[str, Any] | None:
    rows = plan.get("candidate_closures")
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], Mapping):
        return None
    return rows[0]


def _verify_production_evidence(
    *,
    closure: Mapping[str, Any],
    samples: Sequence[Mapping[str, Any]],
    authoritative_evidence: Sequence[Mapping[str, Any]],
    candidate: Mapping[str, Any],
    current_plan: Mapping[str, Any],
    git: _GitEvidencePort,
    receipts: _ReceiptPort,
    selector_digest: str,
) -> _VerificationResult:
    """Prove production evidence against trusted Git and receipt authorities."""
    errors: set[str] = set()
    selector = current_plan.get("selector")
    selector_version = selector.get("version") if isinstance(selector, Mapping) else ""
    plan_selector_digest = selector.get("digest") if isinstance(selector, Mapping) else ""
    recomputed = shadow.evaluate_candidate(
        closure,
        samples,
        selector_version=str(selector_version),
        selector_digest=str(plan_selector_digest),
        authoritative_evidence=authoritative_evidence,
    )
    if candidate != recomputed:
        errors.add("candidate-decision-not-canonical")
    if recomputed.get("eligible") is not True:
        errors.add("candidate-not-eligible")
    if recomputed.get("evidence_mode") != "qualifying-merged-change":
        errors.add("production-provenance-required")
    if selector_version != shadow.SELECTOR_VERSION or plan_selector_digest != selector_digest:
        errors.add("current-selector-not-authoritative")

    plan_range = current_plan.get("range")
    row = _candidate_row(current_plan)
    closure_id = closure.get("id")
    closure_digest = closure.get("input_digest")
    coverage = closure.get("coverage_mapping")
    source_revision = coverage.get("source_revision") if isinstance(coverage, Mapping) else None
    if (
        set(current_plan) != shadow.PLAN_FIELDS
        or current_plan.get("plan_digest")
        != shadow._canonical_digest(shadow._without_digest(current_plan, "plan_digest"))
        or not isinstance(plan_range, Mapping)
        or set(plan_range) != shadow.PLAN_RANGE_FIELDS
        or plan_range.get("merge_base_status") != "resolved"
        or not isinstance(row, Mapping)
        or set(row) != shadow.PLAN_CANDIDATE_FIELDS
        or row.get("id") != closure_id
        or row.get("input_digest") != closure_digest
        or row.get("command") != closure.get("command")
        or row.get("tests") != current_plan.get("tests")
        or row.get("applicability") != closure.get("current_applicability")
        or current_plan.get("changed_modules") != [closure_id]
        or current_plan.get("closure_digests") != {closure_id: closure_digest}
        or current_plan.get("decision") != "selective"
        or current_plan.get("fallback_reasons") != []
        or current_plan.get("commands") != [closure.get("command")]
        or recomputed.get("input_digest") != closure_digest
        or recomputed.get("source_revision") != source_revision
    ):
        errors.add("current-plan-closure-source-not-canonical")

    try:
        checkout = git.checkout_snapshot()
    except (OSError, RuntimeError):
        checkout = None
    if (
        not isinstance(checkout, _CheckoutSnapshot)
        or not checkout.ref.startswith("refs/heads/")
        or not shadow._is_sha(checkout.head)
        or not shadow._is_sha(checkout.tree)
    ):
        errors.add("live-checkout-not-verifiable")

    current_head = plan_range.get("head") if isinstance(plan_range, Mapping) else None
    try:
        current_tree = git.commit_tree(str(current_head)) if shadow._is_sha(current_head) else None
    except (OSError, RuntimeError):
        current_tree = None
    if not shadow._is_sha(current_tree):
        errors.add("current-plan-head-not-verifiable")
    if isinstance(checkout, _CheckoutSnapshot) and (
        current_head != checkout.head or current_tree != checkout.tree
    ):
        errors.add("current-plan-does-not-match-live-checkout")
    if isinstance(plan_range, Mapping):
        base = plan_range.get("base")
        merge_base = plan_range.get("merge_base")
        try:
            actual_merge_base = (
                git.merge_base(str(base), str(current_head))
                if shadow._is_sha(base) and shadow._is_sha(current_head)
                else None
            )
            base_tree = git.commit_tree(str(base)) if shadow._is_sha(base) else None
            merge_base_tree = (
                git.commit_tree(str(merge_base)) if shadow._is_sha(merge_base) else None
            )
        except (OSError, RuntimeError):
            actual_merge_base = None
            base_tree = None
            merge_base_tree = None
        if (
            not shadow._is_sha(base_tree)
            or not shadow._is_sha(merge_base_tree)
            or actual_merge_base != merge_base
        ):
            errors.add("current-plan-range-not-verifiable")

    try:
        source_tree = (
            git.commit_tree(str(source_revision)) if shadow._is_sha(source_revision) else None
        )
    except (OSError, RuntimeError):
        source_tree = None
    if not shadow._is_sha(source_tree):
        errors.add("source-revision-not-verifiable")

    seen_commits: set[str] = set()
    seen_trees: set[str] = set()
    seen_receipts: set[str] = set()
    for sample in samples:
        commit = sample.get("commit")
        tree = sample.get("tree")
        if commit in seen_commits or tree in seen_trees:
            errors.add("duplicate-verified-commit-or-tree")
        if isinstance(commit, str):
            seen_commits.add(commit)
        if isinstance(tree, str):
            seen_trees.add(tree)
        try:
            actual_tree = git.commit_tree(str(commit)) if shadow._is_sha(commit) else None
            merged = bool(
                isinstance(checkout, _CheckoutSnapshot)
                and actual_tree
                and git.is_ancestor(str(commit), checkout.head)
            )
        except (OSError, RuntimeError):
            actual_tree = None
            merged = False
        if actual_tree != tree or not merged:
            errors.add("sample-commit-tree-or-ancestry-not-verifiable")

        for receipt_name in ("selected", "full"):
            receipt = sample.get(receipt_name)
            receipt_id = receipt.get("receipt_id") if isinstance(receipt, Mapping) else None
            if not isinstance(receipt_id, str) or receipt_id in seen_receipts:
                errors.add("duplicate-or-invalid-local-receipt")
                continue
            seen_receipts.add(receipt_id)
            try:
                stored = receipts.load(receipt_id)
            except (OSError, RuntimeError):
                stored = None
            if (
                not isinstance(receipt, Mapping)
                or stored != receipt
                or receipt.get("status") != "completed"
                or receipt.get("commit") != commit
                or receipt.get("tree") != tree
            ):
                errors.add("completed-local-receipt-not-verifiable")

    if errors:
        return _VerificationResult(None, tuple(sorted(errors)))

    assert isinstance(plan_range, Mapping)
    assert isinstance(checkout, _CheckoutSnapshot)
    route_binding = {
        "schema_version": 1,
        "checkout_ref": checkout.ref,
        "checkout_head": checkout.head,
        "plan_digest": current_plan["plan_digest"],
        "base": plan_range["base"],
        "head": plan_range["head"],
        "merge_base": plan_range["merge_base"],
        "tree": current_tree,
        "closure_id": closure_id,
        "closure_input_digest": recomputed["input_digest"],
        "source_revision": recomputed["source_revision"],
        "candidate_decision_digest": recomputed["decision_digest"],
        "authoritative_evidence_digest": recomputed["authoritative_evidence_digest"],
        "evidence_digest": recomputed["evidence_digest"],
    }
    return _VerificationResult(route_binding, ())


def _full_route(*reasons: str) -> dict[str, Any]:
    return {
        "command": shadow.FULL_GATE,
        "selective": False,
        "closure": None,
        "reasons": sorted(set(reasons)),
    }


def _route_production_validation_with_ports(
    *,
    closure: Mapping[str, Any],
    samples: Sequence[Mapping[str, Any]],
    authoritative_evidence: Sequence[Mapping[str, Any]],
    candidate: Mapping[str, Any],
    current_plan: Mapping[str, Any],
    boundary: str,
    is_leaf: bool,
    local_enabled: bool,
    git: _GitEvidencePort,
    receipts: _ReceiptPort,
    selector_digest: str,
) -> dict[str, Any]:
    """Private adapter seam used by focused contract tests."""
    if not local_enabled:
        return _full_route("local-activation-disabled")
    if boundary not in shadow.LOCAL_LEAF_BOUNDARIES:
        return _full_route("boundary-requires-full-gate")
    if not is_leaf:
        return _full_route("non-leaf-work-item")

    verification = _verify_production_evidence(
        closure=closure,
        samples=samples,
        authoritative_evidence=authoritative_evidence,
        candidate=candidate,
        current_plan=current_plan,
        git=git,
        receipts=receipts,
        selector_digest=selector_digest,
    )
    if verification.errors:
        return _full_route("production-evidence-not-verified", *verification.errors)

    # Re-read every Git and receipt authority immediately before choosing the route. Immutable
    # object IDs plus an identical second binding make ref/store swaps fail closed without
    # exporting a replayable token or caller-supplied binding.
    reverified = _verify_production_evidence(
        closure=closure,
        samples=samples,
        authoritative_evidence=authoritative_evidence,
        candidate=candidate,
        current_plan=current_plan,
        git=git,
        receipts=receipts,
        selector_digest=selector_digest,
    )
    if reverified.errors or reverified.route_binding != verification.route_binding:
        return _full_route("production-authority-snapshot-changed", *reverified.errors)

    try:
        final_snapshot = git.checkout_snapshot()
    except (OSError, RuntimeError):
        final_snapshot = None
    expected_snapshot = _CheckoutSnapshot(
        ref=str(verification.route_binding["checkout_ref"]),
        head=str(verification.route_binding["checkout_head"]),
        tree=str(verification.route_binding["tree"]),
    )
    if final_snapshot != expected_snapshot:
        return _full_route("production-authority-snapshot-changed")

    command = candidate.get("command")
    closure_id = candidate.get("id")
    if not isinstance(command, str) or not isinstance(closure_id, str):
        return _full_route("production-evidence-not-verified")
    return {"command": command, "selective": True, "closure": closure_id, "reasons": []}


def route_production_validation(
    *,
    closure: Mapping[str, Any],
    samples: Sequence[Mapping[str, Any]],
    authoritative_evidence: Sequence[Mapping[str, Any]],
    candidate: Mapping[str, Any],
    current_plan: Mapping[str, Any],
    boundary: str,
    is_leaf: bool,
    local_enabled: bool,
) -> dict[str, Any]:
    """Verify repository-owned authorities and atomically return a production route."""
    git = _SubprocessGitEvidence(shadow.ROOT)
    common_dir = git.common_dir()
    if common_dir is None:
        return _full_route("production-repository-not-verifiable")
    try:
        receipts = _LocalReceiptDirectory(common_dir / "bh" / "shadow-validation" / "receipts")
    except OSError:
        return _full_route("production-receipt-store-not-verifiable")
    try:
        selector_digest = shadow._digest_bytes(shadow.SELECTOR_PATH.read_bytes())
    except OSError:
        return _full_route("production-selector-not-verifiable")
    return _route_production_validation_with_ports(
        closure=closure,
        samples=samples,
        authoritative_evidence=authoritative_evidence,
        candidate=candidate,
        current_plan=current_plan,
        boundary=boundary,
        is_leaf=is_leaf,
        local_enabled=local_enabled,
        git=git,
        receipts=receipts,
        selector_digest=selector_digest,
    )
