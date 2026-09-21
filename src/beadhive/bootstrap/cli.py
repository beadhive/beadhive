"""Installed CLI composition root."""

from __future__ import annotations


def main() -> None:
    """Enter the CLI through its import-lazy process boundary."""
    from ..cli_entrypoint import main as cli_main

    cli_main()
