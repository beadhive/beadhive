"""The proposed ``plugins.tracker-github`` subtree with its defaults (spike bh-rktrh section 3).

In the implementation this is a pydantic ``_Section`` model (``extra="forbid"``) next to
``HitchConfig`` in ``modules/config/contracts.py``; a plain dict is enough to test the
properties the spike asserts (every feature flag defaults off; no key can hold a secret or an
approval).
"""

from __future__ import annotations

import re
from collections.abc import Mapping

PROPOSED_DEFAULTS: dict = {
    "enabled": False,
    "repo": None,
    "base_url": None,
    "credential": {"sources": ["env", "gh"]},
    "read": {
        "issues": {"enabled": False},
        "comments": {"enabled": False},
        "include_closed": True,
        "page_size": None,
    },
    "http_cache": {"enabled": False},
    "retry": {"max_retries": 3, "max_single_wait_seconds": 60, "max_total_wait_seconds": 120},
    "write": {
        "comment": {"enabled": False},
        "labels": {"enabled": False, "owned_prefixes": []},
    },
}

#: A key whose NAME suggests a secret, a command or a pre-approval can never exist. The one
#: allowed exception is ``credential.sources`` (names of where to look, never a value).
FORBIDDEN_KEY = re.compile(
    r"(token|secret|passw|key|auth|header|command|argv|exec|grant|approv|yes|auto|bypass)",
    re.IGNORECASE,
)
ALLOWED_PATHS = frozenset({"credential", "credential.sources"})


def flatten(tree: Mapping, prefix: str = "") -> dict[str, object]:
    out: dict[str, object] = {}
    for key, value in tree.items():
        path = f"{prefix}{key}"
        if isinstance(value, Mapping):
            out.update(flatten(value, f"{path}."))
        else:
            out[path] = value
    return out


def all_paths(tree: Mapping, prefix: str = "") -> list[str]:
    paths: list[str] = []
    for key, value in tree.items():
        path = f"{prefix}{key}"
        paths.append(path)
        if isinstance(value, Mapping):
            paths.extend(all_paths(value, f"{path}."))
    return paths
