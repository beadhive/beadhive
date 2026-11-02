"""The XTDB BeadLog store plugin (``beadlog-xtdb``, capability ``beadlog.store`` v1).

Importing this package imports nothing heavy: the ``beadlog_xtdb`` library (and with it psycopg)
is imported only inside functions, when a store actually connects or doctor probes the node.
Root resolves the plugin lazily, by name, through :mod:`beadhive_beadlog_xtdb.factory`, and only
after the manifest was discovered, selected and enabled (``plugins.beadlog-xtdb.enabled``);
installing the package enables nothing. The plugin never starts, stops or upgrades XTDB: the
operator runs the node (``docs/spikes/bh-cntr0-xtdb-operational-fit.md``).
"""

from __future__ import annotations

#: The plugin id (``plugin.json`` ``plugin_id``; the ``plugins.<id>`` namespace).
PLUGIN_ID = "beadlog-xtdb"
#: The ``bh backfill --store`` name this plugin answers to.
STORE_NAME = "xtdb"

__all__ = ["PLUGIN_ID", "STORE_NAME"]
