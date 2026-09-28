from __future__ import annotations

from pathlib import Path

from beadhive_worktrees import CommandOutcome
from beadhive_worktrees.policy import init_rules


class FakeCommandRunner:
    """No real subprocess: scripted outcomes keyed by the exact argv tuple, in call order."""

    def __init__(self, outcomes: dict[tuple[str, ...], CommandOutcome] | None = None) -> None:
        self.outcomes = outcomes or {}
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: list[str], *, cwd: Path) -> CommandOutcome:
        key = tuple(argv)
        self.calls.append(key)
        return self.outcomes.get(key, CommandOutcome(returncode=0))


def _report_warn():
    reports: list[str] = []
    warnings: list[str] = []
    return reports, warnings, reports.append, warnings.append


def test_run_init_rules_respects_if_exists_and_tolerates_failure(tmp_path: Path) -> None:
    (tmp_path / "trigger.txt").write_text("")
    rules = [
        {"if_exists": "trigger.txt", "run": "touch matched.marker"},
        {"if_exists": "absent.txt", "run": "touch unmatched.marker"},
        {"run": "false"},
    ]
    runner = FakeCommandRunner({("false",): CommandOutcome(returncode=1)})
    reports, warnings, report, warn = _report_warn()

    ok = init_rules.run_init_rules(rules, tmp_path, runner=runner, report=report, warn=warn)

    assert ok is False
    assert runner.calls == [("touch", "matched.marker"), ("false",)]
    assert any("'false' exited 1" in line for line in warnings)
    assert any("1 optional provisioning rule(s) failed" in line for line in warnings)


def test_run_init_rules_verify_only_filters_to_flagged_rules(tmp_path: Path) -> None:
    rules = [
        {"run": "flagged-cmd", "verify": True},
        {"run": "unflagged-cmd"},
    ]
    runner = FakeCommandRunner()
    _reports, _warnings, report, warn = _report_warn()

    init_rules.run_init_rules(
        rules, tmp_path, verify_only=True, runner=runner, report=report, warn=warn
    )

    assert runner.calls == [("flagged-cmd",)]


def test_run_init_rules_default_mode_runs_flagged_and_unflagged(tmp_path: Path) -> None:
    rules = [
        {"run": "flagged-cmd", "verify": True},
        {"run": "unflagged-cmd"},
    ]
    runner = FakeCommandRunner()
    _reports, _warnings, report, warn = _report_warn()

    init_rules.run_init_rules(rules, tmp_path, runner=runner, report=report, warn=warn)

    assert runner.calls == [("flagged-cmd",), ("unflagged-cmd",)]


def test_run_init_rules_missing_binary_warns_and_counts_as_failed(tmp_path: Path) -> None:
    rules = [{"run": "ghost-tool"}]
    runner = FakeCommandRunner({("ghost-tool",): CommandOutcome(returncode=127, missing=True)})
    _reports, warnings, report, warn = _report_warn()

    ok = init_rules.run_init_rules(rules, tmp_path, runner=runner, report=report, warn=warn)

    assert ok is False
    assert any("command not found: ghost-tool" in line for line in warnings)


def test_run_init_rules_relays_runner_notes_and_warnings_in_order(tmp_path: Path) -> None:
    rules = [{"run": "cached-cmd"}]
    runner = FakeCommandRunner(
        {
            ("cached-cmd",): CommandOutcome(
                returncode=0,
                notes=("  → cache[uv] warm /cache",),
                warnings=("  ⚠ cache locality: fallback to copy",),
            )
        }
    )
    reports, warnings, report, warn = _report_warn()

    ok = init_rules.run_init_rules(rules, tmp_path, runner=runner, report=report, warn=warn)

    assert ok is True
    assert reports == ["  → cached-cmd", "  → cache[uv] warm /cache"]
    assert warnings == ["  ⚠ cache locality: fallback to copy"]


def test_fingerprint_is_mapping_order_independent_but_rule_order_sensitive() -> None:
    first = [{"run": "echo first", "if_exists": "one"}, {"run": "echo second"}]
    same = [{"if_exists": "one", "run": "echo first"}, {"run": "echo second"}]
    reversed_rules = list(reversed(first))

    assert init_rules.rules_fingerprint(first) == init_rules.rules_fingerprint(same)
    assert init_rules.rules_fingerprint(first) != init_rules.rules_fingerprint(reversed_rules)


def test_record_fingerprint_writes_the_current_digest(tmp_path: Path) -> None:
    rules = [{"run": "echo hi"}]
    runner = FakeCommandRunner()
    _reports, warnings, _report, warn = _report_warn()

    ok = init_rules.record_fingerprint(tmp_path, rules, runner=runner, warn=warn)

    assert ok is True
    assert warnings == []
    assert runner.calls[0] == (
        "git",
        "-C",
        str(tmp_path),
        "config",
        "extensions.worktreeConfig",
        "true",
    )
    assert runner.calls[1] == (
        "git",
        "-C",
        str(tmp_path),
        "config",
        "--worktree",
        init_rules.INIT_RULES_CONFIG_KEY,
        init_rules.rules_fingerprint(rules),
    )


def test_record_fingerprint_warns_when_worktree_config_cannot_be_enabled(tmp_path: Path) -> None:
    enable_key = (
        "git",
        "-C",
        str(tmp_path),
        "config",
        "extensions.worktreeConfig",
        "true",
    )
    runner = FakeCommandRunner({enable_key: CommandOutcome(returncode=1)})
    _reports, warnings, _report, warn = _report_warn()

    ok = init_rules.record_fingerprint(tmp_path, [{"run": "echo hi"}], runner=runner, warn=warn)

    assert ok is False
    assert any("could not record the provisioning rule set" in line for line in warnings)


def test_warn_drift_is_silent_when_unstamped_and_no_rules_configured(tmp_path: Path) -> None:
    runner = FakeCommandRunner()
    _reports, warnings, _report, warn = _report_warn()

    drifted = init_rules.warn_drift(
        tmp_path, [], runner=runner, warn=warn, reinit_hint="bh wt init"
    )

    assert drifted is False
    assert warnings == []


def test_warn_drift_fires_when_the_recorded_stamp_no_longer_matches(tmp_path: Path) -> None:
    rules = [{"run": "echo hi"}]
    get_key = (
        "git",
        "-C",
        str(tmp_path),
        "config",
        "--worktree",
        "--get",
        init_rules.INIT_RULES_CONFIG_KEY,
    )
    runner = FakeCommandRunner({get_key: CommandOutcome(returncode=0, stdout="v1:stale\n")})
    _reports, warnings, _report, warn = _report_warn()

    drifted = init_rules.warn_drift(
        tmp_path, rules, runner=runner, warn=warn, reinit_hint='bh wt init "x"'
    )

    assert drifted is True
    assert any("worktree init rules changed" in line for line in warnings)
    assert any('bh wt init "x"' in line for line in warnings)


def test_warn_drift_is_silent_when_the_recorded_stamp_matches(tmp_path: Path) -> None:
    rules = [{"run": "echo hi"}]
    current = init_rules.rules_fingerprint(rules)
    get_key = (
        "git",
        "-C",
        str(tmp_path),
        "config",
        "--worktree",
        "--get",
        init_rules.INIT_RULES_CONFIG_KEY,
    )
    runner = FakeCommandRunner({get_key: CommandOutcome(returncode=0, stdout=f"{current}\n")})
    _reports, warnings, _report, warn = _report_warn()

    drifted = init_rules.warn_drift(
        tmp_path, rules, runner=runner, warn=warn, reinit_hint="bh wt init"
    )

    assert drifted is False
    assert warnings == []
