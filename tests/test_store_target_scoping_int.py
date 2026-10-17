"""Integration: a detached store's named routes read THAT store, never the checkout's (bh-lniwf.6).

Measured against bd 1.3.0: ``bd -C <checkout>`` re-discovers the checkout's own store and ignores
``BEADS_DIR``, so a public route scoped to the code checkout would silently read and write the
INTERNAL store. Real `bd` (embedded), real Git, no network: one bead in each store, then the
public routes must see only the public bead and the internal routes only the internal one.

Marked `integration`; self-skips without `bd` on PATH.
"""

from __future__ import annotations

import os

import pytest

from beadhive import store_target
from harness.beads import create, embedded_env, init_embedded, skip_if_no_bd
from harness.world import git

pytestmark = [pytest.mark.integration, skip_if_no_bd]


@pytest.fixture
def two_stores(tmp_path, monkeypatch):
    for key in set(os.environ) - set(embedded_env()):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("BEADS_DIR", raising=False)
    root = tmp_path / "ws"
    checkout = root / "github" / "acme" / "widget"
    checkout.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=checkout)
    monkeypatch.setenv("GIT_WORKSPACE", str(root))
    public_root = tmp_path / "stores" / "widget-public"
    public_root.mkdir(parents=True)
    init_embedded(checkout, "wid")
    init_embedded(public_root, "widpub")
    entry = {
        "provider": "github",
        "org": "acme",
        "repo": "widget",
        "prefix": "wid",
        "stores": {
            "internal": {"remote": "widget-private"},
            "public": {
                "prefix": "widpub",
                "path": str(public_root),
                "remote": "git-origin",
                "source": "github-issues",
            },
        },
    }
    internal_id = create(checkout, "internal plan")
    public_id = create(public_root, "public issue")
    return entry, internal_id, public_id


def _ids(rows) -> set[str]:
    return {row["id"] for row in rows or []}


def test_public_routes_never_reach_the_checkout_store(two_stores):
    entry, internal_id, public_id = two_stores
    public = store_target.resolve_store(entry, "public")
    internal = store_target.resolve_store(entry)

    assert _ids(store_target.routes(public).issue_list(all_=True)) == {public_id}
    # even when a caller hands the code checkout as cwd
    assert _ids(store_target.routes(public, cwd=public.repo_dir).issue_list(all_=True)) == {
        public_id
    }
    assert _ids(store_target.routes(internal).issue_list(all_=True)) == {internal_id}
