"""The ``beadlog.store`` composition root (beadlog-format-adr.md A6): every refusal says what to
do, the provider is imported only when selected and enabled, and no value or secret is echoed.

Provider rows are injected (a recording loader), so nothing here imports the real plugin unless a
test asks for it; the subprocess import test in ``tests/test_beadlog_store_import_isolation.py``
proves the real path never loads psycopg while the plugin is off.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from beadhive.beadlog_store_credentials import fnox_password_source
from beadhive.bootstrap import beadlog_store as root
from beadhive.bootstrap.beadlog_store import (
    BEADLOG_STORE,
    BUILTIN_STORE_PROVIDERS,
    BeadlogStoreProvider,
    BeadlogStores,
    credential_configured,
    store_names,
)
from beadhive.hq_sql_transport import SqlTransportError
from beadhive.modules.beadlog_store.contracts import (
    BeadlogStore,
    BeadlogStoreError,
    BeadlogStoreUnavailable,
    StoreSnapshot,
)
from beadhive.modules.config.contracts import BeadlogXtdbConfig

SECRET = "pw-" + "Zq9" * 8
REF = {"config_path": "/etc/fnox/fnox.toml", "profile": "xtdb", "key": "XTDB_PASSWORD"}


class FakeStore:
    store = "xtdb"

    def loader(self):
        raise AssertionError("not used")

    def snapshot(self, *, group, extracted_at):
        return StoreSnapshot("memory:", ())

    def close(self):
        pass


def recording(result=None, exc: BaseException | None = None):
    calls: list = []

    def loader(request):
        calls.append(request)
        if exc is not None:
            raise exc
        return FakeStore() if result is None else result

    return calls, (BeadlogStoreProvider("xtdb", "beadlog-xtdb", "beadhive[xtdb]", loader),)


def stores(plugins=None, plugin_kernel=None, providers=None, **kwargs) -> BeadlogStores:
    return BeadlogStores(
        plugins=plugins,
        plugin_kernel=plugin_kernel,
        providers=providers if providers is not None else recording()[1],
        **kwargs,
    )


ON = {"beadlog-xtdb": {"enabled": True}}


def test_the_capability_and_the_store_names() -> None:
    assert BEADLOG_STORE.render() == "beadlog.store@1"
    assert store_names() == ("bd", "xtdb")
    assert [(p.store, p.plugin_id, p.install_hint) for p in BUILTIN_STORE_PROVIDERS] == [
        ("xtdb", "beadlog-xtdb", "beadhive[xtdb]")
    ]


def test_the_default_is_disabled_and_nothing_is_imported_or_built() -> None:
    calls, providers = recording()
    with pytest.raises(BeadlogStoreUnavailable) as caught:
        stores(providers=providers).resolve("xtdb")
    message = str(caught.value)
    assert "--store xtdb needs the beadlog-xtdb plugin" in message
    assert "disabled (the default)" in message
    assert "bh config set plugins.beadlog-xtdb.enabled true" in message
    assert "beadhive[xtdb]" in message
    assert calls == []


@pytest.mark.parametrize(
    ("plugins", "plugin_kernel", "expected"),
    [
        ({"beadlog-xtdb": {"enabled": "true"}}, None, "disabled"),  # exact True only
        (ON, {"enabled": {"beadlog-xtdb": False}}, "disabled"),  # the kernel's false wins
        (
            {"beadlog-xtdb": {"enabled": True, "dsn": f"host=127.0.0.1 password={SECRET}"}},
            None,
            "plugins.beadlog-xtdb is invalid",
        ),
        (ON, {"enabled": "nope"}, "plugin_kernel is invalid"),
        (
            ON,
            {"capability_owners": {"beadlog.store@1": "someone-else"}},
            "'someone-else' is the selected owner",
        ),
    ],
)
def test_each_refusal_names_the_remedy_and_builds_nothing(plugins, plugin_kernel, expected) -> None:
    calls, providers = recording()
    with pytest.raises(BeadlogStoreUnavailable) as caught:
        stores(plugins, plugin_kernel, providers).resolve("xtdb")
    assert expected in str(caught.value)
    assert SECRET not in str(caught.value)
    assert calls == []


@pytest.mark.parametrize("name", ["bd", "nope"])
def test_bd_and_unknown_names_are_refused_here(name) -> None:
    with pytest.raises(BeadlogStoreUnavailable, match="--store must be one of bd, xtdb"):
        stores(ON).resolve(name)


def test_an_enabled_store_is_built_lazily_and_bound() -> None:
    calls, providers = recording()
    bound = stores(ON, providers=providers, password_source_for=lambda v: lambda: SECRET).resolve(
        "xtdb"
    )
    assert (bound.store, bound.plugin_id) == ("xtdb", "beadlog-xtdb")
    assert isinstance(bound.port, BeadlogStore)
    [request] = calls
    assert request.plugin_id == "beadlog-xtdb"
    assert request.settings["enabled"] is True
    assert request.settings["dsn"] == "host=127.0.0.1 port=5432 user=xtdb dbname=xtdb"
    assert request.password_source() == SECRET
    assert SECRET not in repr(request)


def test_a_missing_package_is_an_install_hint() -> None:
    _calls, providers = recording(
        exc=ModuleNotFoundError("No module named 'beadhive_beadlog_xtdb'")
    )
    with pytest.raises(BeadlogStoreUnavailable) as caught:
        stores(ON, providers=providers).resolve("xtdb")
    assert "its package is not installed" in str(caught.value)
    assert "uv sync --extra xtdb" in str(caught.value)


def test_a_factory_failure_names_only_the_exception_class() -> None:
    _calls, providers = recording(exc=ValueError(f"bad dsn {SECRET}"))
    with pytest.raises(BeadlogStoreUnavailable) as caught:
        stores(ON, providers=providers).resolve("xtdb")
    assert "could not be built (ValueError)" in str(caught.value)
    assert SECRET not in str(caught.value)


def test_a_store_that_does_not_conform_is_refused() -> None:
    _calls, providers = recording(result=object())
    with pytest.raises(BeadlogStoreUnavailable, match="does not implement beadlog.store@1"):
        stores(ON, providers=providers).resolve("xtdb")


def test_the_real_provider_row_builds_the_plugin_store_without_connecting() -> None:
    bound = BeadlogStores(plugins=ON, plugin_kernel=None).resolve("xtdb")
    assert type(bound.port).__name__ == "XtdbBeadlogStore"
    assert bound.port.target == "xtdb://xtdb@127.0.0.1:5432/xtdb"
    bound.port.close()


# -- the credential port -------------------------------------------------------------------


class Broker:
    def __init__(self, value=SECRET, exc=None):
        self.value, self.exc, self.calls = value, exc, []

    def get(self, reference, *, deadline):
        self.calls.append(reference)
        if self.exc is not None:
            raise self.exc
        return self.value


def test_no_fnox_reference_means_no_password_and_no_broker_call() -> None:
    broker = Broker()
    assert not credential_configured({"credential": {"profile": "x"}})
    assert fnox_password_source({"credential": {"profile": "x"}}, broker=broker)() is None
    assert broker.calls == []


def test_a_fnox_reference_is_resolved_only_when_called() -> None:
    broker = Broker()
    source = fnox_password_source({"credential": REF}, broker=broker)
    assert broker.calls == []
    assert source() == SECRET
    assert broker.calls == [REF]


def test_a_broker_failure_is_a_static_store_error() -> None:
    broker = Broker(exc=SqlTransportError("fnox credential unavailable"))
    with pytest.raises(BeadlogStoreError) as caught:
        fnox_password_source({"credential": REF}, broker=broker)()
    assert str(caught.value) == (
        "the XTDB password could not be resolved: fnox credential unavailable"
    )


# -- the config model ---------------------------------------------------------------------


def test_the_fragment_defaults_are_off_and_loopback() -> None:
    config = BeadlogXtdbConfig()
    assert config.enabled is False
    assert config.dsn == "host=127.0.0.1 port=5432 user=xtdb dbname=xtdb"
    assert config.image.startswith("ghcr.io/xtdb/xtdb@sha256:a6a4265")
    assert (config.data_dir, config.credential.key) == ("", "")
    assert config.health_url == "http://127.0.0.1:8080/healthz/alive"
    assert root.STORE_FRAGMENT_MODELS["beadlog-xtdb"] is BeadlogXtdbConfig


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ({"dsn": f"host=127.0.0.1 password={SECRET}"}, "must not embed a password"),
        ({"dsn": f"host=127.0.0.1 PASSWORD = {SECRET}"}, "must not embed a password"),
        ({"dsn": f"postgresql://xtdb:{SECRET}@127.0.0.1/xtdb"}, "must not embed a password"),
        ({"dsn": f"postgresql://127.0.0.1/xtdb?password={SECRET}"}, "must not embed a password"),
        ({"image": "ghcr.io/xtdb/xtdb:latest"}, "pinned by digest"),
        ({"data_dir": "relative/dir"}, "absolute path"),
        ({"health_url": f"http://u:{SECRET}@127.0.0.1:8080/"}, "without userinfo"),
        ({"enabled": "true"}, "valid boolean"),
        ({"password": SECRET}, "Extra inputs"),
    ],
)
def test_the_fragment_refuses_secrets_tags_and_relative_paths(values, expected) -> None:
    with pytest.raises(ValidationError) as caught:
        BeadlogXtdbConfig.model_validate(values)
    assert expected in str(caught.value)
    assert SECRET not in str(caught.value)
