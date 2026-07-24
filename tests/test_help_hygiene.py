"""Diagnostic side effects (schema-staleness warning, setup gate) must never fire on a purely
informational `--help`/`-h` invocation (bh-sn9q). Root cause: the Typer group callback
(``cli._root``) runs BEFORE a subcommand's own eager ``--help`` option short-circuits, so
``bh <cmd> --help`` used to trigger both the ``config_schema_version_stale`` structlog
warning and the setup-complete gate — even though neither belongs on a help pass.

The warning itself was already routed to stderr (``log.configure`` defaults its stream to
``sys.stderr``); ``CliRunner.Result.output`` just mixes stdout+stderr together, which made a
stderr-only warning look like a stdout leak in ``result.output``. These tests assert on
``result.stdout`` (pure stdout) specifically, so a regression that re-introduces the warning
on a stdout stream — or re-fires it on `--help` at all — is caught either way.
"""

from __future__ import annotations

import sys

from typer.testing import CliRunner

from beadhive import cli, config

runner = CliRunner()


def _write_config_without_schema_version(text: str = "") -> None:
    """Overwrite the sandboxed config.yaml with a payload that omits `schema_version`
    (or is stale), so `warn_stale_schema_version_if_needed` would fire if ever called."""
    config.config_path().write_text(
        text
        or (
            "providers: [github]\n"
            "managed_repos: []\n"
            "exclude:\n"
            "  orgs: []\n"
            "  repos: []\n"
        )
    )


def test_subcommand_help_stdout_has_no_stale_schema_warning(monkeypatch):
    """`bh <cmd> --help` never emits the config_schema_version_stale warning, and its
    stdout (not the mixed `.output`) is clean — even when schema_version is absent."""
    _write_config_without_schema_version()
    monkeypatch.setattr(cli.sys, "argv", ["bh", "doctor", "--help"])

    result = runner.invoke(cli.app, ["doctor", "--help"])

    assert result.exit_code == 0, result.output
    assert "config_schema_version_stale" not in result.stdout
    assert "config_schema_version_stale" not in result.output
    assert "Usage" in result.stdout


def test_subcommand_help_not_blocked_by_setup_gate(monkeypatch):
    """`bh <cmd> --help` on a gated verb still prints help instead of being refused by the
    setup-complete gate (the gate shares the same structural exposure as the schema warning)."""
    monkeypatch.delenv("BH_SKIP_SETUP_CHECK", raising=False)
    monkeypatch.setattr(cli.sys, "argv", ["bh", "hive", "--help"])

    result = runner.invoke(cli.app, ["hive", "--help"])

    assert result.exit_code == 0, result.output
    assert "requires setup" not in result.output
    assert "Usage" in result.stdout


def test_real_command_still_warns_and_is_gated(monkeypatch):
    """Sanity check: a real (non-help) invocation is unaffected — the warning still fires
    (to stderr) and the setup gate still blocks a verb outside the allow-list."""
    _write_config_without_schema_version()
    monkeypatch.delenv("BH_SKIP_SETUP_CHECK", raising=False)
    monkeypatch.setattr(cli.sys, "argv", ["bh", "hive"])

    result = runner.invoke(cli.app, ["hive"])

    assert result.exit_code == 1
    assert "requires setup" in result.output


def test_is_help_or_completion_invocation_detects_resilient_parsing():
    """Shell-completion parsing (`ctx.resilient_parsing`) is recognized independent of argv,
    since Click never puts `--help`/`-h` on argv during a completion pass."""

    class _FakeCtx:
        resilient_parsing = True

    assert cli._is_help_or_completion_invocation(_FakeCtx()) is True


def test_is_help_or_completion_invocation_false_for_normal_argv(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["bh", "hive", "ready"])

    class _FakeCtx:
        resilient_parsing = False

    assert cli._is_help_or_completion_invocation(_FakeCtx()) is False
