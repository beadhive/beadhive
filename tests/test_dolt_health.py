"""`dolt_health` — the real bd/Dolt schema-migration version, and why the two decoy
``schema_version`` fields never get read as if they were it (`bh-wnly`).

Covers the acceptance bar directly:
  * the raw probe reads `schema_migrations` via a direct `dolt sql`, not `bd dolt status`'s
    always-``1`` JSON-envelope field (the trap this bead's own design work uncovered);
  * mode dispatch (embedded -> direct `dolt`, server-shaped -> `bd sql`);
  * `local_bd_schema_version` caches by the exact `bd --version` string, so a version bump
    invalidates it and a repeat call for the same binary never re-probes;
  * `schema_skew_advisory` fires ONLY when both sides are known AND local < recorded, names
    both numbers, and stays silent (not a green claim — see `hive_schema` for that half) when
    either side is unknown.

All subprocess calls are faked (`monkeypatch.setattr(dolt_health, "run", ...)`) — no real `bd`/
`dolt` binary is required for this file. `tests/test_dolt_health_int.py` covers the real-binary
regression proof for the trap itself.
"""

from __future__ import annotations

import json
from collections import namedtuple
from pathlib import Path

from beadhive import dolt_health

Completed = namedtuple("Completed", "returncode stdout stderr")


def _json_rows(**row) -> str:
    """`dolt ... sql -r json`'s real shape — an envelope."""
    return json.dumps({"rows": [row]})


def _json_bare_array(**row) -> str:
    """`bd sql --json`'s real shape — NO envelope, a bare array (confirmed by review against a
    live server-mode hive, `github/briancripe/testfoo`; `_json_rows`'s envelope is a DIFFERENT
    tool's shape and must never stand in for this one — that mix-up is exactly the bug review
    caught: a test double built from the embedded shape silently validated the server path
    against the wrong format)."""
    return json.dumps([row])


# ---- _parse_max_version -----------------------------------------------------------------


def test_parse_max_version_reads_the_aliased_column_from_the_dolt_cli_envelope_shape():
    assert dolt_health._parse_max_version(_json_rows(max_version=59)) == 59


def test_parse_max_version_reads_the_aliased_column_from_bd_sqls_bare_array_shape():
    """The bug review caught: `bd sql --json` returns `[{"max_version": 59}]`, not
    `{"rows": [...]}`. Both real shapes must parse to the same value."""
    assert dolt_health._parse_max_version(_json_bare_array(max_version=59)) == 59


def test_parse_max_version_none_on_garbage():
    assert dolt_health._parse_max_version("not json") is None
    assert dolt_health._parse_max_version(json.dumps({"rows": []})) is None
    assert dolt_health._parse_max_version(json.dumps({"rows": [{}]})) is None
    assert dolt_health._parse_max_version(json.dumps({"rows": [{"max_version": "59"}]})) is None
    assert dolt_health._parse_max_version(json.dumps([])) is None
    assert dolt_health._parse_max_version(json.dumps([{}])) is None
    assert dolt_health._parse_max_version(json.dumps("neither a dict nor a list")) is None


# ---- probe_embedded_schema_version -------------------------------------------------------


def test_embedded_probe_reads_the_real_int_not_the_json_envelope_decoy(tmp_path, monkeypatch):
    """The trap this bead exists to avoid: `bd dolt status --json`'s `schema_version` is always
    `1` (the CLI's own JSON envelope version). This probe must never be that number by
    construction — it doesn't even call `bd dolt status` — verified here by returning 59 from
    the faked `dolt sql` call and asserting the probe reports exactly that, not 1."""
    db_dir = tmp_path / "embeddeddolt" / "beads"
    (db_dir / ".dolt").mkdir(parents=True)

    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return Completed(0, _json_rows(max_version=59), "")

    monkeypatch.setattr(dolt_health, "run", fake_run)
    result = dolt_health.probe_embedded_schema_version(db_dir)

    assert result.version == 59
    assert result.version != 1  # the JSONSchemaVersion decoy this bead's own trap section names
    assert calls[0][:2] == ["dolt", "--data-dir"]
    assert str(db_dir) in calls[0]
    assert "sql" in calls[0]


def test_embedded_probe_none_when_not_a_dolt_dir(tmp_path):
    result = dolt_health.probe_embedded_schema_version(tmp_path / "nope")
    assert result.version is None
    assert "not a Dolt data directory" in result.detail


def test_embedded_probe_none_on_nonzero_exit(tmp_path, monkeypatch):
    db_dir = tmp_path / "db"
    (db_dir / ".dolt").mkdir(parents=True)
    monkeypatch.setattr(dolt_health, "run", lambda *a, **k: Completed(1, "", "boom"))
    result = dolt_health.probe_embedded_schema_version(db_dir)
    assert result.version is None
    assert "boom" in result.detail


# ---- _metadata_dolt_database / embedded_store_dir ----------------------------------------


def test_embedded_store_dir_reads_dolt_database_from_metadata(tmp_path):
    hive_dir = tmp_path / "hive"
    (hive_dir / ".beads").mkdir(parents=True)
    (hive_dir / ".beads" / "metadata.json").write_text(json.dumps({"dolt_database": "beads"}))

    # Two candidate database dirs, mirroring the real repo's own embeddeddolt/ (beads + bh) —
    # a naive "just glob the one subdir" approach would be ambiguous here.
    (hive_dir / ".beads" / "embeddeddolt" / "beads").mkdir(parents=True)
    (hive_dir / ".beads" / "embeddeddolt" / "bh").mkdir(parents=True)

    assert dolt_health.embedded_store_dir(hive_dir) == (
        hive_dir / ".beads" / "embeddeddolt" / "beads"
    )


def test_embedded_store_dir_falls_back_without_metadata(tmp_path):
    hive_dir = tmp_path / "hive"
    hive_dir.mkdir()
    assert dolt_health.embedded_store_dir(hive_dir, database="fallback") == (
        hive_dir / ".beads" / "embeddeddolt" / "fallback"
    )


# ---- probe_server_schema_version + dispatch ------------------------------------------------


def test_server_probe_uses_bd_sql(tmp_path, monkeypatch):
    """`bd sql --json` returns a BARE array, not the `dolt ... sql -r json` envelope — the
    double here must emit what the real tool actually returns (`_json_bare_array`), confirmed
    against a live server-mode hive by review; a double built from the embedded shape
    (`_json_rows`) would validate against the wrong format and silently mask a real parse
    failure, as it did before this fix."""
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return Completed(0, _json_bare_array(max_version=12), "")

    monkeypatch.setattr(dolt_health, "run", fake_run)
    result = dolt_health.probe_server_schema_version(tmp_path)
    assert result.version == 12
    assert calls[0][0] == "bd"
    assert "sql" in calls[0]


def test_server_probe_returns_none_if_it_were_fed_the_wrong_envelope_shape():
    """Documents the exact bug review caught, as a standing regression guard: if
    `_parse_max_version` only understood `dolt`'s `{"rows": [...]}` envelope, `bd sql --json`'s
    real bare-array output would silently parse to `None` — a probe that can never succeed for
    any owned/shared/external hive. This must NOT be the case after the fix."""
    assert dolt_health._parse_max_version(_json_bare_array(max_version=59)) == 59


def test_probe_raw_schema_version_dispatches_embedded(tmp_path, monkeypatch):
    (tmp_path / ".beads" / "embeddeddolt" / "x" / ".dolt").mkdir(parents=True)
    (tmp_path / ".beads" / "metadata.json").write_text(json.dumps({"dolt_database": "x"}))
    seen = {}

    def fake_embedded(db_dir, **kw):
        seen["db_dir"] = db_dir
        return dolt_health.SchemaProbeResult(7, "ok")

    monkeypatch.setattr(dolt_health, "probe_embedded_schema_version", fake_embedded)
    result = dolt_health.probe_raw_schema_version(tmp_path, dolt_mode="embedded")
    assert result.version == 7
    assert seen["db_dir"] == tmp_path / ".beads" / "embeddeddolt" / "x"


def test_probe_raw_schema_version_dispatches_none_mode_to_embedded_too(tmp_path, monkeypatch):
    """Measured (bh-u562.1 finding 9): owned + local-external modes also report no "mode" key
    at all — `dolt_mode=None` must not be routed to the server probe by default."""
    called = {}
    monkeypatch.setattr(
        dolt_health,
        "probe_embedded_schema_version",
        lambda *a, **k: called.setdefault("embedded", True) or dolt_health.SchemaProbeResult(3, ""),
    )
    monkeypatch.setattr(
        dolt_health,
        "probe_server_schema_version",
        lambda *a, **k: called.setdefault("server", True) or dolt_health.SchemaProbeResult(3, ""),
    )
    dolt_health.probe_raw_schema_version(tmp_path, dolt_mode=None)
    assert called == {"embedded": True}


def test_probe_raw_schema_version_dispatches_server_mode(tmp_path, monkeypatch):
    called = {}
    monkeypatch.setattr(
        dolt_health,
        "probe_server_schema_version",
        lambda *a, **k: called.setdefault("server", True) or dolt_health.SchemaProbeResult(3, ""),
    )
    dolt_health.probe_raw_schema_version(tmp_path, dolt_mode="external")
    assert called == {"server": True}


# ---- local_bd_schema_version + caching ------------------------------------------------------


def test_local_bd_schema_version_none_when_bd_missing(monkeypatch):
    monkeypatch.setattr(dolt_health, "run", lambda *a, **k: Completed(1, "", "not found"))
    result = dolt_health.local_bd_schema_version()
    assert result.version is None


def _fake_bd_init_scratch(cmd, cwd) -> None:
    """Mirror real `bd init`'s one observable side effect this probe depends on: a
    `.beads/embeddeddolt/<prefix>/.dolt/` directory under `cwd` — the `--prefix` value is the
    database name (`embedded_store_dir`'s fallback)."""
    prefix = cmd[cmd.index("--prefix") + 1]
    (Path(cwd) / ".beads" / "embeddeddolt" / prefix / ".dolt").mkdir(parents=True)


def test_local_bd_schema_version_caches_by_bd_version_string(tmp_path, monkeypatch):
    monkeypatch.setattr(dolt_health.config, "cache_dir", lambda: tmp_path)
    calls = {"init": 0, "version": 0}

    def fake_run(cmd, **kw):
        if cmd[:2] == ["bd", "--version"]:
            calls["version"] += 1
            return Completed(0, "bd version 9.9.9", "")
        if cmd[:2] == ["bd", "init"]:
            calls["init"] += 1
            _fake_bd_init_scratch(cmd, kw["cwd"])
            return Completed(0, "", "")
        if cmd[0] == "dolt":
            return Completed(0, _json_rows(max_version=42), "")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(dolt_health, "run", fake_run)

    first = dolt_health.local_bd_schema_version()
    second = dolt_health.local_bd_schema_version()

    assert first.version == 42
    assert second.version == 42
    assert calls["init"] == 1  # the real (expensive) scratch probe only ran once
    assert calls["version"] == 2  # each call still re-checks WHICH bd is on PATH


def test_local_bd_schema_version_reprobes_after_a_bd_upgrade(tmp_path, monkeypatch):
    monkeypatch.setattr(dolt_health.config, "cache_dir", lambda: tmp_path)
    state = {"version": "bd version 1.0.0", "n": 42}

    def fake_run(cmd, **kw):
        if cmd[:2] == ["bd", "--version"]:
            return Completed(0, state["version"], "")
        if cmd[:2] == ["bd", "init"]:
            _fake_bd_init_scratch(cmd, kw["cwd"])
            return Completed(0, "", "")
        if cmd[0] == "dolt":
            return Completed(0, _json_rows(max_version=state["n"]), "")
        raise AssertionError(cmd)

    monkeypatch.setattr(dolt_health, "run", fake_run)

    before = dolt_health.local_bd_schema_version()
    state["version"] = "bd version 2.0.0"
    state["n"] = 59
    after = dolt_health.local_bd_schema_version()

    assert before.version == 42
    assert after.version == 59  # a version-string change busts the cache, not stale forever


# ---- schema_skew_advisory --------------------------------------------------------------------


def test_advisory_fires_when_local_behind_recorded():
    local = dolt_health.SchemaProbeResult(53, "")
    advisory = dolt_health.schema_skew_advisory("bh", local, 59)
    assert advisory is not None
    assert "v53" in advisory
    assert "v59" in advisory
    assert "bh" in advisory


def test_advisory_silent_when_local_at_or_ahead_of_recorded():
    local = dolt_health.SchemaProbeResult(59, "")
    assert dolt_health.schema_skew_advisory("bh", local, 59) is None
    assert dolt_health.schema_skew_advisory("bh", local, 12) is None


def test_advisory_silent_when_local_unknown():
    local = dolt_health.SchemaProbeResult(None, "bd unavailable")
    assert dolt_health.schema_skew_advisory("bh", local, 59) is None


def test_advisory_silent_when_recorded_unknown():
    local = dolt_health.SchemaProbeResult(53, "")
    assert dolt_health.schema_skew_advisory("bh", local, None) is None
