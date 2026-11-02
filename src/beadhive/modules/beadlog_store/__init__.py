"""The ``beadlog.store`` capability: how ``bh backfill`` reaches a BeadLog store other than bd.

``contracts`` is the whole module: the capability id and API version, the store and loader
Protocols a provider returns, the snapshot record and the typed refusal (standard library only,
API version 1). It has no domain, application or state, and it imports nothing from any other
module. Root composes a provider lazily, after manifest selection
(:mod:`beadhive.bootstrap.beadlog_store`); the only provider today is the opt-in
``beadhive-beadlog-xtdb`` plugin package. Decision: ``docs/design/beadlog-format-adr.md``
amendment A6.
"""
