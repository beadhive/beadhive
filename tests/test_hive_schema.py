"""``hives/<provider>/<org>/<repo>.yaml`` — each hive's observed bd schema version, recorded in
HQ (`bh-wnly`).

Covers the acceptance bar directly:
  * AC1: a saved record round-trips and is readable from HQ alone (no store/hive touched to
    read it back — `load`/`try_load` take only `hq_dir` + the identity triplet).
  * AC2: `refresh` is the one place a record gets written, and it's probe-driven, not guessed.
  * AC4: `refresh` never persists a placeholder when the probe fails (the prior record, if any,
    survives untouched); `is_stale`/`age_seconds` let a reader tell "confirmed recently" from
    "confirmed a while ago, unverified since" instead of treating every record as equally fresh
    forever.
  * a malformed manifest fails loudly (mirrors `hosts.py`'s own "fails loudly" contract).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from beadhive import dolt_health, hive_schema


def _record(**overrides) -> hive_schema.HiveSchemaRecord:
    fields = {
        "provider": "github",
        "org": "acme",
        "repo": "zf",
        "schema_version": 59,
        "dolt_mode": "embedded",
        "observed_at": "2026-08-01T00:00:00Z",
        "observed_by_host": "host-1",
        "observed_by_bd_version": "bd version HEAD-af076b6",
    }
    fields.update(overrides)
    return hive_schema.HiveSchemaRecord(**fields)


# ---- schema shape ----------------------------------------------------------------------


def test_record_requires_the_documented_fields():
    with pytest.raises(ValidationError):
        hive_schema.HiveSchemaRecord(provider="github")


def test_record_rejects_unknown_top_level_key():
    with pytest.raises(ValidationError):
        _record(bogus="nope")


# ---- round trip (AC1: discoverable from HQ alone) ---------------------------------------


def test_record_round_trips_through_save_and_load(tmp_path):
    hq_dir = tmp_path / "hq"
    record = _record()

    written = hive_schema.save(hq_dir, record)

    assert written == hive_schema.manifest_path(hq_dir, "github", "acme", "zf")
    loaded = hive_schema.load(hq_dir, "github", "acme", "zf")
    assert loaded == record


def test_manifest_path_is_the_identity_triplet_as_a_real_subpath(tmp_path):
    hq_dir = tmp_path / "hq"
    assert hive_schema.manifest_path(hq_dir, "github", "acme", "zf") == (
        hq_dir / "hives" / "github" / "acme" / "zf.yaml"
    )


def test_load_raises_file_not_found_when_no_record_exists(tmp_path):
    with pytest.raises(FileNotFoundError):
        hive_schema.load(tmp_path / "hq", "github", "acme", "zf")


def test_try_load_returns_none_instead_of_raising(tmp_path):
    assert hive_schema.try_load(tmp_path / "hq", "github", "acme", "zf") is None


def test_malformed_manifest_fails_loudly_naming_the_key(tmp_path):
    hq_dir = tmp_path / "hq"
    path = hive_schema.manifest_path(hq_dir, "github", "acme", "zf")
    path.parent.mkdir(parents=True)
    path.write_text("schema_version: not-an-int\n")

    with pytest.raises(hive_schema.ManifestError) as exc:
        hive_schema.load(hq_dir, "github", "acme", "zf")
    assert "schema_version" in str(exc.value)

    with pytest.raises(hive_schema.ManifestError):
        # malformed still raises loudly, unlike a merely-absent record (try_load's other case).
        hive_schema.try_load(hq_dir, "github", "acme", "zf")


# ---- staleness (AC4) ---------------------------------------------------------------------


def test_age_seconds_measures_from_observed_at():
    record = _record(observed_at="2026-08-01T00:00:00Z")
    now = datetime(2026, 8, 3, 0, 0, 0, tzinfo=UTC)
    assert hive_schema.age_seconds(record, now=now) == pytest.approx(2 * 86400)


def test_age_seconds_infinite_on_unparseable_timestamp():
    record = _record(observed_at="not-a-timestamp")
    assert hive_schema.age_seconds(record) == float("inf")


def test_is_stale_true_for_no_record_at_all():
    assert hive_schema.is_stale(None) is True


def test_is_stale_false_within_the_default_bound():
    now = datetime.now(UTC)
    fresh = _record(observed_at=(now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    assert hive_schema.is_stale(fresh) is False


def test_is_stale_true_beyond_the_default_bound():
    now = datetime.now(UTC)
    old = _record(observed_at=(now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    assert hive_schema.is_stale(old) is True


def test_is_stale_respects_an_explicit_max_age():
    now = datetime.now(UTC)
    record = _record(observed_at=(now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    assert hive_schema.is_stale(record, max_age=3600) is True
    assert hive_schema.is_stale(record, max_age=3 * 3600) is False


# ---- refresh: the one write path (AC2), never a placeholder on failure (AC4) -------------


def test_refresh_persists_a_probed_version(tmp_path, monkeypatch):
    hq_dir = tmp_path / "hq"
    hive_dir = tmp_path / "hive"
    hive_dir.mkdir()

    monkeypatch.setattr(
        dolt_health,
        "probe_raw_schema_version",
        lambda *a, **k: dolt_health.SchemaProbeResult(59, "probed"),
    )
    monkeypatch.setattr(dolt_health, "_local_bd_version_string", lambda **k: "bd version 9.9.9")

    record = hive_schema.refresh(
        hive_dir, "github", "acme", "zf", hq_dir=hq_dir, dolt_mode="embedded"
    )

    assert record is not None
    assert record.schema_version == 59
    assert record.dolt_mode == "embedded"
    assert record.observed_by_bd_version == "bd version 9.9.9"
    # And it's genuinely persisted — a fresh read agrees.
    assert hive_schema.load(hq_dir, "github", "acme", "zf") == record


def test_refresh_never_writes_a_placeholder_on_probe_failure(tmp_path, monkeypatch):
    hq_dir = tmp_path / "hq"
    hive_dir = tmp_path / "hive"
    hive_dir.mkdir()

    monkeypatch.setattr(
        dolt_health,
        "probe_raw_schema_version",
        lambda *a, **k: dolt_health.SchemaProbeResult(None, "dolt not on PATH"),
    )

    result = hive_schema.refresh(
        hive_dir, "github", "acme", "zf", hq_dir=hq_dir, dolt_mode="embedded"
    )

    assert result is None
    assert hive_schema.try_load(hq_dir, "github", "acme", "zf") is None


def test_refresh_leaves_a_prior_record_untouched_when_a_later_probe_fails(tmp_path, monkeypatch):
    """The core AC4 behavior: a refresh that can't confirm the current state must not clobber
    the last CONFIRMED observation — the caller (doctor/hive_ready) reads that prior record back
    via `try_load` and marks it stale via `is_stale`, rather than losing it outright."""
    hq_dir = tmp_path / "hq"
    hive_dir = tmp_path / "hive"
    hive_dir.mkdir()

    monkeypatch.setattr(
        dolt_health,
        "probe_raw_schema_version",
        lambda *a, **k: dolt_health.SchemaProbeResult(59, "probed"),
    )
    monkeypatch.setattr(dolt_health, "_local_bd_version_string", lambda **k: "bd version 9.9.9")
    first = hive_schema.refresh(
        hive_dir, "github", "acme", "zf", hq_dir=hq_dir, dolt_mode="embedded"
    )
    assert first is not None

    monkeypatch.setattr(
        dolt_health,
        "probe_raw_schema_version",
        lambda *a, **k: dolt_health.SchemaProbeResult(None, "transient failure"),
    )
    second = hive_schema.refresh(
        hive_dir, "github", "acme", "zf", hq_dir=hq_dir, dolt_mode="embedded"
    )

    assert second is None
    assert hive_schema.try_load(hq_dir, "github", "acme", "zf") == first
