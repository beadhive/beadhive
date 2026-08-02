"""validate_probe.probe_validate_cmd (bh-l44i rework): resolves a bare `just <recipe>` through
the hive's own justfile instead of pattern-matching the top-level command string, so the
fleet-wide dominant `just check` -> `check: lint lint-md test` -> `uv run pytest` chain reads
as "runs tests", not "compile-only". Tri-state: False (runs tests) never warns, True (fully
resolved AND provably test-free) is the only warn-worthy result, None (can't resolve) never
warns either — see the module docstring for the confidence rationale."""

from __future__ import annotations

from beadhive import validate_probe as vp

# Mirrors this repo's OWN justfile shape closely enough to exercise the real pattern: a `check`
# recipe with plain deps, a `check-all` with a parenthesized dependency-with-args, and a `test`
# recipe whose body is the actual test runner.
_REAL_SHAPE_JUSTFILE = """\
# list available recipes
default:
    @just --list

# fast gate
check: lint lint-md test

# full gate
check-all: lint lint-md (test FULL)

lint:
    uv run ruff check
    uv run ruff format --check

lint-md:
    markdownlint-cli2

FAST := "not integration"
FULL := ""

test set=FAST:
    uv run pytest {{ if set == "" { "" } else { "-m " + quote(set) } }}
"""

# Same shape, but `check` never reaches a test recipe at all — genuinely compile-only.
_COMPILE_ONLY_JUSTFILE = """\
check: lint typecheck

lint:
    uv run ruff check

typecheck:
    uv run mypy src
"""


def _write(tmp_path, text, name="justfile"):
    (tmp_path / name).write_text(text)
    return tmp_path


# ---- the pinned acceptance case -----------------------------------------------


def test_just_check_resolving_to_pytest_is_not_test_free(tmp_path):
    """PINNED: `just check` with a justfile whose `check` recipe transitively runs pytest must
    NOT be flagged test-free (and therefore must not warn) — this is the exact false-positive
    bh-l44i's rework fixes."""
    root = _write(tmp_path, _REAL_SHAPE_JUSTFILE)
    assert vp.probe_validate_cmd("just check", root) is False


def test_just_check_all_parenthesized_dep_also_resolves_to_pytest(tmp_path):
    root = _write(tmp_path, _REAL_SHAPE_JUSTFILE)
    assert vp.probe_validate_cmd("just check-all", root) is False


# ---- genuine compile-only IS still caught -------------------------------------


def test_just_check_with_no_reachable_test_recipe_is_test_free(tmp_path):
    root = _write(tmp_path, _COMPILE_ONLY_JUSTFILE)
    assert vp.probe_validate_cmd("just check", root) is True


# ---- fast path: the raw command already says it ------------------------------


def test_raw_command_containing_test_substring_short_circuits(tmp_path):
    # no justfile needed at all — pytest/npm test/go test/cargo test/just test all match directly.
    assert vp.probe_validate_cmd("uv run pytest", None) is False
    assert vp.probe_validate_cmd("npm test", None) is False
    assert vp.probe_validate_cmd("go test ./...", None) is False
    assert vp.probe_validate_cmd("just test", None) is False
    assert vp.probe_validate_cmd("sh -c 'just check && just test'", None) is False


# ---- unresolvable -> None, never warn-worthy ----------------------------------


def test_no_justfile_is_unknown(tmp_path):
    assert vp.probe_validate_cmd("just check", tmp_path) is None


def test_no_root_is_unknown():
    assert vp.probe_validate_cmd("just check", None) is None


def test_non_just_command_is_unknown(tmp_path):
    root = _write(tmp_path, _COMPILE_ONLY_JUSTFILE)
    assert vp.probe_validate_cmd("make check", root) is None


def test_unresolved_recipe_reference_is_unknown(tmp_path):
    root = _write(tmp_path, "check: lint nonexistent-recipe\n\nlint:\n    ruff check\n")
    assert vp.probe_validate_cmd("just check", root) is None


def test_unknown_target_recipe_is_unknown(tmp_path):
    root = _write(tmp_path, _REAL_SHAPE_JUSTFILE)
    assert vp.probe_validate_cmd("just nope", root) is None


# ---- sh -c wrapping ------------------------------------------------------------


def test_sh_c_wrapped_just_check_resolves(tmp_path):
    root = _write(tmp_path, _REAL_SHAPE_JUSTFILE)
    assert vp.probe_validate_cmd("sh -c 'just check'", root) is False


def test_bash_c_wrapped_compile_only_resolves(tmp_path):
    root = _write(tmp_path, _COMPILE_ONLY_JUSTFILE)
    assert vp.probe_validate_cmd("bash -c 'just check'", root) is True


# ---- transitive `just <other>` call inside a recipe body ----------------------


def test_just_call_inside_body_is_followed(tmp_path):
    root = _write(
        tmp_path,
        "check:\n    just _inner\n\n_inner:\n    uv run pytest\n",
    )
    assert vp.probe_validate_cmd("just check", root) is False


# ---- Justfile / .justfile capitalization + alternate filenames ---------------


def test_capitalized_justfile_name_is_found(tmp_path):
    root = _write(tmp_path, _COMPILE_ONLY_JUSTFILE, name="Justfile")
    assert vp.probe_validate_cmd("just check", root) is True


# ---- recipe self-reference / dependency cycle never hangs ---------------------


def test_self_referencing_recipe_terminates(tmp_path):
    root = _write(tmp_path, "check: check\n    uv run pytest\n")
    assert vp.probe_validate_cmd("just check", root) is False


# ---- parsing internals (targeted, not just end-to-end) ------------------------


def test_parse_recipes_splits_deps_and_body():
    recipes = vp._parse_recipes(_REAL_SHAPE_JUSTFILE)
    assert recipes["check"]["deps"] == ["lint", "lint-md", "test"]
    assert recipes["check-all"]["deps"] == ["lint", "lint-md", "test"]
    assert "uv run pytest" in recipes["test"]["body"]
    assert "FAST" not in recipes  # `FAST := "..."` is a variable, not a recipe
    assert "FULL" not in recipes


def test_bare_just_target_skips_flags():
    assert vp._bare_just_target("just check") == "check"
    assert vp._bare_just_target("just --justfile x check") == "x"  # known limitation, documented
    assert vp._bare_just_target("just") is None
    assert vp._bare_just_target("make check") is None
