"""``~/.beadhive/host.yaml`` — this machine's stable identity, distinct from configuration.

Minted once by ``bh config init``, never regenerated after that, and never synced or
templated — see ``docs/design/multi-host-model-adr.md`` ("host id"). Sibling to
``config.yaml`` under :func:`beadhive.config.home`; NOT inside a hive's ``.beads/``, and not
one of the files ``hq.scaffold_layout`` writes into the HQ store that gets pushed to a
remote (that store's contents are enumerated there — ``fleet.yaml`` / ``workspace.toml`` /
``hosts/README.md`` — and this file is deliberately absent from the list; see
``tests/test_host.py``'s sync-exclusion tests for the enforcement).

Shape: ``{host_id: <uuid4 str>, label: <human string, hostname-derived default>}``.
``host_id`` is the fencing/identity value the rest of this molecule (``guard_primary``, the
lease/epoch ref, the per-host HQ manifest at ``hosts/<host_id>.yaml``) keys off; ``label`` is
cosmetic only, freely editable, and never consulted for identity — a machine rename never
changes ``host_id``.
"""

from __future__ import annotations

import socket
import uuid
from pathlib import Path

from ruamel.yaml import YAML

from . import config

# Same round-trip settings as config.py's writer — plain 2-key file, but consistent style.
_yaml = YAML()
_yaml.indent(mapping=2, sequence=4, offset=2)


def path() -> Path:
    """Where the host identity file lives: sibling to ``config.yaml`` under
    :func:`beadhive.config.home` — never inside a hive's ``.beads/`` and never under
    :func:`beadhive.config.hq_dir` (see module docstring)."""
    return config.home() / "host.yaml"


def _default_label() -> str:
    return socket.gethostname()


def mint_if_needed() -> bool:
    """Mint ``host.yaml`` with a fresh UUID ``host_id`` + hostname-derived ``label``, if and
    only if it doesn't already exist. Returns ``True`` when it minted, ``False`` when an
    existing file was left completely untouched — the one-time, never-regenerate contract
    ``bh config init`` relies on. Deliberately takes no ``force`` parameter: unlike the other
    templated files ``config init`` scaffolds, host identity is never rewritten, not even by
    ``--force`` — see the module docstring."""
    p = path()
    if p.exists():
        return False
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {"host_id": str(uuid.uuid4()), "label": _default_label()}
    with p.open("w") as f:
        _yaml.dump(data, f)
    return True


def load() -> dict:
    """The host identity exactly as written. Raises ``FileNotFoundError`` (with ``config
    init`` guidance, matching :func:`beadhive.config.load_host`'s message shape) when
    ``host.yaml`` hasn't been minted yet."""
    p = path()
    if not p.exists():
        raise FileNotFoundError(
            f"{config.BINARY_ALIAS} host identity not found at {p}\n"
            f"  scaffold it with:  {config.BINARY_ALIAS} config init"
        )
    return _yaml.load(p.read_text())


def host_id() -> str:
    return str(load()["host_id"])


def label() -> str:
    return str(load()["label"])
