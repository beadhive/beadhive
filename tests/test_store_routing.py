"""Operational workflows route exclusively to the internal store (bh-lniwf.5).

docs/design/beads-methodology-divergence-adr.md (the subject-and-visibility table) and
docs/design/two-store-scope-adr.md section 5: internal is the implicit operational target, a
public projection id is refused by every operational verb before any store is read, and bead
discovery never lets a public record shadow an internal bead.
"""

from __future__ import annotations

import pytest
import typer

from beadhive import (
    config,
    registry,
    store_target,
    work,
    work_lifecycle,
    work_review,
)

TWO_STORE = """\
providers: [github]
managed_repos:
  - {provider: github, org: acme, repo: widget, prefix: wid,
     stores: {internal: {remote: widget-private},
              public: {prefix: widpub, path: /srv/widget-public}}}
  - {provider: github, org: acme, repo: plain, prefix: pl}
"""

LEGACY = """\
providers: [github]
managed_repos:
  - {provider: github, org: acme, repo: widget, prefix: wid}
"""


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    def _write(text):
        path = tmp_path / "config.yaml"
        path.write_text(text)
        monkeypatch.setenv("BH_CONFIG", str(path))
        monkeypatch.setenv("GIT_WORKSPACE", str(tmp_path / "ws"))
        monkeypatch.setenv("BH_HOME", str(tmp_path / "bhhome"))
        return config.load()

    return _write


def _explode(*_a, **_k):
    raise AssertionError("an operational path ran for a public projection id")


def test_public_id_is_never_discovered_in_an_internal_store(fleet, monkeypatch):
    cfg = fleet(TWO_STORE)
    monkeypatch.setattr(registry, "_bead_exists", _explode)  # no store read at all
    resolution = registry.resolve_bead_hive(cfg, "widpub-7k2")
    assert not resolution.found and resolution.candidates == ()
    explicit = registry.resolve_bead_hive(cfg, "widpub-7k2", "acme/widget")
    assert not explicit.found


def test_internal_ids_still_resolve_on_a_partitioned_hive(fleet, monkeypatch):
    cfg = fleet(TWO_STORE)
    seen = []
    monkeypatch.setattr(registry, "_bead_exists", lambda e, b: seen.append(e["repo"]) or True)
    resolution = registry.resolve_bead_hive(cfg, "wid-7k2")
    assert resolution.found and str(resolution.entry["repo"]) == "widget"
    assert seen == ["widget"]


def test_operational_store_of_a_partitioned_hive_is_internal(fleet):
    cfg = fleet(TWO_STORE)
    entry = registry.find_entry(cfg, "github", "acme", "widget")
    target = store_target.operational_store(entry)
    assert target.scope == "internal" and target.operational
    assert target.beads_dir.name == ".beads" and not target.detached


@pytest.mark.parametrize(
    ("verb", "call", "seam"),
    [
        ("assign", lambda b: work.assign(b, "dev/x", "", "", False, False), "assign"),
        ("claim", lambda b: work.claim(b, "", "", "", "", False, False), "claim"),
        ("claim", lambda b: work.claim("", "", f"wid-1,{b}", "", "", False, False), "claim"),
        ("resume", lambda b: work.resume(b, "", ""), "resume"),
    ],
)
def test_lifecycle_verbs_refuse_public_ids_before_running(
    fleet, monkeypatch, capsys, verb, call, seam
):
    fleet(TWO_STORE)
    monkeypatch.setattr(work_lifecycle, seam, _explode)
    with pytest.raises(typer.Exit):
        call("widpub-7k2")
    err = capsys.readouterr().err
    assert f"`bh work {verb}` refuses widpub-7k2" in err and "public store" in err


def test_submit_merge_review_and_start_refuse_public_ids(fleet, monkeypatch):
    fleet(TWO_STORE)
    from beadhive import hq_authority_expiry

    # The guard runs first inside submit/merge; anything after it explodes.
    monkeypatch.setattr(hq_authority_expiry, "warn_if_expiring", _explode)
    monkeypatch.setattr(work_review, "approve", _explode)
    monkeypatch.setattr(work_review, "bounce", _explode)
    for call in (
        lambda: work.submit("widpub-1", "", "", "", "", ""),
        lambda: work.merge("widpub-1", "", None, False, "", "", ""),
        lambda: work.approve("widpub-1", "", ""),
        lambda: work.bounce("widpub-1", "msg", "", ""),
        lambda: work.start("widpub-1", "", ""),
    ):
        with pytest.raises(typer.Exit):
            call()


def test_legacy_fleet_is_untouched_by_the_guard(fleet, monkeypatch):
    fleet(LEGACY)
    ran = []
    monkeypatch.setattr(work_lifecycle, "claim", lambda *a: ran.append(a))
    work.claim("widpub-7k2", "", "", "", "", False, False)  # no public store: plain id
    assert ran
    assert store_target.bead_scope(config.load(), "widpub-7k2") is None
