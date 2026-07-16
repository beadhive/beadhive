"""Tests for `ws report` — the INTERNAL terminal of cross-rig report intake (bead
).

Pin the contract for both targets we own:
  * a **cloned** rig — the report is written into its on-disk `.beads` via `bd -C create`, no push;
  * a **clone-on-demand** rig — the rig is fetched by reusing `hub._fetch_cache`, the report is
    written into the cache, then committed + pushed back with bd's native `dolt` verbs.

Both assert the acceptance-critical wiring: the closed `origin=report` intake CHANNEL (NOT the
retired `source_system=report` overload) + reporter (`bd --actor`) provenance, the
`intake=untriaged` queue state (both from the shared `ws/state.py` vocabulary), plus the
auto-applied target provider/org/repo triplet.
"""

from __future__ import annotations

import json
from collections import namedtuple

from beadhive import config, report

Completed = namedtuple("Completed", "returncode stdout stderr")

_ENTRY = {"provider": "github", "org": "acme", "repo": "widget", "prefix": "wid"}


def _cfg():
    return {"managed_repos": [dict(_ENTRY)]}


class _Recorder:
    """Fake `report.run` that records every bd invocation and captures the `--json create` args
    so a test can assert on the exact bead that would be filed (triplet, type) and that no
    `source_system` overload is stamped."""

    def __init__(self, new_id="wid-abc"):
        self.new_id = new_id
        self.calls: list[list[str]] = []
        self.create_args: list[str] | None = None

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        # bd -C <dir> [--actor X] <verb> … — the verb sits past the optional --actor pair
        rest = cmd[3:]
        if rest[:1] == ["--actor"]:
            rest = rest[2:]
        # `--json` is a global flag, so create shows up as `--json create …`
        if rest[:1] == ["--json"]:
            rest = rest[1:]
        verb = rest[0] if rest else ""
        if verb == "create":
            self.create_args = rest
            return Completed(0, json.dumps({"id": self.new_id}), "")
        return Completed(0, "", "")

    def create_labels(self) -> list[str]:
        """The comma-split labels passed to `bd create -l …` (the auto-applied triplet)."""
        args = self.create_args or []
        for i, tok in enumerate(args):
            if tok in ("-l", "--labels") and i + 1 < len(args):
                return args[i + 1].split(",")
        return []

    def create_type(self) -> str:
        """The `--type`/`-t` value passed to `bd create`."""
        args = self.create_args or []
        for i, tok in enumerate(args):
            if tok in ("-t", "--type") and i + 1 < len(args):
                return args[i + 1]
        return ""

    def all_args(self) -> list[str]:
        """Every token across every recorded call — lets a test assert an absence globally."""
        return [tok for cmd in self.calls for tok in cmd]

    def actor_of(self, verb) -> str:
        """The `--actor` value stamped on the first call whose verb matches (skipping the
        `--json` global flag that may sit between `--actor <val>` and the verb)."""
        for cmd in self.calls:
            if "--actor" in cmd:
                i = cmd.index("--actor")
                after = [tok for tok in cmd[i + 2 :] if tok != "--json"]
                if after and after[0] == verb:
                    return cmd[i + 1]
        return ""

    def has_verb(self, *verb_tokens) -> bool:
        return any(
            any(cmd[i : i + len(verb_tokens)] == list(verb_tokens) for i in range(len(cmd)))
            for cmd in self.calls
        )


def _wire(monkeypatch, rec, *, cloned, tmp_path):
    """Point report at a fake bd + the given target kind (cloned vs clone-on-demand)."""
    monkeypatch.setattr(report.bd, "_run", rec)
    monkeypatch.setattr(report.registry, "resolve_hive", lambda cfg, hive: dict(_ENTRY))
    # Intake validates only the NEW bead's labels; default them clean.
    monkeypatch.setattr(report.validate, "bead_violations", lambda *a, **k: [])
    hive_dir = tmp_path / "rig"
    cache_dir = tmp_path / "cache"
    if cloned:
        (hive_dir / ".beads").mkdir(parents=True)
    monkeypatch.setattr(report.registry, "hive_dir", lambda e: hive_dir)

    fetched = {"called": False}

    def fake_fetch(cfg, entry):
        fetched["called"] = True
        (cache_dir / ".beads").mkdir(parents=True, exist_ok=True)
        return cache_dir

    monkeypatch.setattr(report.hub, "_fetch_cache", fake_fetch)
    return hive_dir, cache_dir, fetched


def test_cloned_target_writes_with_provenance_and_intake(tmp_path, monkeypatch):
    """A cloned rig: report is created born-native in its on-disk .beads with the target triplet,
    the closed origin=report channel + reporter actor, and intake=untriaged — and nothing is
    pushed. The retired source_system=report overload must NOT appear anywhere."""
    rec = _Recorder()
    hive_dir, _cache, fetched = _wire(monkeypatch, rec, cloned=True, tmp_path=tmp_path)

    code, error, new_id = report.file_report(
        "wid", "login is broken", "bug", "crew/dev-report", cfg=_cfg()
    )

    assert (code, error, new_id) == (0, "", "wid-abc")
    assert not fetched["called"]  # already cloned → no clone-on-demand
    # provenance: closed origin channel via set-state + reporter actor (two distinct concerns)
    assert rec.has_verb("set-state", "wid-abc", "origin=report")
    assert rec.actor_of("create") == "crew/dev-report"
    assert rec.actor_of("set-state") == "crew/dev-report"
    # RETIRED: no source_system=report overload, and no `import` primitive, anywhere
    assert "source_system" not in " ".join(rec.all_args())
    assert not rec.has_verb("import")
    # bh-nqyv: the set-state reason names the real CLI alias, not the retired `ws` name
    reason_call = next(cmd for cmd in rec.calls if "--reason" in cmd)
    reason = reason_call[reason_call.index("--reason") + 1]
    assert reason == f"filed via {config.BINARY_ALIAS} report"
    # type-aware + target triplet auto-applied on the plain create
    assert rec.create_type() == "bug"
    assert set(rec.create_labels()) >= {"provider:github", "org:acme", "repo:widget"}
    # intake queue state, event-sourced from the shared vocabulary (not an ad-hoc label)
    assert rec.has_verb("set-state", "wid-abc", "intake=untriaged")
    # cloned target is local — no dolt push
    assert not rec.has_verb("dolt", "push")
    # every write is scoped to the cloned rig dir, not the cache
    assert all(cmd[1:3] == ["-C", str(hive_dir)] for cmd in rec.calls)


def test_file_report_passes_description_to_bd_create(tmp_path, monkeypatch):
    """bh-u0qd: a non-empty `description` reaches `bd create -d <body>` verbatim."""
    rec = _Recorder()
    _wire(monkeypatch, rec, cloned=True, tmp_path=tmp_path)

    code, error, new_id = report.file_report(
        "wid", "login is broken", "bug", "crew/dev-report", cfg=_cfg(), description="body text"
    )

    assert (code, error, new_id) == (0, "", "wid-abc")
    args = rec.create_args or []
    assert "-d" in args
    assert args[args.index("-d") + 1] == "body text"


def test_file_report_omits_description_flag_when_empty(tmp_path, monkeypatch):
    """No `description` → no `-d` flag at all (existing callers unaffected)."""
    rec = _Recorder()
    _wire(monkeypatch, rec, cloned=True, tmp_path=tmp_path)

    report.file_report("wid", "login is broken", "bug", "crew/dev-report", cfg=_cfg())

    assert "-d" not in (rec.create_args or [])


def test_clone_on_demand_target_fetches_creates_and_pushes(tmp_path, monkeypatch):
    """An uncloned rig we own: fetched via hub._fetch_cache, the report is created in the cache
    with the same origin + intake wiring, then committed and pushed back."""
    rec = _Recorder(new_id="wid-xyz")
    _hive, cache_dir, fetched = _wire(monkeypatch, rec, cloned=False, tmp_path=tmp_path)

    code, error, new_id = report.file_report(
        "wid", "add dark mode", "feature", "super/intendent", cfg=_cfg()
    )

    assert (code, error, new_id) == (0, "", "wid-xyz")
    assert fetched["called"]  # clone-on-demand reused hub._fetch_cache
    assert rec.has_verb("set-state", "wid-xyz", "origin=report")
    assert "source_system" not in " ".join(rec.all_args())
    assert rec.create_type() == "feature"
    assert rec.actor_of("create") == "super/intendent"  # superintendent-routed, SAME verb
    assert rec.has_verb("set-state", "wid-xyz", "intake=untriaged")
    # uncloned → create + push back with bd's native dolt verbs (not a hand-rolled write)
    assert rec.has_verb("dolt", "commit", "-m", "report: add dark mode")
    assert rec.has_verb("dolt", "push")
    # writes target the fetched cache
    assert all(cmd[1:3] == ["-C", str(cache_dir)] for cmd in rec.calls)


def test_bad_type_is_rejected_before_any_write(tmp_path, monkeypatch):
    """An unsupported --type fails fast with no bd invocation."""
    rec = _Recorder()
    _wire(monkeypatch, rec, cloned=True, tmp_path=tmp_path)

    code, error, new_id = report.file_report("wid", "t", "task", "crew/dev-report", cfg=_cfg())

    assert code == 1
    assert "--type must be one of" in error
    assert new_id == ""
    assert rec.calls == []


def test_uncloned_without_remote_data_is_reported(tmp_path, monkeypatch):
    """A rig we own but haven't cloned and that has no remote beads data to fetch is refused
    (not silently dropped)."""
    rec = _Recorder()
    monkeypatch.setattr(report.bd, "_run", rec)
    monkeypatch.setattr(report.registry, "resolve_hive", lambda cfg, hive: dict(_ENTRY))
    monkeypatch.setattr(report.validate, "has_violations", lambda *a, **k: False)
    monkeypatch.setattr(report.registry, "hive_dir", lambda e: tmp_path / "absent")
    monkeypatch.setattr(report.hub, "_fetch_cache", lambda cfg, entry: None)

    code, error, new_id = report.file_report("wid", "x", "bug", "crew/dev-report", cfg=_cfg())

    assert code == 1
    assert "no remote beads data" in error
    assert rec.calls == []


def test_preexisting_target_debt_does_not_block_a_valid_report(tmp_path, monkeypatch):
    """Regression: a well-formed report SUCCEEDS even when the target rig
    already carries pre-existing label debt. Cross-rig intake validates only the NEW bead's own
    labels — it never consults the target rig's whole DB (`validate.has_violations`), so a
    reporter is never deadlocked by debt it has no authority to fix."""
    rec = _Recorder()
    _wire(monkeypatch, rec, cloned=True, tmp_path=tmp_path)

    # A tripwire: if file_report ever reaches back to the whole-rig linter, fail loudly.
    def _boom(*a, **k):  # pragma: no cover - only runs on regression
        raise AssertionError("file_report must not gate on the target rig's whole DB")

    monkeypatch.setattr(report.validate, "has_violations", _boom)

    code, error, new_id = report.file_report(
        "wid", "login is broken", "bug", "crew/dev-report", cfg=_cfg()
    )

    assert (code, error, new_id) == (0, "", "wid-abc")
    assert rec.has_verb("set-state", "wid-abc", "origin=report")


def test_invalid_new_bead_labels_block_the_report(tmp_path, monkeypatch):
    """The intake gate still refuses when the NEW bead itself would carry an invalid label —
    scoped to just that bead, not the target rig's DB. Nothing is written."""
    rec = _Recorder()
    _wire(monkeypatch, rec, cloned=True, tmp_path=tmp_path)
    monkeypatch.setattr(
        report.validate, "bead_violations", lambda *a, **k: ["wid-intake\tbad-origin:bogus"]
    )

    code, error, _new = report.file_report("wid", "x", "bug", "crew/dev-report", cfg=_cfg())

    assert code == 1
    assert "invalid labels" in error
    assert "bad-origin:bogus" in error
    assert rec.calls == []


# ---- CLI: --description / piped stdin ---------------------------------------


def test_cli_report_reads_description_from_nontty_stdin(monkeypatch):
    """bh-u0qd: `bh report` with no `--description` reads the body from non-TTY stdin."""
    from typer.testing import CliRunner

    from beadhive.cli import app

    captured = {}

    def fake_file_report(hive, title, report_type, actor, description="", **kwargs):
        captured["description"] = description
        return 0, "", "wid-1"

    monkeypatch.setattr(report, "file_report", fake_file_report)
    monkeypatch.setattr(report, "entry_dupes", lambda *a, **k: [])

    result = CliRunner().invoke(
        app, ["report", "wid", "login is broken"], input="the full report body\n"
    )

    assert result.exit_code == 0, result.output
    assert captured["description"] == "the full report body\n"


def test_cli_report_description_flag_wins_over_stdin(monkeypatch):
    """An explicit `--description` is used as-is; stdin is not consulted."""
    from typer.testing import CliRunner

    from beadhive.cli import app

    captured = {}

    def fake_file_report(hive, title, report_type, actor, description="", **kwargs):
        captured["description"] = description
        return 0, "", "wid-1"

    monkeypatch.setattr(report, "file_report", fake_file_report)
    monkeypatch.setattr(report, "entry_dupes", lambda *a, **k: [])

    result = CliRunner().invoke(
        app,
        ["report", "wid", "login is broken", "--description", "flag body"],
        input="stdin body\n",
    )

    assert result.exit_code == 0, result.output
    assert captured["description"] == "flag body"
