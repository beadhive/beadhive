# Herdr compatibility removal ledger

Status: active compatibility facade

Owner: `bh-5wuc0 — Agents module: extract provider-neutral launch policy and the Herdr adapter`

## Target boundary

`modules/agents` owns launch policy, transaction replay, terminal authority, generation checks,
and the `AgentLauncher`, observation, recovery, and teardown ports. `integrations/herdr` owns
provider transport, topology identity, capability binding, and implementations of those ports.
`integrations/herdr/cli_application.py` is the bounded application compatibility owner.
`integrations/herdr/cli.py` and `herdr_views` are presentation adapters. None of those adapters may
complete Beadhive work or create, release, or seize work authority implicitly.

The typed provider capability is `agent.session@1`, declared by the built-in Herdr manifest and
bound by `herdr_agent_provider_binding`. The production CLI composition creates that binding and
passes every command through `HerdrCliApplication`; the retained application callbacks continue to
call native Beadhive work authority explicitly rather than hiding authority mutation in the
provider adapter. Portable results contain only correlation IDs, redacted provider identity,
dispositions, and generations. Raw prompts, argv, credentials, environment variables, provider
error text, and host paths remain local.

## Retained compatibility surface

`beadhive.herdr_plugin` remains the source-compatible import and composition facade for one release
cycle. It aliases the application module so supported monkeypatch targets remain the same mutable
globals. The following surface is intentionally retained:

- `PLUGIN` and `cli` for plugin/bootstrap composition;
- the historical `plugin herdr` command paths, help, exit codes, human output, and JSON schemas;
- the monkeypatch seams frozen in
  `docs/design/agent-launch-boundary-characterization.md`; and
- the public presentation hooks `active_session_name`, `session_scoped`, `cli_available`,
  `session_snapshot`, `roster_payload`, `integration_ready`, `server_up`, and `supported_kinds`.

`herdr_views` may call only those named public hooks. It must not import or call a leading-underscore
member of `herdr_plugin`. New application or integration consumers must use `modules/agents`,
`integrations/herdr`, or the typed plugin capability instead of adding another facade dependency.

## Removal gates

The facade is removable only when all of these are simultaneously true:

1. every launch/observe/recover/teardown caller is composed through `agent.session@1`;
2. CLI and view snapshot tests pass against the adapter-facing use cases without patching a
   private `herdr_plugin` name (the current projection proof still exercises the compatibility
   alias, so this gate is not yet met);
3. the exact `herdr_views -> herdr_plugin` cycle edge and the plugin-registry compatibility edge
   are absent from the import-boundary graph and exception ledger;
4. the supported public import, command-help, human-output, JSON-schema, and exit-code contract has
   completed one release cycle without an untracked consumer; and
5. `plugin.herdr` closure ownership points only at the manifest, integration, CLI adapter, and
   compatibility tests.

Until then, the facade is deprecated but supported. The removal gate does not authorize renaming
commands, weakening generation or identity checks, deleting characterization tests, or moving
work authority into the provider adapter.

## Explicit non-goals

This ledger does not claim selective-CI safety, external consumer completeness, or permission to
manage native Claude Task/Codex collaboration children. It does not move Nix/Herdr installation
ownership, and it does not make optional provider availability a Beadhive startup requirement.
