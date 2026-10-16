"""Per-repository internal/public store descriptors in HQ config (bh-lniwf.2).

docs/design/two-store-scope-adr.md sections 1-4. A managed_repos entry without `stores` is
today's one-store hive, untouched; `stores.internal` / `stores.public` are validated per entry
and fleet-wide; registration carries fields it does not own.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from beadhive import config, registry
from beadhive.modules.config.contracts import BeadhiveConfig, ManagedRepoStores

PUBLIC = {
    "prefix": "bhpub",
    "path": "~/.beadhive/stores/github/beadhive/beadhive/public",
    "remote": "git-origin",
    "source": "github-issues",
}
INTERNAL = {"prefix": "bh", "remote": "beadhive-private"}


def _entry(repo="beadhive", prefix="bh", stores=None):
    entry = {"provider": "github", "org": "beadhive", "repo": repo, "prefix": prefix}
    if stores is not None:
        entry["stores"] = stores
    return entry


def _validate(*entries):
    return BeadhiveConfig(managed_repos=list(entries))


def _error(*entries) -> str:
    with pytest.raises(ValidationError) as excinfo:
        _validate(*entries)
    return str(excinfo.value)


def test_legacy_entry_without_stores_is_unchanged():
    cfg = _validate(_entry())
    assert cfg.managed_repos[0].stores is None


def test_two_store_entry_validates_with_effective_defaults():
    cfg = _validate(_entry(stores={"internal": INTERNAL, "public": PUBLIC}))
    stores = cfg.managed_repos[0].stores
    assert stores.internal.effective_authority("internal") == "local"
    assert stores.internal.effective_sync("internal") == "push-pull"
    assert stores.public.effective_authority("public") == "remote"
    assert stores.public.effective_sync("public") == "pull-only"


@pytest.mark.parametrize(
    ("stores", "needle"),
    [
        ({"public": PUBLIC}, "stores.internal is required"),
        ({"internal": INTERNAL, "public": {**PUBLIC, "prefix": None}}, "prefix is required"),
        ({"internal": INTERNAL, "public": {**PUBLIC, "path": None}}, "path is required"),
        ({"internal": INTERNAL, "public": {**PUBLIC, "dispatch": True}}, "never operational"),
        ({"internal": INTERNAL, "public": {**PUBLIC, "authority": "local"}}, "must be 'remote'"),
        ({"internal": INTERNAL, "public": {**PUBLIC, "sync": "push-pull"}}, "is invalid with"),
        ({"internal": {**INTERNAL, "authority": "remote"}}, "must be 'local'"),
        ({"internal": {**INTERNAL, "sync": "pull-only"}}, "is invalid with"),
        ({"internal": {**INTERNAL, "dispatch": False}}, "cannot be false"),
        ({"internal": {**INTERNAL, "path": "/srv/x"}}, "path is not supported"),
        ({"internal": {**INTERNAL, "source": "github-issues"}}, "only valid on a public"),
        (
            {
                "internal": {**INTERNAL, "remote": "git-origin"},
                "public": {**PUBLIC, "remote": "pub"},
            },
            "would publish private beads",
        ),
        (
            {"internal": INTERNAL, "public": {**PUBLIC, "remote": "beadhive-private"}},
            "different remotes",
        ),
    ],
)
def test_invalid_store_pairs_are_rejected(stores, needle):
    assert needle in _error(_entry(stores=stores))


@pytest.mark.parametrize(
    "remote",
    ["s3://bucket/path", "https://doltremoteapi.dolthub.com/x", "user@host", "host:path"],
)
def test_store_remote_names_a_descriptor_never_a_url_or_credential(remote):
    assert "HQ remote descriptor" in _error(_entry(stores={"internal": {"remote": remote}}))


def test_relative_public_path_is_rejected():
    stores = {"internal": INTERNAL, "public": {**PUBLIC, "path": "stores/public"}}
    assert "must be absolute" in _error(_entry(stores=stores))


def test_internal_prefix_must_equal_the_hive_prefix():
    stores = {"internal": {**INTERNAL, "prefix": "other"}}
    assert "must equal the hive prefix" in _error(_entry(stores=stores))


def test_public_prefix_is_unique_against_every_hive_and_store():
    a = _entry(stores={"internal": INTERNAL, "public": {**PUBLIC, "prefix": "wid"}})
    b = _entry(repo="widget", prefix="wid")
    assert "already used by beadhive/widget" in _error(a, b)

    b2 = _entry(
        repo="widget",
        prefix="wid2",
        stores={
            "internal": {"prefix": "wid2", "remote": "widget-private"},
            "public": {**PUBLIC, "path": "/srv/widget/public"},
        },
    )
    assert "prefix 'bhpub' is already used" in _error(
        _entry(stores={"internal": INTERNAL, "public": PUBLIC}), b2
    )


def test_detached_paths_and_remote_names_are_unique_fleet_wide():
    a = _entry(stores={"internal": INTERNAL, "public": PUBLIC})
    b = _entry(
        repo="widget",
        prefix="wid",
        stores={
            "internal": {"prefix": "wid", "remote": "beadhive-private"},
            "public": {**PUBLIC, "prefix": "widpub", "remote": "widget-pub"},
        },
    )
    message = _error(a, b)
    assert "path '~/.beadhive/stores/github/beadhive/beadhive/public' is already used" in message
    assert "remote 'beadhive-private' is already used" in message


def test_git_origin_may_be_named_by_several_hives_public_stores():
    a = _entry(stores={"internal": INTERNAL, "public": PUBLIC})
    b = _entry(
        repo="widget",
        prefix="wid",
        stores={
            "internal": {"remote": "widget-private"},
            "public": {**PUBLIC, "prefix": "widpub", "path": "/srv/widget/public"},
        },
    )
    _validate(a, b)


def test_existing_hive_prefix_collisions_stay_report_only():
    """Two legacy hives sharing a prefix were never a schema error (registry reports them);
    the store rules must not newly reject an untouched legacy fleet."""
    _validate(_entry(), _entry(repo="other"))


def test_stores_model_is_usable_standalone():
    ManagedRepoStores.model_validate({"internal": INTERNAL})


# ---- registration preserves fields it does not own -------------------------------------

FLOW_CONFIG = (
    "providers: [github]\n"
    "orgs:\n"
    "  acme: {code: ac, policy: personal}\n"
    "managed_repos:\n"
    '  - {"provider": "github", "org": "acme", "repo": "widget", "prefix": "wid", '
    '"kind": "personal", stores: {internal: {remote: widget-private}, public: {prefix: widpub, '
    "path: /srv/widget/public, source: github-issues}}}\n"
)


def test_register_carries_stores_and_unowned_fields_verbatim(world):
    config.config_path().write_text(FLOW_CONFIG)
    before = json.loads(json.dumps(config.load_host()["managed_repos"][0]["stores"]))

    registry.register("github", "acme", "widget", "wid", "prototype")

    entry = registry.find_entry(config.load(), "github", "acme", "widget")
    assert str(entry["kind"]) == "prototype"  # owned field rewritten
    assert json.loads(json.dumps(entry["stores"])) == before  # unowned field carried as-is
    text = config.config_path().read_text()
    line = next(line for line in text.splitlines() if "widget" in line)
    assert "stores: {internal: {remote: widget-private}" in line  # same flow-style entry line


def test_register_without_a_previous_entry_adds_no_stores(world):
    registry.register("github", "acme", "gadget", "gad", "personal")
    entry = registry.find_entry(config.load(), "github", "acme", "gadget")
    assert "stores" not in entry
