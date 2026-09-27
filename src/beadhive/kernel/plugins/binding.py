"""Forwarding facade — moved to the ``beadhive-plugins`` library package (bh-xh8ku.2).

Typed capability binding now lives in :mod:`beadhive_plugins.binding`. This module re-exports
the identical object at the old import path so existing consumers keep working unchanged.
"""

from __future__ import annotations

from beadhive_plugins.binding import bind_application_port

__all__ = ["bind_application_port"]
