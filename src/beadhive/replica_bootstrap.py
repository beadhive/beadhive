"""replica_bootstrap.py — bootstrap a hive replica on a busy executor (bh-pjhwe).

``bd bootstrap`` takes bd's workspace gates EXCLUSIVELY before it touches anything
(bd ``cmd/bd/bootstrap.go`` ``executeBootstrapPlan`` → ``acquireExclusiveWorkspaceGates``). In
shared-server mode (``BEADS_DOLT_SHARED_SERVER=1``) one of those gates is the PHYSICAL-ROOT gate
of bd's shared Dolt data directory (``doltserver.ResolveDoltDir`` returns the shared dir), and
every long-running bd process on the host — every other hive's bd server — holds that same gate
SHARED for its whole lifetime. So on a busy executor the bootstrap cannot get it: bd polls for a
bounded window (``exclusiveGateWait``, 5s in bd 1.3.0), then refuses with "refuses to run over
live bd activity". Before this module, the only way through was stopping every other hive's bd
server.

What the gate protects is a database that live processes might be using. A replica bootstrap
creates a database that does NOT exist yet, so nothing live can be using it, and the clone itself
runs inside the shared ``dolt sql-server`` (``CALL DOLT_CLONE`` — bd's ``cloneViaServer``), which
serialises its own writes. So when — and only when — the gated attempt is refused for that
reason AND the target database is verifiably absent from the shared server, the bootstrap is
re-run once in GATE-FREE form: shared-server mode is not activated for that one invocation (so
bd's physical-root gate resolves to the hive's own, uncontended ``.beads/dolt``), while the
store's ``dolt_mode: "server"`` metadata plus an explicit server endpoint keep the clone routed
through the running shared server into the same database. Nothing on the host is stopped or
restarted. The window is bd's own bounded one: bh never adds a wait or a retry loop of its own.

Shared-server mode has two switches in bd (``doltserver.IsSharedServerMode``): the
``BEADS_DOLT_SHARED_SERVER`` env var, and the ``dolt.shared-server`` key of the hive's tracked
``.beads/config.yaml`` — which a furnished hive may set to ``true``. Dropping the env var alone
leaves the config key on, so the gate would still resolve to the shared dir (bh-jktzj). The key is
read through bd's viper config (``config.GetBool``), which binds every key to a ``BD_``-prefixed
env var with ``.``/``-`` mapped to ``_`` and gives env precedence over config files
(``internal/config/config.go`` ``SetEnvPrefix("BD")`` / ``AutomaticEnv``; pinned by bd's own
``TestConfigPrecedence``). So the gate-free attempt sets ``BD_DOLT_SHARED_SERVER=false`` — an
override scoped to that one bd process; the tracked file is never touched. ``BEADS_DOLT_DATA_DIR``
is dropped as well: bd consults it first for the hive's own dolt dir
(``doltserver.projectDoltDirPath``), so an override aimed at the shared dir would put the gate
straight back on it, and the server clone (``CALL DOLT_CLONE``) never uses it to place data.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from . import dolt_health, store_locator

#: bd's refusal when ``bd bootstrap`` cannot take its exclusive workspace gates (bd 1.3.0,
#: ``cmd/bd/bootstrap.go``). A substring match on bd's own wording is the only signal bd gives.
#: Pinned to the supported bd range: tests/test_replica_bootstrap_refusal_contract.py.
GATE_BUSY_MARKER = "refuses to run over live bd activity"

#: bd's own env switch for shared-server mode — dropped from the gate-free attempt.
SHARED_SERVER_ENV = "BEADS_DOLT_SHARED_SERVER"
#: bd's env switch selecting the external-server clone path when no config is loaded yet.
SERVER_MODE_ENV = "BEADS_DOLT_SERVER_MODE"
#: bd's viper env binding of the tracked ``dolt.shared-server`` config key (``BD_`` prefix,
#: ``.``/``-`` → ``_``); env beats ``.beads/config.yaml``, so ``false`` here turns the key off.
SHARED_SERVER_CONFIG_ENV = "BD_DOLT_SHARED_SERVER"
#: bd's env override of a hive's own dolt data dir — dropped from the gate-free attempt.
DATA_DIR_ENV = "BEADS_DOLT_DATA_DIR"


def gate_refused(result) -> bool:
    """Whether ``result`` (a CompletedProcess-shaped object) is bd's exclusive-gate refusal."""
    if getattr(result, "returncode", 1) == 0:
        return False
    text = f"{getattr(result, 'stdout', '') or ''}\n{getattr(result, 'stderr', '') or ''}"
    return GATE_BUSY_MARKER in text


def gate_free_env(env: Mapping[str, str]) -> dict[str, str]:
    """``env`` for the gate-free attempt: shared-server mode NOT activated for this one
    invocation — neither by bd's env switch nor by a tracked ``dolt.shared-server: true`` (its
    ``BD_`` env binding forced ``false``) — no data-dir override that could aim the gate back at
    the shared dir, external-server mode on, and the shared server's endpoint pinned explicitly
    (``dolt_health.server_endpoint``, bd's own resolution order) so the clone still lands on the
    running shared server. An endpoint the caller already set is kept."""
    out = {k: v for k, v in env.items() if k not in (SHARED_SERVER_ENV, DATA_DIR_ENV)}
    out[SHARED_SERVER_CONFIG_ENV] = "false"
    host, port = dolt_health.server_endpoint()
    out.setdefault(dolt_health.ENV_SERVER_HOST, host)
    out.setdefault(dolt_health.ENV_SERVER_PORT, str(port))
    out[SERVER_MODE_ENV] = "1"
    return out


def run_through_gate(
    attempt: Callable[[dict], object],
    env: Mapping[str, str],
    database: str | None,
    *,
    note: Callable[[str], None] | None = None,
):
    """Run ``attempt(env)`` (one ``bd bootstrap``); on bd's exclusive-gate refusal for a
    database that does not exist yet on the shared server, run ``attempt`` once more in
    :func:`gate_free_env` form. Returns the last result.

    Never falls back when ``database`` is unnamed (bd would pick its generic default) or already
    present (the gate may then be protecting live users of it) — the gated refusal stands."""
    result = attempt(dict(env))
    if not gate_refused(result) or not database:
        return result
    if store_locator.has_direct_server_database(database):
        return result
    if note is not None:
        note(
            f"• beads: other bd processes hold the shared Dolt gate; {database!r} does not exist "
            "yet, so it is bootstrapped through the running shared server without that gate "
            "(no bd server is stopped)"
        )
    return attempt(gate_free_env(env))
