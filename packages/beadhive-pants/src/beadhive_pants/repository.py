"""Repository discovery shared by the installed Pants tooling commands."""

from __future__ import annotations

from pathlib import Path


def find_repository(start: Path | None = None) -> Path:
    """Return the nearest enclosing Beadhive checkout.

    The former ``scripts/`` implementations could derive the checkout from ``__file__``.
    Installed plugin modules instead resolve the repository containing the caller's working
    directory, which also makes the CLI usable from a checkout subdirectory.
    """

    candidate = (start or Path.cwd()).resolve()
    for root in (candidate, *candidate.parents):
        if (root / "pants.toml").is_file() and (root / "pyproject.toml").is_file():
            return root
    # Legacy path-loading tests and downstream script consumers may assemble only the files a
    # tool reads. Preserve that standalone behavior by treating the caller's directory as the
    # repository; the command itself will report any genuinely required missing input.
    return candidate
