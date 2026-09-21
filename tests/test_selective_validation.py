from __future__ import annotations

from beadhive import selective_validation
from beadhive.modules.config.contracts import AttestConfig
from beadhive.modules.work.domain.impact import ImpactReceipt, KeyEvidence


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


def _attest(*keys):
    return AttestConfig.model_validate({"keys": list(keys)})


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
        return rc, resolver.seen, calls, captured.out, captured.err

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
        return rc, resolver.seen, calls, capsys.readouterr().out

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
