"""Typer projection for the package-owned Pants validation tools."""

from __future__ import annotations

import typer

from . import attest, cache, runner

app = typer.Typer(no_args_is_help=True, help="Pants build and test validation.")
test_app = typer.Typer(no_args_is_help=True, help="Run the proven Pants/native partition.")
app.add_typer(test_app, name="test")
_EXTRA_ARGS = typer.Argument(None, help="Arguments forwarded to the underlying runner.")


def _exit(status: int) -> None:
    if status:
        raise typer.Exit(status)


@test_app.command("affected")
def test_affected(base: str) -> None:
    """Run the affected proven tests and the required native residual."""

    _exit(runner.main(["affected", base]))


@test_app.command("all")
def test_all() -> None:
    """Run the complete proven Pants partition."""

    _exit(runner.main(["all"]))


@app.command("native", context_settings={"ignore_unknown_options": True})
def native(pytest_args: list[str] | None = _EXTRA_ARGS) -> None:
    """Run the native residual, forwarding extra pytest arguments."""

    _exit(runner.main(["native", "--", *(pytest_args or [])]))


@app.command("cache", context_settings={"ignore_unknown_options": True})
def cache_command(args: list[str] | None = _EXTRA_ARGS) -> None:
    """Inspect or maintain the bounded Pants cache."""

    _exit(cache.main(args or ["status"]))


@app.command("attest-check")
def attest_check() -> None:
    """Run the exact-tree Pants attestation prerequisite."""

    _exit(attest.main())
