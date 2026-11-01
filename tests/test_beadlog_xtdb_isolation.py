"""The XTDB client stays out of root: ``import beadhive`` must never import psycopg.

``beadlog-xtdb`` is a dev/CI workspace group only (docs/design/beadlog-format-adr.md amendment A6);
the opt-in plugin that binds it resolves it lazily, so a default install pays nothing for it.
"""

from __future__ import annotations

import subprocess
import sys


def test_importing_beadhive_does_not_import_psycopg() -> None:
    code = (
        "import sys, beadhive\n"
        "banned = {'psycopg', 'beadlog_xtdb'}\n"
        "loaded = sorted(m for m in sys.modules if m.split('.')[0] in banned)\n"
        "sys.exit(0 if not loaded else 'loaded: ' + ', '.join(loaded))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
