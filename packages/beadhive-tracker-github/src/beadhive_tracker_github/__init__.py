"""The GitHub tracker adapter plugin (``tracker-github``, capability ``tracker.adapter`` v1).

Importing this package imports nothing heavy: ``githubkit`` is imported only lazily, only inside
functions under :mod:`beadhive_tracker_github.adapter`. Root resolves the plugin lazily, by name,
through :mod:`beadhive_tracker_github.factory`, and only after the manifest was discovered,
selected and enabled; installing the package enables nothing.
"""

from __future__ import annotations

#: The plugin id (``plugin.json`` ``plugin_id``; the ``plugins.<id>`` namespace).
PLUGIN_ID = "tracker-github"

__all__ = ["PLUGIN_ID"]
