"""The wiring-free ``TrackerAdapter`` implementation for GitHub (lands with the read path).

Rules (tested package-locally): this is the only place ``githubkit`` may be imported, and only
inside functions, never at module top level; it reads no configuration and imports no plugin
kernel; from the plugin layer it imports only ``beadhive_plugins.credentials``.
"""

from __future__ import annotations

__all__: list[str] = []
