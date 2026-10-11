"""Backend-neutral HQ storage port (scaffold, bh-wnhkn).

A **library package** (``docs/design/package-class-library-vs-plugin-adr.md``): stdlib only, and
it never imports ``beadhive``. The port protocols, value and outcome types, the in-memory fake
and the contract scenarios land in ``bh-9fg5k``; the Dolt adapter lives in
``beadhive-hq-storage-dolt`` (``bh-br1cd``). See ``docs/design/hq-storage-port-adr.md``.
"""

from __future__ import annotations

__all__: list[str] = []
