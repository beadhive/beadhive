"""`hive_ready._schema_version_check` — the read-only, no-store-open half of `bh-wnly`
(AC1 + AC5: discoverable from HQ, and reading it never opens the hive's own store).

`root` is passed to `_schema_version_check` only for signature symmetry with the other checks
and is asserted unused below — the whole point of this check living in `bh hive ready` (a
read-only verb) rather than only in `bh doctor` (the refresh trigger) is that it can answer
"is this safe?" from HQ's recorded value alone, without touching the hive's own checkout.
"""

from __future__ import annotations

from pathlib import Path

from beadhive import config, dolt_health, hive_ready, hive_schema

_ENTRY = {"provider": "github", "org": "acme", "repo": "zf", "prefix": "zf", "kind": "personal"}


class _ExplodingPath(type(Path())):
    """A Path stand-in that raises if anything under it is touched — the enforcement for the
    "never opens this hive's store" claim: any accidental filesystem access on `root` fails
    the test loudly instead of silently passing."""

    def __new__(cls, *a, **k):
        return super().__new__(cls, "/should-never-be-touched")

    def __truediv__(self, other):
        raise AssertionError(f"_schema_version_check touched root ({other!r}) — it must not")


def test_na_when_hive_not_registered(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "hq_dir", lambda: tmp_path / "hq")
    check = hive_ready._schema_version_check(None, _ExplodingPath())
    assert check.state == "na"


def test_na_when_no_hq(monkeypatch):
    monkeypatch.setattr(config, "hq_dir", lambda: Path("/definitely/does/not/exist"))
    check = hive_ready._schema_version_check(_ENTRY, _ExplodingPath())
    assert check.state == "na"
    assert "HQ" in check.detail


def test_na_when_local_bd_unknown(monkeypatch, tmp_path):
    hq_dir = tmp_path / "hq"
    (hq_dir / ".beads").mkdir(parents=True)
    monkeypatch.setattr(config, "hq_dir", lambda: hq_dir)
    monkeypatch.setattr(
        dolt_health, "local_bd_schema_version", lambda **k: dolt_health.SchemaProbeResult(None, "x")
    )
    check = hive_ready._schema_version_check(_ENTRY, _ExplodingPath())
    assert check.state == "na"


def test_warn_when_never_recorded(monkeypatch, tmp_path):
    hq_dir = tmp_path / "hq"
    (hq_dir / ".beads").mkdir(parents=True)
    monkeypatch.setattr(config, "hq_dir", lambda: hq_dir)
    monkeypatch.setattr(
        dolt_health, "local_bd_schema_version", lambda **k: dolt_health.SchemaProbeResult(53, "x")
    )
    check = hive_ready._schema_version_check(_ENTRY, _ExplodingPath())
    assert check.state == "warn"
    assert "never recorded" in check.detail


def test_warn_on_real_skew(monkeypatch, tmp_path):
    hq_dir = tmp_path / "hq"
    (hq_dir / ".beads").mkdir(parents=True)
    hive_schema.save(
        hq_dir,
        hive_schema.HiveSchemaRecord(
            provider="github",
            org="acme",
            repo="zf",
            schema_version=59,
            dolt_mode="embedded",
            observed_at="2026-08-03T00:00:00Z",
            observed_by_host="h",
            observed_by_bd_version="bd version 1.1.2",
        ),
    )
    monkeypatch.setattr(config, "hq_dir", lambda: hq_dir)
    monkeypatch.setattr(
        dolt_health, "local_bd_schema_version", lambda **k: dolt_health.SchemaProbeResult(53, "x")
    )

    check = hive_ready._schema_version_check(_ENTRY, _ExplodingPath())

    assert check.state == "warn"
    assert "v59" in check.detail
    assert "v53" in check.detail


def test_ok_when_no_skew_and_fresh(monkeypatch, tmp_path):
    from datetime import UTC, datetime

    hq_dir = tmp_path / "hq"
    (hq_dir / ".beads").mkdir(parents=True)
    hive_schema.save(
        hq_dir,
        hive_schema.HiveSchemaRecord(
            provider="github",
            org="acme",
            repo="zf",
            schema_version=53,
            dolt_mode="embedded",
            observed_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            observed_by_host="h",
            observed_by_bd_version="bd version 1.1.2",
        ),
    )
    monkeypatch.setattr(config, "hq_dir", lambda: hq_dir)
    monkeypatch.setattr(
        dolt_health, "local_bd_schema_version", lambda **k: dolt_health.SchemaProbeResult(59, "x")
    )

    check = hive_ready._schema_version_check(_ENTRY, _ExplodingPath())

    assert check.state == "ok"
    assert "v53" in check.detail


def test_warn_when_no_skew_but_stale(monkeypatch, tmp_path):
    hq_dir = tmp_path / "hq"
    (hq_dir / ".beads").mkdir(parents=True)
    hive_schema.save(
        hq_dir,
        hive_schema.HiveSchemaRecord(
            provider="github",
            org="acme",
            repo="zf",
            schema_version=53,
            dolt_mode="embedded",
            observed_at="2000-01-01T00:00:00Z",  # far past the default staleness bound
            observed_by_host="h",
            observed_by_bd_version="bd version 1.1.2",
        ),
    )
    monkeypatch.setattr(config, "hq_dir", lambda: hq_dir)
    monkeypatch.setattr(
        dolt_health, "local_bd_schema_version", lambda **k: dolt_health.SchemaProbeResult(59, "x")
    )

    check = hive_ready._schema_version_check(_ENTRY, _ExplodingPath())

    assert check.state == "warn"  # AC4: stale must never read as a confirmed "ok"
    assert "unverified" in check.detail


def test_check_is_included_in_scan(monkeypatch, tmp_path):
    """The check actually shows up in `scan()`'s full list, not just as a standalone helper."""
    hq_dir = tmp_path / "hq"
    monkeypatch.setattr(config, "hq_dir", lambda: hq_dir)  # no HQ -> na, cheap + deterministic
    root = tmp_path / "repo"
    root.mkdir()

    checks = hive_ready.scan({}, ("github", "acme", "zf"), None, root)

    labels = [c.label for c in checks]
    assert "bd schema version" in labels
