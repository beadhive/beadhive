"""The bead tracker adapter port: a tracker-neutral contract for reading and writing a tracker.

``contracts`` is the port (records, errors and Protocols, standard library only, API version 1)
and ``application`` holds tracker-neutral helpers over it (paging and full id snapshots). The
module has no domain layer and no state, and it imports nothing from any other module: it is a
leaf (``scripts/check_import_boundaries.py``, ``_LEAF_MODULES``). Concrete adapters live in plugin
packages; the in-memory substitute and the conformance cases live in
:mod:`beadhive.testing.tracker`. Decision: ``docs/design/tracker-adapter-port-adr.md``.
"""
