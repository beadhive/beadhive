"""The drift guard (bh-05w7): the config schema must describe what bh's own writers emit.

`bh config validate` had drifted so far behind the code that it could not pass on any host
that had run `bh hq init` — `kind: hq`, `furnish`, and `contribution` were all real, honored,
load-bearing values the schema had never learned about. A validator that always fails is one
operators learn to ignore, so it stops catching the drift it exists to catch.

These tests close that loop by validating entries built through `registry._entry` — the actual
writer — rather than hand-written dicts. A hand-written fixture would drift alongside the
schema and keep passing; going through the writer means adding a field there without adding it
here fails CI.
"""

from __future__ import annotations

import pytest

from beadhive import registry
from beadhive.config_schema import ManagedRepoEntry, iter_schema_fields, known_keys

# Every kind bh itself writes, including the HQ singleton (registry.HQ_KIND), which
# `bh hq init` registers via registry.register.
ALL_KINDS = ("org-native", "personal", "prototype", "fork", "external", registry.HQ_KIND)


def _validate(entry) -> ManagedRepoEntry:
    return ManagedRepoEntry.model_validate(dict(entry))


# ---- every writer-produced entry validates ------------------------------------


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_every_kind_the_writer_emits_validates(kind):
    entry = registry._entry("github", "acme", "widgets", "acme-w", kind)
    assert _validate(entry).kind == kind


def test_hq_singleton_entry_validates():
    """The exact entry `bh hq init` registers (hq.py passes these registry constants)."""
    entry = registry._entry(
        registry.HQ_PROVIDER,
        registry.HQ_ORG,
        registry.HQ_REPO,
        registry.HQ_PREFIX,
        registry.HQ_KIND,
    )
    assert _validate(entry).kind == registry.HQ_KIND


@pytest.mark.parametrize("furnish", ("full", "none"))
def test_furnish_the_writer_emits_validates(furnish):
    entry = registry._entry("github", "acme", "widgets", "acme-w", "fork", furnish=furnish)
    assert _validate(entry).furnish == furnish


def test_contribution_the_writer_emits_validates():
    entry = registry._entry("github", "acme", "widgets", "acme-w", "external", contribution="pull")
    assert _validate(entry).contribution == "pull"


def test_a_fully_populated_writer_entry_validates():
    """Every optional the writer can emit, at once."""
    entry = registry._entry(
        "github",
        "acme",
        "widgets",
        "acme-w",
        "fork",
        upstream="upstream/widgets",
        furnish="none",
        contribution="pull",
    )
    model = _validate(entry)
    assert (model.upstream, model.furnish, model.contribution) == (
        "upstream/widgets",
        "none",
        "pull",
    )


# ---- the guard itself: no writer key may be unknown to the schema -------------


def test_no_key_the_writer_emits_is_unknown_to_the_schema():
    """THE regression test. `_Section` sets extra='forbid', so an emitted-but-undeclared key
    raises — this asserts it across every optional the writer can produce at once, which is
    what would have caught kind=hq, furnish, and contribution before they reached a host."""
    entry = registry._entry(
        "github",
        "acme",
        "widgets",
        "acme-w",
        registry.HQ_KIND,
        upstream="upstream/widgets",
        furnish="full",
        contribution="pull",
    )
    declared = set(ManagedRepoEntry.model_fields)
    assert set(dict(entry)) <= declared, (
        f"registry._entry emits keys the schema does not declare: "
        f"{sorted(set(dict(entry)) - declared)}"
    )


# ---- '' normalizes to unset, matching the runtime -----------------------------


@pytest.mark.parametrize("field", ("kind", "furnish", "contribution"))
def test_empty_string_normalizes_to_unset(field):
    """Every reader does str(entry.get(field, '')) and compares against known values, so ''
    already means "unset" at runtime. The schema agrees rather than widening the Literal."""
    model = ManagedRepoEntry.model_validate(
        {"provider": "github", "org": "a", "repo": "b", "prefix": "p", field: ""}
    )
    assert getattr(model, field) is None


def test_a_genuinely_bogus_kind_is_still_rejected():
    """Normalizing '' must not turn the Literal into a rubber stamp."""
    with pytest.raises(ValueError):
        ManagedRepoEntry.model_validate(
            {"provider": "github", "org": "a", "repo": "b", "prefix": "p", "kind": "nonsense"}
        )


# ---- collection members are described but not settable ------------------------


def test_collection_member_fields_are_listed_in_the_schema():
    paths = {f.path for f in iter_schema_fields()}
    assert "managed_repos[].furnish" in paths
    assert "managed_repos[].kind" in paths


def test_collection_member_fields_are_not_settable_keys():
    """`managed_repos.6.furnish` is dynamically keyed — describable, but not a key anyone can
    `bh config set`, so it must stay out of did-you-mean's universe."""
    assert not [k for k in known_keys() if "[]" in k]
