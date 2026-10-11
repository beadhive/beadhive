"""Dolt adapter for the HQ storage port (scaffold, bh-wnhkn).

A **library package**: it imports ``beadhive_hq_storage`` only through its ``__all__``, never
imports ``beadhive``, and never imports PyMySQL -- root injects the connection factory. The
adapter itself lands in ``bh-br1cd``. See ``docs/design/hq-storage-port-adr.md``.
"""

from __future__ import annotations

__all__: list[str] = []
