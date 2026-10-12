"""Hypothesis profiles for the port's state machine (hq-storage-port-adr section 8).

``gate`` is the default, so every ``hq-storage-check`` run is derandomized and reproducible from
the tree alone; ``BH_HYPOTHESIS_PROFILE=nightly`` (the scheduled lane) or ``=dev`` (local
exploration) select the others.
"""

from __future__ import annotations

from beadhive_hq_storage.contract import load_hypothesis_profile

load_hypothesis_profile()
