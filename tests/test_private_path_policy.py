"""Repository-wide tripwires for Beadhive's two-root private-storage policy."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

from beadhive import private_paths

ROOT = Path(__file__).parents[1]
SOURCE = ROOT / "src" / "beadhive"
_HIDDEN = re.compile(r"^(\.[A-Za-z0-9_-]+)(?:/|$)")
_GIT_BH = re.compile(r"^bh-[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)?$")


@dataclass(frozen=True)
class _Use:
    module: str
    context: str
    value: str
    line: int

    @property
    def root(self) -> str | None:
        match = _HIDDEN.match(self.value)
        return match.group(1) if match else None

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.module, self.context, self.value)


def _static_value(node: ast.AST, names: dict[str, object]) -> object | None:
    """Evaluate the small side-effect-free value domain needed by the path audit."""
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str) or type(node.value) is int:
            return node.value
    if isinstance(node, ast.Name):
        return names.get(node.id)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        operand = _static_value(node.operand, names)
        if type(operand) is int:
            return +operand if isinstance(node.op, ast.UAdd) else -operand
    if isinstance(node, ast.Subscript):
        value = _static_value(node.value, names)
        index = _static_value(node.slice, names)
        if isinstance(value, tuple) and type(index) is int:
            try:
                return value[index]
            except IndexError:
                return None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _static_value(node.left, names), _static_value(node.right, names)
        return left + right if isinstance(left, str) and isinstance(right, str) else None
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                return None
            parts.append(value.value)
        return "".join(parts)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        values = tuple(_static_value(element, names) for element in node.elts)
        return None if any(value is None for value in values) else values
    if isinstance(node, ast.Dict):
        values = tuple(_static_value(key, names) for key in node.keys if key is not None)
        invalid = len(values) != len(node.keys) or any(value is None for value in values)
        return None if invalid else values
    return None


def _static(node: ast.AST, names: dict[str, object]) -> str | None:
    value = _static_value(node, names)
    return value if isinstance(value, str) else None


def _target_names(target: ast.AST) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        return set().union(*(_target_names(element) for element in target.elts))
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    return set()


def _bind_static(target: ast.AST, value: object, names: dict[str, object]) -> bool:
    if isinstance(target, ast.Name):
        names[target.id] = value
        return True
    if isinstance(target, ast.Starred):
        return _bind_static(target.value, value, names)
    if isinstance(target, (ast.Tuple, ast.List)) and isinstance(value, tuple):
        starred = [
            index for index, element in enumerate(target.elts) if isinstance(element, ast.Starred)
        ]
        if len(starred) > 1:
            return False
        if starred:
            star = starred[0]
            suffix_count = len(target.elts) - star - 1
            if len(value) < len(target.elts) - 1:
                return False
            expanded = (
                value[:star]
                + (value[star : len(value) - suffix_count if suffix_count else len(value)],)
                + (value[len(value) - suffix_count :] if suffix_count else ())
            )
        else:
            if len(target.elts) != len(value):
                return False
            expanded = value
        return all(
            _bind_static(element, item, names)
            for element, item in zip(target.elts, expanded, strict=True)
        )
    return False


def _module_constants(tree: ast.Module) -> dict[str, object]:
    names: dict[str, object] = {}
    pending = [node for node in tree.body if isinstance(node, (ast.Assign, ast.AnnAssign))]
    for _ in range(len(pending) + 1):
        changed = False
        for node in pending:
            value_node = node.value
            value = _static_value(value_node, names) if value_node is not None else None
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if value is None:
                continue
            before = names.copy()
            for target in targets:
                _bind_static(target, value, names)
            changed = changed or names != before
        if not changed:
            break
    return names


class _PathUseScanner(ast.NodeVisitor):
    def __init__(self, module: str, tree: ast.Module):
        self.module = module
        self.names = _module_constants(tree)
        self.path_modules: set[str] = set()
        self.path_constructors = {"Path"}
        self.context = ["<module>"]
        self.uses: set[_Use] = set()

    def _record(self, node: ast.AST, value: str | None) -> None:
        if value is None:
            return
        hidden = _HIDDEN.match(value)
        basename = Path(value).name
        git_private_candidate = _GIT_BH.fullmatch(basename) and (
            basename.endswith((".json", ".log")) or basename == "bh-build"
        )
        if hidden or git_private_candidate:
            self.uses.add(_Use(self.module, self.context[-1], value, node.lineno))

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        old = self.names.copy()
        old_path_modules = self.path_modules.copy()
        old_path_constructors = self.path_constructors.copy()
        self.context.append(node.name)

        # Function signature and decorator expressions are not in ``body``. Walk each AST
        # placement exactly once so defaults and annotations cannot evade the path policy.
        for decorator in node.decorator_list:
            self.visit(decorator)
        self.visit(node.args)
        if node.returns is not None:
            self.visit(node.returns)
        for type_param in getattr(node, "type_params", ()):
            self.visit(type_param)
        for statement in node.body:
            self.visit(statement)

        self.context.pop()
        self.names = old
        self.path_modules = old_path_modules
        self.path_constructors = old_path_constructors

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        old = self.names.copy()
        old_path_modules = self.path_modules.copy()
        old_path_constructors = self.path_constructors.copy()
        self.context.append(node.name)
        for decorator in node.decorator_list:
            self.visit(decorator)
        for base in node.bases:
            self.visit(base)
        for keyword in node.keywords:
            self.visit(keyword)
        for type_param in getattr(node, "type_params", ()):
            self.visit(type_param)
        for statement in node.body:
            self.visit(statement)
        self.context.pop()
        self.names = old
        self.path_modules = old_path_modules
        self.path_constructors = old_path_constructors

    def visit_Lambda(self, node: ast.Lambda) -> None:
        # Keep the enclosing function/class/module context, but make the complete traversal
        # explicit: ``ast.arguments`` includes positional and keyword-only defaults.
        self.visit(node.args)
        self.visit(node.body)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name == "pathlib":
                self.path_modules.add(alias.asname or "pathlib")

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module == "pathlib":
            for alias in node.names:
                if alias.name == "Path":
                    self.path_constructors.add(alias.asname or "Path")

    def visit_Assign(self, node: ast.Assign) -> None:
        value = _static_value(node.value, self.names)
        for target in node.targets:
            for name in _target_names(target):
                self.names.pop(name, None)
            if value is not None:
                _bind_static(target, value, self.names)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        value = _static_value(node.value, self.names) if node.value is not None else None
        for name in _target_names(node.target):
            self.names.pop(name, None)
        if value is not None:
            _bind_static(node.target, value, self.names)
        self.generic_visit(node)

    def _visit_for(self, node: ast.For | ast.AsyncFor) -> None:
        self.visit(node.iter)
        values = _static_value(node.iter, self.names)
        items = values if isinstance(values, tuple) else None
        old = self.names.copy()
        if items is None:
            for name in _target_names(node.target):
                self.names.pop(name, None)
            for statement in node.body:
                self.visit(statement)
        else:
            binding_failed = False
            for item in items:
                self.names = old.copy()
                for name in _target_names(node.target):
                    self.names.pop(name, None)
                if _bind_static(node.target, item, self.names):
                    for statement in node.body:
                        self.visit(statement)
                else:
                    binding_failed = True
            if binding_failed:
                self.names = old.copy()
                for name in _target_names(node.target):
                    self.names.pop(name, None)
                for statement in node.body:
                    self.visit(statement)
        self.names = old
        for statement in node.orelse:
            self.visit(statement)

    def visit_For(self, node: ast.For) -> None:
        self._visit_for(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self._visit_for(node)

    def _visit_comprehension(
        self,
        generators: list[ast.comprehension],
        index: int,
        result_nodes: tuple[ast.AST, ...],
    ) -> None:
        if index == len(generators):
            for result in result_nodes:
                self.visit(result)
            return
        generator = generators[index]
        self.visit(generator.iter)
        values = _static_value(generator.iter, self.names)
        items = values if isinstance(values, tuple) else None
        old = self.names.copy()
        if items is None:
            for name in _target_names(generator.target):
                self.names.pop(name, None)
            for condition in generator.ifs:
                self.visit(condition)
            self._visit_comprehension(generators, index + 1, result_nodes)
        else:
            binding_failed = False
            for item in items:
                self.names = old.copy()
                for name in _target_names(generator.target):
                    self.names.pop(name, None)
                if not _bind_static(generator.target, item, self.names):
                    binding_failed = True
                    continue
                for condition in generator.ifs:
                    self.visit(condition)
                self._visit_comprehension(generators, index + 1, result_nodes)
            if binding_failed:
                self.names = old.copy()
                for name in _target_names(generator.target):
                    self.names.pop(name, None)
                for condition in generator.ifs:
                    self.visit(condition)
                self._visit_comprehension(generators, index + 1, result_nodes)
        self.names = old

    def _visit_comp(self, node: ast.AST, *result_nodes: ast.AST) -> None:
        old = self.names.copy()
        self._visit_comprehension(node.generators, 0, result_nodes)
        self.names = old

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._visit_comp(node, node.elt)

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._visit_comp(node, node.elt)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._visit_comp(node, node.elt)

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._visit_comp(node, node.key, node.value)

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if isinstance(node.op, ast.Div):
            self._record(node.right, _static(node.right, self.names))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        is_path = (isinstance(node.func, ast.Name) and node.func.id in self.path_constructors) or (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "Path"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in self.path_modules
        )
        is_join = isinstance(node.func, ast.Attribute) and node.func.attr in {"join", "joinpath"}
        if is_path or is_join:
            for arg in node.args:
                self._record(arg, _static(arg, self.names))
        self.generic_visit(node)


def _scan(module: str, source: str) -> set[_Use]:
    tree = ast.parse(source, filename=module)
    scanner = _PathUseScanner(module, tree)
    scanner.visit(tree)
    return scanner.uses


# Each exception names its owner, exact source module, exact function context, and exact static
# operand. It cannot authorize the same root in a different function (for example `hive/'.ssh'`).
def _owned(module: str, context: str, *values: str) -> set[tuple[str, str, str]]:
    return {(f"src/beadhive/{module}", context, value) for value in values}


_EXACT_OWNERSHIP_EXCEPTIONS = frozenset(
    # Beads/Dolt own their repository and embedded-store roots.
    _owned("backup.py", "pre_migrate_stores", ".beads")
    | _owned("backup.py", "hive_backup_dir", ".beads")
    | _owned("backup.py", "bd_backup_target", ".beads")
    | _owned("backup.py", "_hive_rotated_dirs", ".beads")
    | _owned("bd.py", "<module>", ".beads")
    | _owned("doctor.py", "_data_prefix_mismatches", ".beads")
    | _owned("doctor.py", "_data_node_id", ".beads")
    | _owned("doctor.py", "_data_beads_role", ".beads")
    | _owned("doctor.py", "_data_store_engine", ".beads")
    | _owned("doctor.py", "_this_host_manifest", ".beads")
    | _owned("doctor.py", "_data_warnings", ".beads")
    | _owned("doctor.py", "_bd_schema_skew_warnings", ".beads")
    | _owned("dolt_health.py", "probe_embedded_schema_version", ".dolt")
    | _owned("dolt_health.py", "probe_embedded_lineage", ".dolt")
    | _owned("hive.py", "cleanup_failed_bd_init", ".beads")
    | _owned("hive_migrate.py", "<module>", ".beads")
    | _owned("hive_ready.py", "_deprecation_checks", ".beads/PRIME.md")
    | _owned("hive_ready.py", "_schema_version_check", ".beads")
    | _owned("hive_ready.py", "scan", ".beads")
    | _owned("hive_repair.py", "detect", ".beads")
    | _owned("hive_repair.py", "detect_node_id", ".beads")
    | _owned("hive_repair.py", "detect_role", ".beads")
    | _owned("hive_repair.py", "detect_server_database", ".beads")
    | _owned("host_fence.py", "transport_lookup", ".beads")
    | _owned("host_provision.py", "_beads_dirs", ".beads")
    | _owned("host_provision.py", "_store_state", ".beads")
    | _owned("host_provision.py", "status", ".beads")
    | _owned("host_retire.py", "_hq_fold", ".beads")
    | _owned("hq.py", "_hq_dir_or_exit", ".beads")
    | _owned("hq.py", "_wire_remote", ".beads")
    | _owned("hq_restore.py", "_plan_jsonl", ".beads")
    | _owned("hub.py", "ensure_store", ".beads")
    | _owned("hub.py", "_retire_legacy_hub", ".beads")
    | _owned("hub.py", "_adopt_cache_identity", ".beads")
    | _owned("hub.py", "_sync_hive", ".beads")
    | _owned("hub.py", "sync", ".beads")
    | _owned("hub.py", "bounded_bd", ".beads")
    | _owned("localloop.py", "_default_instructions", ".beads")
    | _owned("mcp.py", "hq_intake_resource", ".beads")
    | _owned("onboard.py", "_act_bd_init", ".beads")
    | _owned("publish_export.py", "_resolve_hive_root", ".beads")
    | _owned("report.py", "_target", ".beads")
    | _owned("safety.py", "_scan_bd_dolt_state", ".beads")
    | _owned("storage_migrate.py", "_metadata_path", ".beads")
    | _owned("storage_migrate.py", "shared_server_target_dir", ".beads")
    | _owned("storage_migrate.py", "_tracked_gitignore_lines", ".beads", ".gitignore")
    | _owned("storage_migrate.py", "_ensure_gitignore_pattern", ".beads", ".gitignore")
    | _owned("storage_migrate.py", "migrate_hive", ".beads")
    | _owned("storage_migrate.py", "plan_targets", ".beads")
    | _owned("store_locator.py", "<module>", ".beads")
    | _owned("store_locator.py", "embedded_store_dir", ".beads")
    # Harnesses/plugins own these exact configuration and agent-install callsites.
    | _owned("config_paths.py", "plugin_root", ".claude-plugin")
    | _owned("config_release.py", "_marketplace_root", ".claude-plugin")
    | _owned("hive.py", "_link_skills_claude", ".claude", ".claude/skills")
    | _owned("hive.py", "_install_agents_claude", ".claude")
    | _owned("hive.py", "_install_plugin_claude", ".claude-plugin")
    | _owned("hive.py", "_install_claude_settings", ".claude", ".claude/settings.json")
    | _owned("hive.py", "_install_agents_opencode", ".opencode")
    | _owned("hive.py", "_install_bd_steer_opencode", ".opencode")
    | _owned("hive.py", "_commit_scaffolding", ".beads", ".claude", ".opencode")
    | _owned("hive.py", "_install_sandbox_grant", ".claude", ".claude/settings.local.json")
    | _owned("hive.py", "granted_subtree", ".claude")
    | _owned("hive.py", "_install_codex_sandbox_grant", ".codex")
    | _owned("hive.py", "codex_granted_subtree", ".codex")
    | _owned("hive_migrate.py", "<module>", ".claude")
    | _owned("hive_ready.py", "_has_bundled_agent", ".claude")
    | _owned("hive_ready.py", "scan", ".claude/settings.json")
    | _owned("role.py", "_local_agent_override", ".claude", ".opencode")
    | _owned("repowise_plugin.py", "_state", ".repowise")
    | _owned("repowise_plugin.py", "readiness", ".repowise")
    | _owned("repowise_plugin.py", "_backfill_vscode_config", ".repowise/config.yaml")
    | _owned("repowise_plugin.py", "_install_workspace_overlay", ".repowise-workspace")
    # Git-native administration and GitHub repository configuration.
    | _owned("config_policy.py", "hq_has_remote", ".git")
    | _owned("doctor.py", "_scan", ".git")
    | _owned("doctor.py", "_channel_drift_warnings", ".git")
    | _owned("doctor.py", "_hq_ahead_warnings", ".git")
    | _owned("guard.py", "primary_state", ".git")
    | _owned("herdr_plugin.py", "_managed_worktree", ".git")
    | _owned("hive.py", "_git_exclude", ".git", ".git/info/exclude")
    | _owned("hive.py", "_remove_stealth_exclude", ".git/info/exclude")
    | _owned("hive.py", "_ensure_stealth_exclude", ".git/info/exclude")
    | _owned("hive.py", "_ensure_export_exclude", ".git/info/exclude")
    | _owned("hive.py", "_relocate_bd_gitignore", ".gitignore", ".git/info/exclude")
    | _owned("hive_migrate.py", "migrate", ".git")
    | _owned("host_adopt.py", "adopt", ".git")
    | _owned("host_cli.py", "_require_hq_dir", ".git")
    | _owned("hub.py", "_fetch_cache", ".git")
    | _owned("metadata.py", "_fleet_keys", ".git")
    | _owned("metadata.py", "fingerprint", ".git")
    | _owned("onboard.py", "build_steps", ".git")
    | _owned("orca.py", "discover_repos", ".git")
    | _owned("seatrun.py", "validate_workspace", ".git")
    | _owned("validation_ledger.py", "_verdict_path", ".git")
    | _owned("validation_ledger.py", "_legacy_ledger_path", ".git")
    | _owned("validation_records.py", "_validation_root", ".git")
    | _owned("worktree.py", "add", ".git")
    | _owned("worktree.py", "preview", ".git")
    | _owned("worktree.py", "ensure", ".git")
    | _owned("worktree_inventory.py", "impl__managed_for_entry", ".git")
    | _owned("worktree_inventory.py", "impl_unregistered_worktrees", ".git")
    | _owned("worktree_merge.py", "merge_with_union", ".git")
    | _owned("validate_probe.py", "_find_justfile", ".justfile")
    # User/workspace config paths, never repository-private state.
    | _owned("config.py", "env_file", ".env")
    | _owned("config_services.py", "archive_dir", ".archived")
    | _owned("dispatch_supervisor.py", "_systemd_user_dir", ".config")
    | _owned("doctor.py", "_missing_required_dep_warnings", ".nix-profile")
    | _owned("doctor.py", "_devshell_only_warnings", ".nix-profile")
    | _owned("host.py", "discover_signing_key", ".ssh")
    | _owned("install_plane.py", "<module>", ".nix-profile")
    # Bounded compatibility readers/inventory for retired top-level `.git/bh-*` paths.
    | _owned("claim_authority.py", "_legacy_record_path", "bh-claim.json")
    | _owned(
        "private_paths.py",
        "inventory_private_state",
        "bh-validation-ledger.json",
        "bh-release-bump-gate.json",
        "bh-release-bump-gate.log",
        "bh-build",
        "bh-claim.json",
    )
    | _owned("release.py", "_legacy_marker_path", "bh-release-bump-gate.json", ".git")
    | _owned("release.py", "_legacy_gate_log_path", "bh-release-bump-gate.log", ".git")
    | _owned("validation_ledger.py", "_legacy_ledger_path", "bh-validation-ledger.json")
)


def _violations(module: str, source: str) -> list[_Use]:
    return sorted(
        (
            use
            for use in _scan(module, source)
            if use.root != private_paths.REPO_PRIVATE_DIRNAME
            and use.key not in _EXACT_OWNERSHIP_EXCEPTIONS
        ),
        key=lambda use: (use.module, use.line, use.value),
    )


def test_no_undocumented_repo_hidden_roots_outside_bh():
    violations = []
    for path in sorted(SOURCE.glob("*.py")):
        module = str(path.relative_to(ROOT))
        violations.extend(_violations(module, path.read_text()))
    assert not violations, (
        "Beadhive-owned repo-private state belongs below .bh/; add only an exact "
        "(module, function, static path) ownership exception:\n"
        + "\n".join(f"{use.module}:{use.line} {use.context}: {use.value}" for use in violations)
    )


def test_scanner_resolves_named_constants_and_static_concatenation():
    source = """
from pathlib import Path
HIDDEN = '.secret'
def named():
    return Path(HIDDEN)
def computed(root):
    return root / ('.' + 'computed')
"""
    uses = _violations("hostile.py", source)
    assert {(use.context, use.value) for use in uses} == {
        ("named", ".secret"),
        ("computed", ".computed"),
    }


def test_scanner_recognizes_qualified_and_aliased_pathlib_constructors():
    source = """
import pathlib
import pathlib as pl
from pathlib import Path as P
def qualified():
    return pathlib.Path('.qualified')
def module_alias():
    return pl.Path('.module-alias')
def constructor_alias():
    return P('.constructor-alias')
"""
    uses = _violations("hostile.py", source)
    assert {(use.context, use.value) for use in uses} == {
        ("qualified", ".qualified"),
        ("module_alias", ".module-alias"),
        ("constructor_alias", ".constructor-alias"),
    }


def test_scanner_walks_function_signature_decorator_and_annotation_fields():
    source = """
import pathlib
import pathlib as pl
from pathlib import Path as P
def decorate(value):
    return lambda function: function
@decorate(pl.Path('.decorator'))
def hostile(
    positional: P('.arg-annotation') = pathlib.Path('.pos-default'),
    *args: pl.Path('.vararg-annotation'),
    keyword: P('.kw-annotation') = pl.Path('.kw-default'),
    **kwargs: pathlib.Path('.kwargs-annotation'),
) -> P('.return-annotation'):
    return positional
"""
    uses = _violations("hostile.py", source)
    assert {use.value for use in uses if use.context == "hostile"} == {
        ".decorator",
        ".arg-annotation",
        ".pos-default",
        ".vararg-annotation",
        ".kw-annotation",
        ".kw-default",
        ".kwargs-annotation",
        ".return-annotation",
    }


def test_scanner_walks_async_lambda_and_class_attached_expressions():
    source = """
import pathlib as pl
from pathlib import Path as P
def decorate(value):
    return lambda target: target
@decorate(pl.Path('.class-decorator'))
class Hostile:
    placed = lambda value=P('.lambda-default'): pl.Path('.lambda-body')
@decorate(P('.async-decorator'))
async def async_hostile(value=pl.Path('.async-default')):
    return value
"""
    uses = _violations("hostile.py", source)
    assert {(use.context, use.value) for use in uses} >= {
        ("Hostile", ".class-decorator"),
        ("Hostile", ".lambda-default"),
        ("Hostile", ".lambda-body"),
        ("async_hostile", ".async-decorator"),
        ("async_hostile", ".async-default"),
    }


def test_scanner_resolves_static_comprehension_bindings():
    source = """
import pathlib as pl
from pathlib import Path
def hostile(root):
    list_paths = [Path(name) for name in ('.boundcomp',)]
    set_paths = {pl.Path(name) for name in ('.setcomp',) if name.startswith('.')}
    dict_paths = {name: root.joinpath(name) for name in {'.dictcomp': 1}}
    generator_paths = (
        root / name
        for group in (('.nested-one', '.nested-two'),)
        for name in group
        if name
    )
    unpacked = [
        (pl.Path(left), root.joinpath(right))
        for left, right in (('.tuple-left', '.tuple-right'),)
    ]
    return list_paths, set_paths, dict_paths, generator_paths, unpacked
"""
    uses = _violations("hostile.py", source)
    assert {use.value for use in uses if use.context == "hostile"} == {
        ".boundcomp",
        ".setcomp",
        ".dictcomp",
        ".nested-one",
        ".nested-two",
        ".tuple-left",
        ".tuple-right",
    }


def test_scanner_resolves_ordinary_for_bindings_without_leaking_scope():
    source = """
import pathlib as pl
from pathlib import Path as P
def hostile(root):
    for name in ('.for-one', '.for-two'):
        P(name)
    for group in (('.nested-for',),):
        for name in group:
            pl.Path(name)
    for left, right in (('.for-left', '.for-right'),):
        pl.Path(left)
        root.joinpath(right)
    name = '.assigned-after-loop'
    return root / name
"""
    uses = _violations("hostile.py", source)
    assert {use.value for use in uses if use.context == "hostile"} == {
        ".for-one",
        ".for-two",
        ".nested-for",
        ".for-left",
        ".for-right",
        ".assigned-after-loop",
    }


def test_scanner_binds_starred_for_and_comprehension_targets():
    source = """
import pathlib as pl
from pathlib import Path as P
def hostile():
    for first, *rest in (('.bound', '.tail'),):
        P(first)
        pl.Path(rest[0])
        P('.direct-in-live-body')
    for *prefix, last in (('.prefix', '.suffix'),):
        pl.Path(prefix[0])
        P(last)
    return [
        (P(first), pl.Path(rest[0]))
        for first, *rest in (('.comp-first', '.comp-rest'),)
        if P('.direct-filter')
    ]
"""
    uses = _violations("hostile.py", source)
    assert {use.value for use in uses if use.context == "hostile"} == {
        ".bound",
        ".tail",
        ".direct-in-live-body",
        ".prefix",
        ".suffix",
        ".comp-first",
        ".comp-rest",
        ".direct-filter",
    }


def test_scanner_resolves_signed_literal_and_named_sequence_indices():
    source = """
import pathlib as pl
from pathlib import Path
from pathlib import Path as P
ONE = 1
NEGATIVE = -ONE
POSITIVE = +ONE
def hostile(root):
    for first, *rest in (('.first', '.rest-zero', '.rest-one'),):
        Path(rest[-1])
        pl.Path(rest[+0])
        P(rest[NEGATIVE])
        root.joinpath(rest[-1])
        root / rest[POSITIVE]
    return [
        P(rest[NEGATIVE])
        for first, *rest in (('.comp-first', '.comp-zero', '.comp-one'),)
        if pl.Path(rest[-1])
    ]
"""
    uses = _violations("hostile.py", source)
    assert {use.value for use in uses if use.context == "hostile"} == {
        ".rest-zero",
        ".rest-one",
        ".comp-one",
    }


def test_invalid_or_unsupported_indices_do_not_skip_independent_paths():
    source = """
from pathlib import Path as P
def hostile():
    for first, *rest in (('.first', '.rest'),):
        P(rest[-99])
        P(rest[True])
        P(rest[0:])
        P('.direct-after-unknown-index')
    return [
        P('.direct-comp-after-unknown-index')
        for first, *rest in (('.comp-first', '.comp-rest'),)
        if P(rest[-99])
    ]
"""
    uses = _violations("hostile.py", source)
    assert {use.value for use in uses if use.context == "hostile"} == {
        ".direct-after-unknown-index",
        ".direct-comp-after-unknown-index",
    }


def test_failed_or_nonstatic_iteration_still_visits_independent_paths_once():
    source = """
import pathlib as pl
from pathlib import Path as P
def runtime_values():
    return object()
def hostile():
    for left, right in (('.cannot-unpack',),):
        P('.direct-failed-for')
    for unknown in runtime_values():
        pl.Path('.direct-unknown-for')
    failed_comp = [
        P('.direct-failed-comp')
        for left, right in (('.cannot-unpack-either',),)
        if pl.Path('.direct-failed-filter')
    ]
    unknown_comp = [pl.Path('.direct-unknown-comp') for value in runtime_values()]
    return failed_comp, unknown_comp
"""
    uses = _violations("hostile.py", source)
    assert {use.value for use in uses if use.context == "hostile"} == {
        ".direct-failed-for",
        ".direct-unknown-for",
        ".direct-failed-comp",
        ".direct-failed-filter",
        ".direct-unknown-comp",
    }


def test_static_iteration_does_not_flag_hidden_strings_outside_path_construction():
    source = """
def safe_strings():
    values = [name.removeprefix('.') for name in ('.not-a-path',) if name]
    for name in ('.also-not-a-path',):
        values.append(name.upper())
    for first, *rest in (('.safe-first', '.safe-rest'),):
        values.extend((first, *rest))
    return values
"""
    assert _violations("safe.py", source) == []


def test_exception_is_context_specific_not_a_global_root_name():
    source = """
from pathlib import Path
def hostile(hive):
    return hive / '.ssh' / 'beadhive-private'
"""
    [violation] = _violations("hostile.py", source)
    assert violation.context == "hostile"
    assert violation.value == ".ssh"


def test_scanner_resolves_named_and_computed_git_private_siblings():
    source = """
NAME = 'bh-' + 'secret.json'
def hostile(git_dir):
    return git_dir / NAME
"""
    [violation] = _violations("hostile.py", source)
    assert violation.value == "bh-secret.json"
