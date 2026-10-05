from __future__ import annotations

import json
import re
from types import SimpleNamespace

from beadhive import selective_validation
from beadhive.modules.config.contracts import AttestConfig
from beadhive.modules.work.domain.impact import ImpactReceipt, KeyEvidence


def _without_durations(output: str) -> str:
    return re.sub(r"\d+\.\d{3}s", "<elapsed>", output)


def test_impact_fallback_warning_is_prominent_and_sent_to_stderr(capsys) -> None:
    selective_validation.warn_impact_fallback(
        "pants: error: RuntimeError: Pants peek failed after retry"
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "!!! WARNING: IMPACT RESOLUTION FALLBACK !!!" in captured.err
    assert "Pants peek failed after retry" in captured.err
    assert "running every attestation key" in captured.err


class _Resolver:
    def __init__(self, fallback_reason=""):
        self.seen = []
        self.fallback_reason = fallback_reason

    def resolve(self, _repo, _base, _head, keys):
        self.seen.append(tuple(key.name for key in keys))
        return ImpactReceipt(
            backend="native-full",
            backend_version="1",
            base_tree="base",
            head_tree="head",
            changed_paths=("src/x.py",),
            unowned_paths=(),
            global_inputs_hit=(),
            invalidated_keys=tuple(key.name for key in keys),
            unaffected_keys=(),
            evidence={key.name: KeyEvidence("native-full") for key in keys},
            fallback_reason=self.fallback_reason,
        )


class _UnaffectedResolver:
    def resolve(self, _repo, _base, _head, keys):
        return ImpactReceipt(
            backend="pants",
            backend_version="1",
            base_tree="base",
            head_tree="head",
            changed_paths=("notes/readme.md",),
            unowned_paths=(),
            global_inputs_hit=(),
            invalidated_keys=(),
            unaffected_keys=tuple(key.name for key in keys),
            evidence={key.name: KeyEvidence("pants") for key in keys},
        )


def _attest(*keys):
    return AttestConfig.model_validate({"keys": list(keys)})


def _semantic_attest(*keys):
    return AttestConfig.model_validate(
        {"keys": list(keys), "semantic": {"enabled": True, "command": "jevwrap select"}}
    )


def test_semantic_selection_filters_keys_without_claiming_evidence(monkeypatch) -> None:
    attest = _semantic_attest(
        {"name": "unit", "cmd": "just unit"},
        {"name": "guide", "cmd": "just guide"},
    )
    keys = selective_validation.attest_keys(attest)
    payload = {
        "schema": "jevwrap/select/1",
        "affected": ["unit"],
        "unaffected": ["guide"],
    }
    seen = {}

    def run(command, **kwargs):
        seen.update(command=command, kwargs=kwargs)
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload))

    monkeypatch.setattr(selective_validation.subprocess, "run", run)
    selected, record = selective_validation.semantic_selection(
        attest, "/repo", "base", "head", keys
    )

    assert [key.name for key in selected] == ["unit"]
    assert record == {"applied": True, "ran": ["unit"], "skipped": ["guide"]}
    assert seen["command"] == ["jevwrap", "select", "--base", "base", "--head", "head"]
    assert seen["kwargs"]["cwd"] == "/repo"


def test_semantic_selection_error_falls_back_to_normal_route(monkeypatch) -> None:
    attest = _semantic_attest({"name": "unit", "cmd": "just unit"})
    keys = selective_validation.attest_keys(attest)
    monkeypatch.setattr(
        selective_validation.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "schema": "jevwrap/select/1",
                    "error": "network unavailable",
                    "affected": ["unit"],
                    "unaffected": [],
                }
            ),
        ),
    )

    selected, record = selective_validation.semantic_selection(
        attest, "/repo", "base", "head", keys
    )

    assert selected is None
    assert record["applied"] is False
    assert "network unavailable" in record["error"]


def test_semantic_selection_partial_or_empty_answer_falls_back(monkeypatch) -> None:
    attest = _semantic_attest(
        {"name": "unit", "cmd": "just unit"},
        {"name": "guide", "cmd": "just guide"},
    )
    keys = selective_validation.attest_keys(attest)

    for payload in (
        {"schema": "jevwrap/select/1", "affected": ["unit"], "unaffected": []},
        {"schema": "jevwrap/select/1", "affected": [], "unaffected": ["unit", "guide"]},
    ):
        monkeypatch.setattr(
            selective_validation.subprocess,
            "run",
            lambda *_a, _payload=payload, **_k: SimpleNamespace(
                returncode=0, stdout=json.dumps(_payload)
            ),
        )
        selected, record = selective_validation.semantic_selection(
            attest, "/repo", "base", "head", keys
        )
        assert selected is None
        assert record["applied"] is False
        assert record["error"]


def _run(monkeypatch, attest, runner, *, fallback_reason=""):
    resolver = _Resolver(fallback_reason)
    monkeypatch.setattr(selective_validation.config, "attest_config", lambda *_: attest)
    monkeypatch.setattr(selective_validation, "impact_resolver", lambda *_a, **_k: resolver)
    rc = selective_validation.run(
        {},
        {},
        base_rev="base",
        head_rev="head",
        repo_path="/repo",
        runner=runner,
    )
    return rc, resolver


def _run_unaffected_without_source(monkeypatch, attest, runner):
    monkeypatch.setattr(selective_validation.config, "attest_config", lambda *_: attest)
    monkeypatch.setattr(
        selective_validation, "impact_resolver", lambda *_a, **_k: _UnaffectedResolver()
    )
    monkeypatch.setattr(
        selective_validation.validation_ledger, "carry_key_verdict", lambda *_a, **_k: False
    )

    def absent(_entry, rev, key, cfg=None):  # noqa: ARG001
        return selective_validation.validation_ledger.KeyVerdict(
            key.name,
            rev,
            "cmd-hash",
            selective_validation.validation_ledger.KeyVerdictState.ABSENT,
        )

    monkeypatch.setattr(selective_validation.validation_ledger, "key_verdict", absent)
    return selective_validation.run(
        {},
        {},
        base_rev="base",
        head_rev="head",
        repo_path="/repo",
        runner=runner,
    )


def test_unaffected_required_key_without_source_runs_to_bootstrap_verdict(
    monkeypatch, capsys
) -> None:
    calls = []
    rc = _run_unaffected_without_source(
        monkeypatch,
        _attest({"name": "integration", "cmd": "just integration", "policy": "required"}),
        lambda cmd: calls.append(cmd) or 0,
    )

    assert rc == 0
    assert calls == ["just integration"]
    assert "integration: ran green (no qualifying source verdict)" in capsys.readouterr().out


def test_unaffected_optional_key_without_source_remains_unknown(monkeypatch, capsys) -> None:
    calls = []
    rc = _run_unaffected_without_source(
        monkeypatch,
        _attest({"name": "advisory", "cmd": "just advisory", "policy": "optional"}),
        lambda cmd: calls.append(cmd) or 0,
    )

    assert rc == 0
    assert calls == []
    out = capsys.readouterr().out
    assert "advisory: unknown (no qualifying source verdict)" in out
    assert "advisory: not required (optional unknown)" in out


def test_invalidated_key_reuses_qualifying_exact_tree_verdict(monkeypatch, capsys) -> None:
    calls = []
    ledger = selective_validation.validation_ledger

    def current(_entry, rev, key, cfg=None):  # noqa: ARG001
        return ledger.KeyVerdict(
            key.name,
            rev,
            "cmd-hash",
            ledger.KeyVerdictState.CURRENT,
            {"exit_code": 0, "verdict_confidence": "attested"},
        )

    monkeypatch.setattr(ledger, "key_verdict", current)
    monkeypatch.setattr(ledger, "is_qualifying_green", lambda record: record["exit_code"] == 0)
    rc, _resolver = _run(
        monkeypatch,
        _attest({"name": "unit", "cmd": "just unit"}),
        lambda cmd: calls.append(cmd) or 0,
    )

    assert rc == 0
    assert calls == []
    out = capsys.readouterr().out
    assert "unit: exact-tree verdict reused" in out
    assert "selective validation total:" in out


def test_green_full_gate_records_every_active_key(monkeypatch) -> None:
    attest = _attest(
        {"name": "docs", "cmd": "just attest-docs"},
        {"name": "unit", "cmd": "just attest-unit"},
        {
            "name": "paused",
            "cmd": "just attest-paused",
            "enabled": False,
            "disabled_reason": "maintenance",
        },
    )
    recorded = []
    monkeypatch.setattr(selective_validation.config, "attest_config", lambda *_: attest)
    monkeypatch.setattr(
        selective_validation.config,
        "validate_cmd",
        lambda *_a, phase=None, **_k: "just check-all-native" if phase else "just check-native",
    )
    monkeypatch.setattr(
        selective_validation.validation_ledger,
        "record",
        lambda _entry, rev, cmd, rc, **kwargs: recorded.append((rev, cmd, rc, kwargs)),
    )

    entry = {"work": {"validate": {"merge-main": "just check-all-native"}}}
    selective_validation.record_full_gate_keys(entry, {}, "head", "just check-all-native", 0)

    assert [(rev, cmd, rc) for rev, cmd, rc, _kwargs in recorded] == [
        ("head", "just attest-docs", 0),
        ("head", "just attest-unit", 0),
    ]
    assert all(kwargs["phase"] == "full-gate-key" for *_rest, kwargs in recorded)


def test_fast_or_red_gate_does_not_record_key_verdicts(monkeypatch) -> None:
    attest = _attest({"name": "unit", "cmd": "just attest-unit"})
    monkeypatch.setattr(selective_validation.config, "attest_config", lambda *_: attest)
    monkeypatch.setattr(
        selective_validation.config,
        "validate_cmd",
        lambda *_a, phase=None, **_k: "just check-all-native" if phase else "just check-native",
    )
    monkeypatch.setattr(
        selective_validation.validation_ledger,
        "record",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not record")),
    )

    entry = {"work": {"validate": {"merge-main": "just check-all-native"}}}
    selective_validation.record_full_gate_keys(entry, {}, "head", "just check-native", 0)
    selective_validation.record_full_gate_keys(entry, {}, "head", "just check-all-native", 1)


def test_unresolved_impact_defaults_to_byte_compatible_fallback(monkeypatch, capsys) -> None:
    key = {"name": "unit", "cmd": "just unit"}

    def once(attest):
        calls = []
        rc, resolver = _run(
            monkeypatch,
            attest,
            lambda cmd: calls.append(cmd) or 0,
            fallback_reason="pants: unavailable",
        )
        captured = capsys.readouterr()
        return (
            rc,
            resolver.seen,
            calls,
            _without_durations(captured.out),
            _without_durations(captured.err),
        )

    default = once(_attest(key))
    explicit = once(
        AttestConfig.model_validate({"keys": [key], "impact": {"on_unresolved": "fallback"}})
    )

    assert default == explicit
    assert default[:3] == (0, [("unit",)], ["just unit"])
    assert "IMPACT RESOLUTION FALLBACK" in default[4]
    assert "pants: unavailable" in default[4]


def test_unresolved_impact_strict_exits_before_any_key_runs(monkeypatch, capsys) -> None:
    calls = []
    attest = AttestConfig.model_validate(
        {
            "keys": [{"name": "unit", "cmd": "just unit"}],
            "impact": {"on_unresolved": "strict"},
        }
    )

    rc, resolver = _run(
        monkeypatch,
        attest,
        lambda cmd: calls.append(cmd) or 0,
        fallback_reason="pants: backend not available",
    )

    captured = capsys.readouterr()
    assert rc == selective_validation.UNRESOLVED_IMPACT_EXIT == 76
    assert rc not in {1, 2, 3, 75}
    assert resolver.seen == [("unit",)]
    assert calls == []
    assert captured.out == ""
    assert "STRICT MODE" in captured.err
    assert "pants: backend not available" in captured.err
    assert "No attestation key ran" in captured.err
    assert "running every attestation key" not in captured.err


def test_disabled_key_is_loudly_absent_from_resolution_execution_and_policy(
    monkeypatch, capsys
) -> None:
    calls = []
    rc, resolver = _run(
        monkeypatch,
        _attest(
            {
                "name": "stateful",
                "cmd": "just stateful",
                "policy": "required",
                "enabled": False,
                "disabled_reason": "host capacity incident bh-example",
            }
        ),
        lambda cmd: calls.append(cmd) or 1,
    )

    assert rc == 0
    assert calls == []
    assert resolver.seen == [()]
    assert "· stateful: DISABLED — host capacity incident bh-example" in capsys.readouterr().out


def test_expired_disable_reenables_fail_closed(monkeypatch, capsys) -> None:
    calls = []
    rc, resolver = _run(
        monkeypatch,
        _attest(
            {
                "name": "stateful",
                "cmd": "just stateful",
                "enabled": False,
                "disabled_reason": "past maintenance",
                "disabled_until": "2000-01-01T00:00:00Z",
            }
        ),
        lambda cmd: calls.append(cmd) or 0,
    )

    assert rc == 0
    assert calls == ["just stateful"]
    assert resolver.seen == [("stateful",)]
    assert "DISABLED" not in capsys.readouterr().out


def test_enable_disable_enable_restores_identical_execution(monkeypatch, capsys) -> None:
    base = {
        "name": "stateful",
        "cmd": "just stateful",
        "selectors": {"pants": "attest:stateful"},
    }

    def once(key):
        calls = []
        rc, resolver = _run(monkeypatch, _attest(key), lambda cmd: calls.append(cmd) or 0)
        output = _without_durations(capsys.readouterr().out)
        return rc, resolver.seen, calls, output

    before = once(base)
    disabled = once(
        base
        | {
            "enabled": False,
            "disabled_reason": "bounded maintenance",
            "disabled_until": "2999-01-01T00:00:00Z",
        }
    )
    after = once(
        base
        | {
            "enabled": True,
            "disabled_reason": "bounded maintenance",
            "disabled_until": "2999-01-01T00:00:00Z",
        }
    )

    assert before == after
    assert disabled[0] == 0 and disabled[1:] != before[1:]


def test_disabled_key_cannot_satisfy_all_keys_green(monkeypatch) -> None:
    attest = _attest(
        {
            "name": "stateful",
            "cmd": "just stateful",
            "enabled": False,
            "disabled_reason": "bounded maintenance",
        }
    )
    monkeypatch.setattr(selective_validation.config, "attest_config", lambda *_: attest)
    monkeypatch.setattr(
        selective_validation.validation_ledger,
        "key_verdicts",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("disabled proof was consulted")),
    )

    assert not selective_validation.all_keys_green({}, {}, "head")


# ---- bh-2kodj: cheap-first order, generated-evidence precheck, opt-in fail-fast ----------

_COSTS = {"docs": 9.0, "unit": 3.0, "stateful": 380.0, "integration": 325.0}
_CATALOG = (
    {"name": "docs", "cmd": "just docs"},
    {"name": "unit", "cmd": "just unit"},
    {"name": "stateful", "cmd": "just stateful"},
    {"name": "integration", "cmd": "just integration"},
)


def _ordered_run(monkeypatch, runner, *, fail_fast=False, precheck="", keys=_CATALOG):
    if fail_fast:
        monkeypatch.setenv(selective_validation.FAIL_FAST_ENV, "1")
    else:
        monkeypatch.delenv(selective_validation.FAIL_FAST_ENV, raising=False)
    monkeypatch.setattr(selective_validation, "historical_costs", lambda *_a: dict(_COSTS))
    attest = _attest(*keys)
    monkeypatch.setattr(selective_validation.config, "attest_config", lambda *_: attest)
    monkeypatch.setattr(selective_validation, "impact_resolver", lambda *_a, **_k: _Resolver())
    cfg = {"work": {"validate": {"precheck": precheck}}} if precheck else {}
    return selective_validation.run(
        {}, cfg, base_rev="base", head_rev="head", repo_path="/repo", runner=runner
    )


def test_cheapest_first_orders_measured_keys_then_unmeasured_in_configured_order() -> None:
    names = ("stateful", "new-a", "docs", "integration", "new-b", "unit")

    assert selective_validation.cheapest_first(names, _COSTS) == (
        "unit",
        "docs",
        "integration",
        "stateful",
        "new-a",
        "new-b",
    )
    assert selective_validation.cheapest_first(names, {}) == names


def test_historical_costs_map_ledger_medians_back_to_key_names(monkeypatch) -> None:
    keys = selective_validation.attest_keys(_attest(*_CATALOG))
    hash_of = {key.name: selective_validation.validation_ledger.cmd_hash(key.cmd) for key in keys}
    seen = {}

    def durations(hive, hashes):
        seen.update(hive=hive, hashes=set(hashes))
        return {hash_of["unit"]: 3.0, hash_of["stateful"]: 380.0}

    monkeypatch.setattr(selective_validation.validation_records, "command_durations", durations)
    monkeypatch.setattr("beadhive.registry.hive_dir", lambda _entry: "/hive")

    assert selective_validation.historical_costs({"repo": "x"}, keys) == {
        "unit": 3.0,
        "stateful": 380.0,
    }
    assert seen == {"hive": "/hive", "hashes": set(hash_of.values())}
    assert selective_validation.historical_costs({}, keys) == {}

    def unreadable(*_a):
        raise OSError("ledger gone")

    monkeypatch.setattr(selective_validation.validation_records, "command_durations", unreadable)
    assert selective_validation.historical_costs({"repo": "x"}, keys) == {}


def test_invalidated_keys_run_cheapest_first(monkeypatch, capsys) -> None:
    calls = []

    rc = _ordered_run(monkeypatch, lambda cmd: calls.append(cmd) or 0)

    assert rc == 0
    assert calls == ["just unit", "just docs", "just integration", "just stateful"]
    out = capsys.readouterr().out
    assert out.index("unit: ran green") < out.index("stateful: ran green")


def test_fail_fast_off_by_default_still_runs_every_key_after_a_red_one(monkeypatch, capsys) -> None:
    calls = []

    rc = _ordered_run(
        monkeypatch, lambda cmd: calls.append(cmd) or (1 if cmd == "just unit" else 0)
    )

    assert rc == 1
    assert calls == ["just unit", "just docs", "just integration", "just stateful"]
    assert "fail-fast" not in capsys.readouterr().out


def test_fail_fast_on_starts_no_further_key_after_a_red_required_key(monkeypatch, capsys) -> None:
    calls = []

    rc = _ordered_run(
        monkeypatch,
        lambda cmd: calls.append(cmd) or (1 if cmd == "just docs" else 0),
        fail_fast=True,
    )

    assert rc == 1
    assert calls == ["just unit", "just docs"]
    out = capsys.readouterr().out
    assert "docs: ran red (exit 1)" in out
    assert "integration: not run — fail-fast after docs (BH_VALIDATION_FAIL_FAST)" in out
    assert "stateful: not run — fail-fast after docs (BH_VALIDATION_FAIL_FAST)" in out


def test_fail_fast_tolerates_an_optional_unknown_key(monkeypatch) -> None:
    calls = []
    keys = (
        {"name": "unit", "cmd": "just unit", "policy": "optional"},
        {"name": "docs", "cmd": "just docs"},
    )

    rc = _ordered_run(
        monkeypatch,
        lambda cmd: calls.append(cmd) or (75 if cmd == "just unit" else 0),
        fail_fast=True,
        keys=keys,
    )

    assert rc == 0
    assert calls == ["just unit", "just docs"]


def test_fail_fast_switch_parses_only_explicit_truthy_values() -> None:
    enabled = selective_validation.fail_fast_enabled
    name = selective_validation.FAIL_FAST_ENV

    assert not enabled({})
    assert not enabled({name: ""})
    assert not enabled({name: "0"})
    assert not enabled({name: "false"})
    assert all(enabled({name: value}) for value in ("1", "true", "YES", " on "))


def test_precheck_runs_before_every_key_and_green_changes_nothing_else(monkeypatch, capsys) -> None:
    calls = []

    rc = _ordered_run(
        monkeypatch, lambda cmd: calls.append(cmd) or 0, precheck="just generated-evidence-check"
    )

    assert rc == 0
    assert calls[0] == "just generated-evidence-check"
    assert calls[1:] == ["just unit", "just docs", "just integration", "just stateful"]
    assert "precheck: ran green" in capsys.readouterr().out


def test_red_precheck_blocks_and_names_the_fix_but_default_mode_runs_every_key(
    monkeypatch, capsys
) -> None:
    calls = []
    precheck = "just generated-evidence-check"

    rc = _ordered_run(
        monkeypatch,
        lambda cmd: calls.append(cmd) or (1 if cmd == precheck else 0),
        precheck=precheck,
    )

    assert rc == 1
    assert calls == [precheck, "just unit", "just docs", "just integration", "just stateful"]
    out = capsys.readouterr().out
    assert "✗ precheck: ran red (exit 1)" in out
    assert "re-run the generator it names" in out


def test_red_precheck_with_fail_fast_starts_no_key(monkeypatch, capsys) -> None:
    calls = []
    precheck = "just generated-evidence-check"

    rc = _ordered_run(
        monkeypatch,
        lambda cmd: calls.append(cmd) or (1 if cmd == precheck else 0),
        precheck=precheck,
        fail_fast=True,
    )

    assert rc == 1
    assert calls == [precheck]
    out = capsys.readouterr().out
    assert out.count("not run — fail-fast after precheck") == len(_CATALOG)


def test_precheck_is_skipped_when_no_key_needs_to_run(monkeypatch, capsys) -> None:
    calls = []
    ledger = selective_validation.validation_ledger

    def current(_entry, rev, key, cfg=None):  # noqa: ARG001
        return ledger.KeyVerdict(
            key.name, rev, "cmd-hash", ledger.KeyVerdictState.CURRENT, {"exit_code": 0}
        )

    monkeypatch.setattr(ledger, "key_verdict", current)
    monkeypatch.setattr(ledger, "is_qualifying_green", lambda record: record["exit_code"] == 0)

    rc = _ordered_run(
        monkeypatch, lambda cmd: calls.append(cmd) or 1, precheck="just generated-evidence-check"
    )

    assert rc == 0
    assert calls == []
    assert "precheck" not in capsys.readouterr().out


def test_unconfigured_precheck_reads_as_empty() -> None:
    assert selective_validation.precheck_cmd({}, {}) == ""
    assert (
        selective_validation.precheck_cmd({}, {"work": {"validate": {"precheck": " just pre "}}})
        == "just pre"
    )


def test_precheck_and_reordered_keys_all_resolve_from_the_run_cfg(monkeypatch) -> None:
    """bh-931we x bh-2kodj: the precheck and every cheapest-first key run inside the selective
    run's cfg scope, so none of their git/ledger reads reloads config; the scope ends with it."""
    from beadhive import identity

    scoped = []
    rc = _ordered_run(
        monkeypatch,
        lambda cmd: scoped.append((cmd, identity._scoped_config.get() is not None)) or 0,
        precheck="just generated-evidence-check",
    )

    assert rc == 0
    assert scoped[0] == ("just generated-evidence-check", True)
    assert [cmd for cmd, _ in scoped[1:]] == [
        "just unit",
        "just docs",
        "just integration",
        "just stateful",
    ]
    assert all(in_scope for _, in_scope in scoped)
    assert identity._scoped_config.get() is None
