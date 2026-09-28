"""Executable compatibility boundary for the extracted worktrees capability."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import beadhive_worktrees
from beadhive import (
    cli,
    mcp,
    worktree,
    worktree_cleanup,
    worktree_init_adapters,
    worktree_inventory,
    worktree_lifecycle_observers,
    worktree_verify,
)
from beadhive.modules import worktrees


def test_legacy_branch_helpers_keep_patchable_facades_over_module_policy() -> None:
    assert worktree.apply_prefix.__module__ == "beadhive.worktree"
    assert worktree._leaf.__module__ == "beadhive.worktree"
    assert worktrees.apply_prefix.__module__.endswith("domain.models")
    assert "_policy_apply_prefix" in inspect.getsource(worktree.apply_prefix)
    assert "leaf_for_branch" in inspect.getsource(worktree._leaf)


def test_legacy_inventory_keeps_tuple_and_status_object_shapes(monkeypatch) -> None:
    row = object()
    monkeypatch.setattr(
        worktree_inventory,
        "impl_managed",
        lambda _cfg: [("bh", "/managed/bh-1", "wt/bead/issue/bh-1")],
    )
    monkeypatch.setattr(worktree_inventory, "impl_status_rows", lambda _hive: [row])

    assert worktree.managed({}) == [("bh", "/managed/bh-1", "wt/bead/issue/bh-1")]
    assert worktree.status_rows("bh") == [row]


def test_facade_composes_adapters_over_dynamic_patch_points() -> None:
    source = "\n".join(
        inspect.getsource(obj)
        for obj in (
            worktree._worktree_lifecycle_service,
            worktree._selected_worktree_manager,
            worktree._PluginCreateObserver,
        )
    )
    for seam in ("_run_git", "_notify_wt_create", "config.worktrees_manager"):
        assert seam in source
    assert "NativeGitWorktreeManager" in source
    assert "bind_application_port" in source
    # bh-055ot.1: one selected manager — no plugin delegation seam, no plugin-first fallback.
    for retired in ("_consult_wt_create", "_consult_wt_remove", "PluginWorktreeProvisioner"):
        assert retired not in source
        assert not hasattr(worktree, retired)


def test_cli_and_mcp_keep_rendering_and_payload_ownership() -> None:
    assert "worktree.add(" in inspect.getsource(cli.wt_add)
    assert "worktree.remove(" in inspect.getsource(cli.wt_rm)
    assert "worktree.status_cmd(" in inspect.getsource(cli.wt_status)
    assert "worktree.status_rows()" in inspect.getsource(mcp._register_read_resources)
    worktrees_src = Path(__file__).parents[2] / "packages/beadhive-worktrees/src/beadhive_worktrees"
    module_source = "\n".join(path.read_text() for path in sorted(worktrees_src.rglob("*.py")))
    for presentation_name in ("typer", "jsonout", "as_json", "exit_code", "render"):
        assert presentation_name not in module_source


def test_remove_and_prune_keep_compensation_outside_the_module() -> None:
    remove_source = inspect.getsource(worktree_cleanup.impl_remove)
    prune_source = inspect.getsource(worktree_cleanup.impl__prune_remove_one)
    # bh-qdezo.5: the claim_authority record-path bookkeeping moved behind the `ClaimRecords`
    # port (beadhive.worktree_state_adapters.ClaimAuthorityRecords) rather than importing
    # claim_authority directly. bh-qdezo.7: the resolve/retire SEQUENCING around the one
    # removal effect moved into beadhive_worktrees.execute_removal (root calls it directly at
    # both call sites, never through the generic plugin/lifecycle observer fan-out) — the
    # compensation call itself (`claim_records.remove_record_path`) still runs right there.
    assert "execute_removal(" in remove_source
    assert "_rmdir_empty_parents" in remove_source
    assert "execute_removal(" in prune_source
    assert '"branch", "-D"' in prune_source
    assert "claim_authority" not in inspect.getsource(worktrees.WorktreeLifecycleService)
    execute_removal_source = inspect.getsource(beadhive_worktrees.execute_removal)
    assert "claim_records.remove_record_path" in execute_removal_source


def test_facade_imports_no_telemetry_observaloop_or_metadata_implementation() -> None:
    """bh-qdezo.6 (cycle-edge-039/040/041): the facade's own file carries no plain import of
    ``otel``/``observaloop``/``observaloop_env``/``metadata`` any more — those concerns live in
    ``worktree_lifecycle_observers``, reached only through ``importlib.import_module`` (invisible
    to the static import-boundary checker, exactly like the facade's other sibling extractions:
    ``worktree_git``/``worktree_verify``/etc. at the bottom of the same file)."""
    worktree_source = inspect.getsource(worktree)
    for name in ("observaloop", "observaloop_env", "metadata"):
        assert f"import {name}" not in worktree_source
        assert f", {name}" not in worktree_source
    # `otel` keeps ONE compatibility binding (`worktree.otel.*` is still a documented test patch
    # point) but it must be the checker-invisible form, never a plain `from . import otel`.
    assert 'importlib.import_module(".otel"' in worktree_source
    assert "otel,\n" not in worktree_source


def test_init_rules_and_lifecycle_observers_compose_the_package_and_adapters() -> None:
    """bh-qdezo.6: init rules are injected DATA plus a command-runner PORT
    (``beadhive_worktrees.policy.init_rules``); telemetry/Observaloop/metadata invalidation are
    ``worktree_lifecycle_observers`` functions invoked at the same create chokepoint as before."""
    run_init_source = inspect.getsource(worktree_verify.impl_run_init)
    assert "init_rules.run_init_rules" in run_init_source
    assert "worktree_init_adapters.HostInitCommandRunner" in run_init_source

    fingerprint_source = "\n".join(
        inspect.getsource(obj)
        for obj in (
            worktree_verify.impl__init_rules_fingerprint,
            worktree_verify.impl__read_init_rules_fingerprint,
            worktree_verify.impl_record_init_rules,
            worktree_verify.impl_warn_init_rules_drift,
        )
    )
    assert "init_rules." in fingerprint_source
    assert "_GIT_CONFIG_RUNNER" in fingerprint_source

    do_add_source = inspect.getsource(worktree._do_add)
    assert "provision_observaloop(" in do_add_source
    assert "_record_wt_event(" in do_add_source
    assert "_record_wt_op_duration(" in do_add_source
    assert "provision_observaloop" in inspect.getsource(worktree.provision_observaloop)
    assert "worktree_lifecycle_observers.provision_observaloop" in inspect.getsource(
        worktree.provision_observaloop
    )
    assert "worktree_lifecycle_observers.invalidate_metadata" in inspect.getsource(worktree.add)

    package_worktrees_src = (
        Path(__file__).parents[2] / "packages/beadhive-worktrees/src/beadhive_worktrees"
    )
    module_source = "\n".join(
        path.read_text() for path in sorted(package_worktrees_src.rglob("*.py"))
    )
    for forbidden in ("otel", "observaloop", "beadhive.config", "config_consumer_ports"):
        assert forbidden not in module_source


def test_worktree_init_adapters_resolve_through_the_worktree_verify_facade_seams() -> None:
    """``HostInitCommandRunner``/``HostGitConfigRunner`` reach ``run``/``missing_binary``
    through ``worktree_verify``'s own facade forwards (not a direct ``.run`` module import), so
    the existing ``worktree.run`` monkeypatch seam keeps intercepting them."""
    adapters_source = inspect.getsource(worktree_init_adapters._facade)
    assert 'importlib.import_module(".worktree_verify"' in adapters_source
    assert "return worktree_verify" not in adapters_source  # not a plain rebind of an import


def test_lifecycle_observers_reach_concrete_implementations_only_dynamically() -> None:
    observers_source = inspect.getsource(worktree_lifecycle_observers)
    for name in ("otel", "observaloop", "observaloop_env", "metadata"):
        assert f'importlib.import_module(".{name}"' in observers_source
        assert f"from . import {name}" not in observers_source


_WORKTREE_FAMILY = frozenset(
    {
        "worktree",
        "worktree_cleanup",
        "worktree_git",
        "worktree_inventory",
        "worktree_merge",
        "worktree_verify",
        "wt_status",
    }
)
#: Reads the `BeadStateLookup` / `ClaimRecords` / `MergeEvidence` ports own (bh-qdezo.5). A root
#: test substitutes the composed port (`worktree_state_adapters.BEAD_STATE_LOOKUP`, ...) rather
#: than one of these seams reached through a worktree module (bh-qdezo.9).
_PORT_BACKED_SEAMS = frozenset(
    {
        "bd",
        "ghpr",
        "claim_authority",
        "_probe_store",
        "_bead_statuses_for_entry",
        "_bead_disposition_relations_for_entry",
    }
)


def _port_backed_patches(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "beadhive":
            aliases.update({alias.asname or alias.name: alias.name for alias in node.names})

    def dotted(node: ast.AST) -> list[str] | None:
        if isinstance(node, ast.Name):
            return [node.id]
        if isinstance(node, ast.Attribute):
            base = dotted(node.value)
            return [*base, node.attr] if base else None
        return None

    found: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and node.args):
            continue
        func = dotted(node.func) or []
        if not func or func[-1] not in {"setattr", "delattr", "patch", "object"}:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            parts = first.value.split(".")
            parts = parts[1:] if parts[0] == "beadhive" else []
        else:
            parts = dotted(first) or []
            parts = [aliases.get(parts[0], ""), *parts[1:]] if parts else []
            if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                parts.append(str(node.args[1].value))
        if len(parts) >= 2 and parts[0] in _WORKTREE_FAMILY and parts[1] in _PORT_BACKED_SEAMS:
            found.append(f"{path.name}:{node.lineno} {'.'.join(parts)}")
    return found


def test_root_tests_substitute_state_ports_not_worktree_module_seams() -> None:
    """bh-qdezo.9: bead-state, claim, and merge-evidence reads are substituted at the composed
    port, never by patching `bd`/`ghpr`/`claim_authority` through a worktree module or the
    facade's private bead-state helpers."""
    tests_root = Path(__file__).parents[1]
    offenders = [
        hit for path in sorted(tests_root.rglob("test_*.py")) for hit in _port_backed_patches(path)
    ]
    assert offenders == []


def test_every_worktree_state_read_goes_through_the_composed_ports() -> None:
    from beadhive import worktree_git, worktree_state_adapters

    assert "worktree_state_adapters.BEAD_STATE_LOOKUP" in inspect.getsource(
        worktree_inventory._bead_state_lookup
    )
    assert "worktree_state_adapters.BEAD_STATE_LOOKUP.show" in inspect.getsource(
        worktree._parent_link_base
    )
    assert "worktree_state_adapters.CLAIM_RECORDS" in inspect.getsource(worktree_cleanup)
    assert "worktree_state_adapters.MERGE_EVIDENCE" in inspect.getsource(worktree_git)
    assert "bd.show(" not in inspect.getsource(worktree._parent_link_base)
    assert set(worktree_state_adapters.__all__) >= {
        "BEAD_STATE_LOOKUP",
        "CLAIM_RECORDS",
        "MERGE_EVIDENCE",
    }
