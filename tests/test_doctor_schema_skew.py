"""`doctor._bd_schema_skew_warnings` — `bh doctor` as the schema-version refresh trigger
(`bh-wnly`, AC2 + AC3).

All probing is faked (`dolt_health.probe_raw_schema_version` / `_local_bd_version_string`) so
this suite needs no real `bd`/`dolt` binary — the real-binary trap-avoidance proof lives in
`tests/test_dolt_health_int.py`; this file covers the WIRING: that `bh doctor` actually
refreshes+persists a hive's record and turns a real skew into a flat warning line, and that it
costs nothing (no probing at all) when there's no HQ to read/write against.
"""

from __future__ import annotations

from beadhive import doctor, dolt_health, hive_schema


def _entry(**overrides) -> dict:
    fields = {"provider": "github", "org": "acme", "repo": "zf", "prefix": "zf", "kind": "personal"}
    fields.update(overrides)
    return fields


def _checkout(tmp_path) -> None:
    path = tmp_path / "github" / "acme" / "zf"
    (path / ".beads").mkdir(parents=True)


def _hq(tmp_path):
    hq_dir = tmp_path / "hq"
    (hq_dir / ".beads").mkdir(parents=True)
    return hq_dir


def test_no_probing_at_all_without_an_hq(tmp_path, monkeypatch):
    """The guard this bead's own doctor.py docstring names: no HQ means nowhere to persist or
    compare, so this must be a genuine no-op — not just a silent empty result."""
    _checkout(tmp_path)
    monkeypatch.setattr(
        dolt_health,
        "local_bd_schema_version",
        lambda **k: (_ for _ in ()).throw(AssertionError("must not probe without an HQ")),
    )
    warns = doctor._bd_schema_skew_warnings({}, [_entry()], tmp_path)
    assert warns == []


def test_silent_when_local_bd_cannot_be_determined(tmp_path, monkeypatch):
    hq_dir = _hq(tmp_path)
    _checkout(tmp_path)
    monkeypatch.setattr(doctor.config, "hq_dir", lambda: hq_dir)
    monkeypatch.setattr(
        dolt_health, "local_bd_schema_version", lambda **k: dolt_health.SchemaProbeResult(None, "x")
    )
    warns = doctor._bd_schema_skew_warnings({}, [_entry()], tmp_path)
    assert warns == []


def test_refreshes_and_warns_on_real_skew(tmp_path, monkeypatch):
    hq_dir = _hq(tmp_path)
    _checkout(tmp_path)
    monkeypatch.setattr(doctor.config, "hq_dir", lambda: hq_dir)
    monkeypatch.setattr(doctor.safety, "_bd_dolt_mode", lambda path: "embedded")
    monkeypatch.setattr(
        dolt_health,
        "local_bd_schema_version",
        lambda **k: dolt_health.SchemaProbeResult(53, "cached"),
    )
    monkeypatch.setattr(
        dolt_health,
        "probe_raw_schema_version",
        lambda *a, **k: dolt_health.SchemaProbeResult(59, "probed"),
    )
    monkeypatch.setattr(dolt_health, "_local_bd_version_string", lambda **k: "bd version 1.1.2")

    warns = doctor._bd_schema_skew_warnings({}, [_entry()], tmp_path)

    assert len(warns) == 1
    assert "v53" in warns[0]
    assert "v59" in warns[0]
    assert "zf" in warns[0]
    # And doctor's refresh actually persisted it — a later, independent read agrees.
    record = hive_schema.load(hq_dir, "github", "acme", "zf")
    assert record.schema_version == 59
    assert record.dolt_mode == "embedded"


def test_silent_when_no_skew(tmp_path, monkeypatch):
    hq_dir = _hq(tmp_path)
    _checkout(tmp_path)
    monkeypatch.setattr(doctor.config, "hq_dir", lambda: hq_dir)
    monkeypatch.setattr(doctor.safety, "_bd_dolt_mode", lambda path: "embedded")
    monkeypatch.setattr(
        dolt_health,
        "local_bd_schema_version",
        lambda **k: dolt_health.SchemaProbeResult(59, "cached"),
    )
    monkeypatch.setattr(
        dolt_health,
        "probe_raw_schema_version",
        lambda *a, **k: dolt_health.SchemaProbeResult(53, "probed"),
    )
    monkeypatch.setattr(dolt_health, "_local_bd_version_string", lambda **k: "bd version HEAD")

    warns = doctor._bd_schema_skew_warnings({}, [_entry()], tmp_path)
    assert warns == []


def test_skips_hives_with_no_local_checkout(tmp_path, monkeypatch):
    hq_dir = _hq(tmp_path)
    monkeypatch.setattr(doctor.config, "hq_dir", lambda: hq_dir)
    monkeypatch.setattr(
        dolt_health,
        "local_bd_schema_version",
        lambda **k: dolt_health.SchemaProbeResult(53, "cached"),
    )
    probed = {"called": False}

    def fail_if_called(*a, **k):
        probed["called"] = True
        return dolt_health.SchemaProbeResult(59, "probed")

    monkeypatch.setattr(dolt_health, "probe_raw_schema_version", fail_if_called)

    warns = doctor._bd_schema_skew_warnings({}, [_entry()], tmp_path)  # no checkout under tmp_path
    assert warns == []
    assert probed["called"] is False


def test_stale_prior_record_survives_a_failed_reprobe_and_is_flagged(tmp_path, monkeypatch):
    """AC4: a refresh that fails this run must not silently drop or hide a PRIOR confirmed
    skew — it still reads back and reports the last known-true observation, marked stale."""
    hq_dir = _hq(tmp_path)
    _checkout(tmp_path)
    monkeypatch.setattr(doctor.config, "hq_dir", lambda: hq_dir)
    monkeypatch.setattr(doctor.safety, "_bd_dolt_mode", lambda path: "embedded")
    monkeypatch.setattr(
        dolt_health,
        "local_bd_schema_version",
        lambda **k: dolt_health.SchemaProbeResult(53, "cached"),
    )
    monkeypatch.setattr(dolt_health, "_local_bd_version_string", lambda **k: "bd version 1.1.2")

    # A prior, real, VERY OLD confirmed observation — old enough to be past hive_schema's
    # default staleness bound.
    stale_record = hive_schema.HiveSchemaRecord(
        provider="github",
        org="acme",
        repo="zf",
        schema_version=59,
        dolt_mode="embedded",
        observed_at="2000-01-01T00:00:00Z",
        observed_by_host="host-old",
        observed_by_bd_version="bd version 1.1.2",
    )
    hive_schema.save(hq_dir, stale_record)

    # This run's probe fails (e.g. dolt transiently unavailable).
    monkeypatch.setattr(
        dolt_health,
        "probe_raw_schema_version",
        lambda *a, **k: dolt_health.SchemaProbeResult(None, "transient failure"),
    )

    warns = doctor._bd_schema_skew_warnings({}, [_entry()], tmp_path)

    assert len(warns) == 1
    assert "v59" in warns[0]
    assert "unverified since" in warns[0]
    # And the prior record was never overwritten with a placeholder.
    assert hive_schema.load(hq_dir, "github", "acme", "zf") == stale_record


def test_concurrent_probes_preserve_order_and_persist_every_hive(tmp_path, monkeypatch):
    """bh-ti7ws: the per-hive `bd dolt status` / `bd sql schema_migrations` probes now run in a
    thread pool, not sequentially. The reported warnings must still come back in registry order
    regardless of which worker finishes first, and every hive's record must land in ITS OWN
    manifest file (hive_schema's shared `ruamel.yaml.YAML()` is lock-guarded) rather than one
    write corrupting or losing a sibling's."""
    import random
    import re
    import time as time_mod

    hq_dir = _hq(tmp_path)
    n = 14
    # Distinct, non-substring-colliding prefixes (not `f"r{i}"`, where "r1" is also a substring
    # of "r11"/"r13"'s warning) so extracting each warning's hive back out of `warns` is exact.
    prefixes = [f"zz{i:02d}" for i in range(n)]
    entries = []
    for i in range(n):
        entries.append(_entry(org="acme", repo=f"r{i}", prefix=prefixes[i]))
        (tmp_path / "github" / "acme" / f"r{i}" / ".beads").mkdir(parents=True)

    monkeypatch.setattr(doctor.config, "hq_dir", lambda: hq_dir)
    monkeypatch.setattr(doctor.safety, "_bd_dolt_mode", lambda path: "embedded")
    monkeypatch.setattr(
        dolt_health, "local_bd_schema_version", lambda **k: dolt_health.SchemaProbeResult(53, "x")
    )
    monkeypatch.setattr(dolt_health, "_local_bd_version_string", lambda **k: "bd version 1.1.2")

    def fake_probe(hive_dir, *, dolt_mode, timeout=None):
        # jitter completion order across threads
        time_mod.sleep(random.uniform(0, 0.01))
        i = int(hive_dir.name.removeprefix("r"))
        # even i: matches local (53) -> no skew; odd i: skewed -> warns
        version = 53 if i % 2 == 0 else 59
        return dolt_health.SchemaProbeResult(version, "probed")

    monkeypatch.setattr(dolt_health, "probe_raw_schema_version", fake_probe)

    warns = doctor._bd_schema_skew_warnings({}, entries, tmp_path)

    # Every odd hive is skewed; a scrambled completion order only flips one adjacent pair
    # undetected, so this many discriminating positions in registry order is needed to make a
    # wrong-order (or wrong-file) implementation fail reliably.
    expected_prefixes = [prefixes[i] for i in range(1, n, 2)]
    # Assert the ACTUAL sequence `warns` came back in, not a filtered/reordered view of
    # `expected_prefixes` — iterating `warns` (pool-completion order pre-fix) is what makes
    # this discriminate; iterating `expected_prefixes` (already-sorted) cannot.
    actual_prefixes = [re.search(r"hive '([^']+)':", w).group(1) for w in warns]
    assert actual_prefixes == expected_prefixes

    # And every hive — even the non-skewed, non-warned ones — got its OWN record persisted
    # correctly: no cross-hive corruption from the shared YAML instance under concurrency.
    for i in range(n):
        record = hive_schema.load(hq_dir, "github", "acme", f"r{i}")
        assert record.schema_version == (53 if i % 2 == 0 else 59)


def test_stale_no_skew_record_still_warns_not_a_silent_all_clear(tmp_path, monkeypatch):
    """Review-caught AC4 regression, reproduced exactly: an 11-day-old record showing NO skew
    at the time it was confirmed (both sides were v53 then), local bd still v53 now, and this
    run's re-probe transient-failing — i.e. "a newer bd on another host may have advanced the
    real store since, and we can't tell right now". This must never render as silence."""
    hq_dir = _hq(tmp_path)
    _checkout(tmp_path)
    monkeypatch.setattr(doctor.config, "hq_dir", lambda: hq_dir)
    monkeypatch.setattr(doctor.safety, "_bd_dolt_mode", lambda path: "embedded")
    monkeypatch.setattr(
        dolt_health,
        "local_bd_schema_version",
        lambda **k: dolt_health.SchemaProbeResult(53, "cached"),
    )

    old_but_clean_record = hive_schema.HiveSchemaRecord(
        provider="github",
        org="acme",
        repo="zf",
        schema_version=53,  # matched local bd exactly, at the time it was recorded
        dolt_mode="embedded",
        observed_at="2015-01-01T00:00:00Z",  # 11+ days old — well past the staleness bound
        observed_by_host="host-old",
        observed_by_bd_version="bd version 1.1.2",
    )
    hive_schema.save(hq_dir, old_but_clean_record)

    # This run's re-probe fails, so refresh() cannot confirm the CURRENT real version.
    monkeypatch.setattr(
        dolt_health,
        "probe_raw_schema_version",
        lambda *a, **k: dolt_health.SchemaProbeResult(None, "transient failure"),
    )

    warns = doctor._bd_schema_skew_warnings({}, [_entry()], tmp_path)

    assert len(warns) == 1, "a stale, unreconfirmed record must never be a silent all-clear"
    assert "zf" in warns[0]
    assert "unverified" in warns[0]
    # The prior record itself is untouched (refresh never writes a placeholder on failure).
    assert hive_schema.load(hq_dir, "github", "acme", "zf") == old_but_clean_record
