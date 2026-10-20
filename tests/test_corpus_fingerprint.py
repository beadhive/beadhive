"""Corpus fingerprint primitive (bh-ksmt0.2). Fake readers only; no store, no server."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from beadhive import corpus_fingerprint as cf


def facts(**over):
    base = dict(
        project_id="p1",
        prefix="bh",
        schema_version=59,
        main_head="h1",
        branches={"main": "h1", "side": "h2"},
        issue_rows={"bh-1": {"id": "bh-1", "t": "a"}, "bh-2": {"id": "bh-2", "t": "b"}},
        tables={"labels": [{"i": "bh-1", "l": "x"}], "comments": None},
    )
    base.update(over)
    return cf.CorpusFacts(**base)


TABLES = ("labels", "comments")


def fp(tier=cf.TIER_STRONG, **over):
    return cf.compute(facts(**over), tier, tables=TABLES)


def test_equal_facts_equal_digest_and_roundtrip():
    a, b = fp(), fp()
    assert a.digest == b.digest and cf.compare(a, b).equal
    assert cf.Fingerprint.from_dict(a.to_dict()) == a
    assert a.to_dict()["schema"] == "bh.corpus-fingerprint/1"


def test_row_order_is_irrelevant_and_content_matters():
    swapped = {"bh-2": {"id": "bh-2", "t": "b"}, "bh-1": {"id": "bh-1", "t": "a"}}
    assert fp(issue_rows=swapped).digest == fp().digest
    changed = {"bh-1": {"id": "bh-1", "t": "CHANGED"}, "bh-2": {"id": "bh-2", "t": "b"}}
    diff = cf.compare(fp(), fp(issue_rows=changed))
    assert not diff.equal and diff.differing == ("issue_rows_digest",)


def test_mismatch_names_the_component():
    diff = cf.compare(fp(), fp(main_head="zzz", tables={"labels": [], "comments": None}))
    assert set(diff.differing) == {"main_head", "tables.labels.rows", "tables.labels.digest"}
    assert diff.detail["main_head"] == ("h1", "zzz")


def test_lost_branch_detected_in_strong_but_not_corpus():
    lost = {"main": "h1"}
    assert not cf.compare(fp(), fp(branches=lost)).equal
    assert cf.compare(fp(cf.TIER_CORPUS), fp(cf.TIER_CORPUS, branches=lost, main_head="x")).equal


def test_corpus_tier_omits_commit_hashes():
    comps = fp(cf.TIER_CORPUS).components
    assert "main_head" not in comps and "branches" not in comps


def test_cross_tier_comparison_refused():
    with pytest.raises(cf.FingerprintTierMismatch):
        cf.compare(fp(cf.TIER_STRONG), fp(cf.TIER_CORPUS))


def test_unknown_schema_version_refuses_to_fingerprint():
    with pytest.raises(cf.FingerprintIncomplete, match="schema_version"):
        fp(schema_version=None)


def test_strong_without_head_refused():
    with pytest.raises(cf.FingerprintIncomplete):
        fp(main_head="")


def test_missing_table_is_recorded_not_silent_and_exclusions_named():
    f = fp()
    assert f.components["tables"]["comments"] == {"present": False}
    assert "dolt-transport-cache" in f.excluded


def test_tampered_document_refused():
    doc = fp().to_dict()
    doc["components"]["issue_count"] = 99
    with pytest.raises(cf.FingerprintError, match="digest"):
        cf.Fingerprint.from_dict(doc)


def test_configured_tables_extends_defaults_off_by_default():
    assert cf.configured_tables(None) == cf.DEFAULT_TABLES
    cfg = {"migration": {"fingerprint": {"tables": ["wisps", "labels"]}}}
    assert cf.configured_tables(cfg) == (*cf.DEFAULT_TABLES, "wisps")


def test_bd_sql_reader_collects_facts(monkeypatch, tmp_path):
    answers = {
        "HASHOF": [{"h": "h1"}],
        "dolt_branches": [{"name": "main", "hash": "h1"}],
        "information_schema": [{"table_name": "labels"}],
        "FROM issues": [{"id": "bh-1"}],
        "issue_prefix": [{"value": "bh"}],
        "FROM `labels`": [{"l": "x"}],
    }

    def sql(_dir, query):
        return next(v for k, v in answers.items() if k in query)

    monkeypatch.setattr(cf.store_locator, "dolt_mode", lambda d: "server")
    monkeypatch.setattr(cf.store_locator, "project_id", lambda d: "p1")
    probe = lambda d, dolt_mode: SimpleNamespace(version=59)  # noqa: E731
    got = cf.BdSqlCorpusReader(sql, probe=probe).read(tmp_path, ("labels", "comments"))
    assert got.schema_version == 59 and got.prefix == "bh" and got.project_id == "p1"
    assert got.tables == {"labels": [{"l": "x"}], "comments": None}
    assert (
        cf.fingerprint(
            cf.BdSqlCorpusReader(sql, probe=probe), tmp_path, "strong", tables=("labels",)
        ).tier
        == "strong"
    )


def test_bd_sql_reader_rejects_bad_table_name(tmp_path):
    reader = cf.BdSqlCorpusReader(
        lambda d, q: [], probe=lambda d, dolt_mode: SimpleNamespace(version=1)
    )
    with pytest.raises(cf.FingerprintError):
        reader.read(tmp_path, ("x; drop table issues",))
