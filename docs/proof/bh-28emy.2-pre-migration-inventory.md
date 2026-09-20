# `bh-28emy.2`: shared-server pre-migration inventory

Captured on `beadhive-factory` on 2026-09-20 before any schema migration performed by this
initiative. The sweep discovered that other clients/remotes had already migrated some databases
to v66; their exact last-v62 parent refs are recorded below rather than falsely calling their
capture-time v66 heads "pre-migration" refs. This artifact is deliberately per **database**, not
per registered hive.

## Boundary and tools

- Shared store: `~/.beads/shared-server/dolt/`, endpoint `127.0.0.1:3310`.
- Installed client: Beads `1.3.0` at commit
  `f45b249ce6b40ba62aecc03949e6371e8f7c79d8`.
- Installed standalone Dolt: `2.3.5`.
- The long-lived server process is still the pre-upgrade Dolt `2.2.3` process at capture time.
  That is intentional: this bead publishes the pre-migration state. The designated-migrator
  gates below stop that process and prove its 2.3.5 replacement before schema migration.
- No `bd compact`, `bd flatten`, raw `dolt push`, or `dolt gc` was run.
- Local Dolt refs and schema versions came from fully qualified, read-only `dolt_log` and
  `schema_migrations` queries. Remote verification
  uses `git ls-remote <canonical remote> 'refs/dolt/*'`. The latter reports the Git transport
  object's SHA-1, not the local Dolt commit hash, so both values are retained.
- Remote URLs below are canonicalized; dead absolute scratch paths are redacted.

## Database inventory

`managed` means the database has a checkout that `bh` can route to the shared server.
`local-only` is a final classification with its reason stated in the table. Where a database
was already v66, the rollback column is the parent of its first v63 migration commit; two values
for `bhui` expose two historical v63 lineages rather than hiding that divergence.

| database | last-v62 / rollback Dolt ref | canonical remote | pre-push `refs/dolt/data` | classification / managed route |
|---|---|---|---|---|
| `_HUB_ISSUES_NO_IDS` | `vqhjimnqijkvsahdv0b6s2u58ofkapbh` | none | none | local-only: synthetic hub-ingest database, no remote or hive |
| `ag_cp` | `d34s6pc3mtjamdbsljgbov0916guc63r` | `github:agentguides/claude-plugin` | `22160a109231ee3acea629a22ed63ad0b966570b` | managed via `ag-cp`; pushed and verified |
| `ag_hp` | `61qjbap5err11b5ojjp8e3jnpbtu2ojj` | `github:agentguides/hermes-plugin` | `c65179c6cdc2169b5ad715af5e12d27c7e04e531` | managed via `ag-hp`; pushed and verified |
| `ag_infra` | `h84epi9k5tqjnorkclce4dpdqj0h0hhm` | `github:agentguides/infra` | `a51cd0aec92ab611e1af65011006ceb4ef365971` | managed via `ag-infra`; pushed and verified |
| `ag_run` | `03dp6cs94651du5ml42isjbh5uo04134` | `github:agentguides/runtime` | `e1ae578b57c8adf4d7df2eb0f66813deb3b5f17c` | managed via `ag-run`; pushed and verified |
| `agf` | `vvf2igcvo65amfou78oulgg61d9ohc1e` | `github:briancripe/agentic-git-flow` | `af63e80ab06dbef7fdc68d013fa7d4aafe3147f7` | managed via `agf`; pushed and verified |
| `ah` | `0e3ftqn7sgs0h2b99bsra1ur6gut77ju` | `github:briancripe/agent-hitch` | `582919119d911181d705f42634e8f4f2f4bc691d` | managed via `ah`; pushed and verified |
| `bbox` | `at3hmh9o2deslep9jfrvg2tpsfue1s02` | `github:briancripe/baml-box` | `3851221b7edc4c47f4ca9f95f163aa82abca843e` | managed via `bbox`; already v66 at capture (`bj05rct5jkmjsg99beoui8ihah5luntt`), pushed and verified |
| `bd_0xl0c1` | `umlmgbrmm037oe4kq418l6esamm1tenh` | `github:LincForge/0xl0c1` | `a19bbdffacf6686d98b983fb9c07825c3a3926cf` | local-only: unregistered database; no managed `bh bd` route; already v66 at capture (`5k7bf7og38ouq4oi7p620tnvh7qkg4nb`) |
| `beads` | local `8bl5ioga7t8c1cb4b16i88q5dkh10coo`; remote lineage `oqttl2doc3nqb2ql7rlm6s1giet5c8ao` | `github:beadhive/beadhive-gateway` | `c8bcbb77d6506ce7ebd3e27b4737519f27405d7b` | managed via `bh-gw`; reconciled local v62 with remote v66, pushed and verified. The herdr-plugin checkout also names `beads`, but is embedded and is not this shared database |
| `beads_global` | `1sdqucs18c81ai8f34hnt5f6gu0tnra1` | none | none | local-only: Beads global auxiliary database, no hive or remote |
| `bex` | `kq6f8arq8f29rkd4t498m6eldvo5hft4` | redacted dead scratch-file remote | unavailable | local-only: orphaned experiment; configured local path no longer exists |
| `bh` | `a1f2b6tqk20ivido1nhkfti865qt6tmb` | `github:beadhive/beadhive` | `20fc6d319739b12eb2e7cfabbfbc56a9fe75ddf8` | managed via `bh`; already v66 at capture (`jotbln8b36gne8ltgcui4uhq45p7911t`), pushed and verified |
| `bh_app` | `f389q0b971i29m68dk9uae1fjgbspu96` | `github:beadhive/beadhive-app` | `4886eb9e1836bc4c366c6e1cfcd8bbb969fa9975` | managed via `bh-app`; fast-forwarded to remote v62, pushed and verified |
| `bh_baml` | `fjom425vvrt856cvsnptq3v01s7pbub9` | `github:beadhive/baml-harness` | `9c70ef32cdaa0ab2e64041fafb09ede40b9515dc` | managed via `bh-baml`; already v66 at capture (`p4ben03jo2idr8p0e2h27h909egcgjd2`), pushed and verified |
| `bh_cp` | `cce31t01jj3n2qqb3c6c88qmru724f6u` | `github:beadhive/claude-plugin` | `2f5bda04812d7d4b30f1ddaeb77a3b9818227f2a` | managed via `bh-cp`; pushed and verified |
| `bh_frame` | `orl79pnqiieg780g4ffl7g8rqarogfel` | `github:beadhive/frame` | `f6e6bd3eb275da2ef69181ab2e4fc9f3402c5145` | managed via `bh-frame`; pushed and verified |
| `bh_infra` | local `c75ve24dh9f812dlgoslbu3g3c5f9ovj`; remote lineage `1012h3gk3if1t6qkhsaj8rm34ofmn4l4` | `github:beadhive/infra` | `2ae7fa45878f83d69c8a6e443df38adf55daa75c` | managed via `bh-infra`; adopted remote v66, pushed and verified |
| `bh_ndl` | `1iaa3t9iahvqcpvqmlkvj9fv2fgp1ekc` | `github:beadhive/cactus-needle-shim` | `0df4bfe4c9f29eb47a531a3304eefca73110f2ae` | managed via `bh-ndl`; pushed and verified |
| `bh_oci` | `j7sm2scec0g0poe64m4s1qlmianlcrpu` | `github:beadhive/containers` | `f705bb198ecbe4475cac73ff2a5c083589a106ff` | managed via `bh-oci`; pushed and verified |
| `bhui` | `6tk1p87bulmibg1e4cc25af29uda3kel`; second lineage `7a6psjdi9ouftcut8jqbogoatngfbe15` | `github:beadhive/beadhive-ui` | `3fb348a0f912ddf92d06393d044d9b413170ca89` | managed via `bhui`; already v66 at capture (`dovvm43e1aos3b1993jp8ppjiqfjr5oi`), reconciled to `ccqa9ogmq635uu7es0go4tv42knn23d2`, pushed and verified |
| `bvend` | `3kh4lvcl1cnri57g3biaibalaa8oeilr` | `github:briancripe/baml-vendor` | `b6bbc1ae2690bc0131a1c29395513c36930fa0cb` | managed via `bvend`; already v66 at capture (`vaioa87fpgj15km50a3dqcck9o78fm9l`), pushed and verified |
| `dxnvh` | `k9qv5qkigcd7ctr1qg6vpa4pp77aoaor` | `github:ric03uec/dell-x-nvidia-hackathon` | `5b6536f081f4fc2f2058d07f42e22e1fb9fa91d3` | local-only on this shared server: registered remote-only hive has no workspace checkout; `bh --hive dxnvh` skips, and its hydrated cache uses a separate server/store |
| `exh` | `35vtkekfafoc65qqn8vjtuu5uqe6ha0l` | redacted dead scratch-file remote | unavailable | local-only: orphaned experiment; configured local path no longer exists |
| `hl` | `1qgomfqdekgu1r60krdn284rk28e3i1j` | `github:briancripe/homelab` | `5bb54ec1fed295d4c3c5f6c4141ca8aeb2f68e25` | local-only on this shared server: registered remote-only hive has no workspace checkout; `bh --hive hl` skips, and its hydrated cache uses a separate server/store |
| `hq` | `690n8quorhaos3g9tme9vq6poibs3q4a` | `github:briancripe/beadhive-hq` | `139d16d946719a775850a9b114c633f8f6f89584` | managed via `bh hq bd`; reconciled by merge at v62, pushed and verified |
| `jsmd` | `t826gec0jcjkboj73s3rpckg4mb9j70m` | `github:briancripe/job-site-memory` | `6b29699a1feff9be060640ceaf9415d1c7112940` | local-only: unregistered database; no managed `bh bd` route; already v66 at capture (`anqm74d8uqlrkb0rphbkp7m2sbutnoim`) |
| `jsmm` | `fkakeh608lfqjluo5jb3lhfotrlk0d94` | `github:briancripe/job-site-memory-mobile` | `5d8775b28b72e41e4a72f0fcc688cfea5f2fcb1e` | local-only: unregistered database; no managed `bh bd` route; already v66 at capture (`mbhluirivehpsd5ksg6l1v20o7kjl408`) |
| `nvhack` | `0u509sa1atspo3dukbbc7cpjdngusaal` | `github:briancripe/nvidia-hackathon` | `2c16dbd653debba6d30bef64218c0441497f581e` | managed via `nvhack`; pushed and verified |
| `obs` | `5m4qng6knfccl2l744r5gqm5m8uncl08` | `github:briancripe/observaloop` | `bd801efb8fd6b75b1ffd00f2e1c1b270c014a00c` | managed via `obs`; pushed and verified |
| `rst` | `h5p2099b4fb1qrp3bq3ff9qmjlu3oh2t` | none | none | local-only: unregistered orphan database with no configured remote |
| `sgen` | `7pcijot4icor4s12plkc1pc2f8r7hefr` | `github:briancripe/sampler-gen` | absent | managed via `sgen`; first push created `refs/dolt/data`, pushed and verified |
| `unp` | `c1pagt5sooq07svua3bjqs83n18nr1nl` | redacted dead scratch-file remote | unavailable | local-only: orphaned experiment; configured local path no longer exists |

System schemas `dolt`, `information_schema`, and `mysql` were enumerated but are not user
databases and have no publishable bead history.

## Post-push verification

Every row below is the output pair required by the acceptance criterion: the reconciled local
Dolt head/schema plus a fresh `git ls-remote <remote> 'refs/dolt/data'` result after the managed
push returned success.

| database | published local head | schema | post-push Git transport SHA |
|---|---|---:|---|
| `ag_cp` | `d34s6pc3mtjamdbsljgbov0916guc63r` | 62 | `22160a109231ee3acea629a22ed63ad0b966570b` |
| `ag_hp` | `61qjbap5err11b5ojjp8e3jnpbtu2ojj` | 62 | `c65179c6cdc2169b5ad715af5e12d27c7e04e531` |
| `ag_infra` | `h84epi9k5tqjnorkclce4dpdqj0h0hhm` | 62 | `a51cd0aec92ab611e1af65011006ceb4ef365971` |
| `ag_run` | `03dp6cs94651du5ml42isjbh5uo04134` | 62 | `36392cda8d784a360c63128f3cc86883a3295161` |
| `agf` | `vvf2igcvo65amfou78oulgg61d9ohc1e` | 62 | `af63e80ab06dbef7fdc68d013fa7d4aafe3147f7` |
| `ah` | `0e3ftqn7sgs0h2b99bsra1ur6gut77ju` | 62 | `582919119d911181d705f42634e8f4f2f4bc691d` |
| `bbox` | `bj05rct5jkmjsg99beoui8ihah5luntt` | 66 | `3851221b7edc4c47f4ca9f95f163aa82abca843e` |
| `beads` | `2q5eh279ekoo13k7274ss1tmnqc5q7lh` | 66 | `757f3868662d04a1c5da1885a4f63c04897d60c4` |
| `bh` | `jotbln8b36gne8ltgcui4uhq45p7911t` | 66 | `17b094014f97d9ed591f1a6b9584c8aeefc2a33f` |
| `bh_app` | `66mkmdfiatulca9ssb567k59eqt83jnc` | 62 | `4886eb9e1836bc4c366c6e1cfcd8bbb969fa9975` |
| `bh_baml` | `p4ben03jo2idr8p0e2h27h909egcgjd2` | 66 | `f93766d975d41e20384ec3b2de89d680bf210949` |
| `bh_cp` | `cce31t01jj3n2qqb3c6c88qmru724f6u` | 62 | `faccb4d46c27bc688b4b6c0b5c6cfc63cfe6bc09` |
| `bh_frame` | `orl79pnqiieg780g4ffl7g8rqarogfel` | 62 | `f6e6bd3eb275da2ef69181ab2e4fc9f3402c5145` |
| `bh_infra` | `nskpe84frk08lq2rg046rbi0g10a8lr8` | 66 | `2ae7fa45878f83d69c8a6e443df38adf55daa75c` |
| `bh_ndl` | `1iaa3t9iahvqcpvqmlkvj9fv2fgp1ekc` | 62 | `0df4bfe4c9f29eb47a531a3304eefca73110f2ae` |
| `bh_oci` | `j7sm2scec0g0poe64m4s1qlmianlcrpu` | 62 | `f705bb198ecbe4475cac73ff2a5c083589a106ff` |
| `bhui` | `ccqa9ogmq635uu7es0go4tv42knn23d2` | 66 | `3fb348a0f912ddf92d06393d044d9b413170ca89` |
| `bvend` | `vaioa87fpgj15km50a3dqcck9o78fm9l` | 66 | `55fabc628e9353edef055648aa0a73742f3841d8` |
| `hq` | `ppjhu8bc4jhfcb498fcf1nddr5alcnic` | 62 | `006ba914eb23fee001a6c2d34daf6fa2e4257bc5` |
| `nvhack` | `0u509sa1atspo3dukbbc7cpjdngusaal` | 62 | `2c16dbd653debba6d30bef64218c0441497f581e` |
| `obs` | `5m4qng6knfccl2l744r5gqm5m8uncl08` | 62 | `bd801efb8fd6b75b1ffd00f2e1c1b270c014a00c` |
| `sgen` | `7pcijot4icor4s12plkc1pc2f8r7hefr` | 62 | `996b387d1e9d0451bc2158c05bc207de2ce56a94` |

## Client inventory and compatibility disposition

| client / install plane | observed or declared state | migration disposition |
|---|---|---|
| `beadhive-factory`, uid 8335, ordinary shell | Beads 1.3.0 and Dolt 2.3.5 from Nix profile generation 3 | designated migration host; keep all writers stopped during the gate |
| `beadhive-factory`, uid 8335, service context | same Nix store paths, proven by the `bh-28emy.1` transient-service probe after `bh-qn9zt` | permitted only after parity is rechecked immediately before migration |
| current shared daemon | Dolt 2.2.3, started before generation 3 | pre-migration reads/pushes only; must be stopped and replaced by a proven 2.3.5 process before migration |
| second developer/Homebrew plane (`bh-tp38g`) | may resolve a different `bd` while sharing state; not installed on this Linux host | no access from the stop gate until it reports exact Beads 1.3.0 parity; no silent accepted skew |
| agent-computer image (`bh-2lccw`) | declared Beads 1.1.2, understands at most schema v53 | explicitly quarantined from the shared store; temporary post-v66 breakage is accepted until `bh-2lccw` upgrades the image |
| cache-local servers for `ag-cp`, `hl`, and `dxnvh` | separate Dolt 2.2.3 processes and separate stores | not clients of this shared datadir, but must not publish competing remote state from the stop gate through final publish verification |

## The one designated migrator

The sole designated migrator is **`beadhive-factory` host
`6ae345b9-81a8-4c9b-8661-c5a4420fc12d`, account `bees` (uid 8335), using the managed Nix
profile and the `bh` checkout**. A second host, clone, container, or install plane is not
authorized to run `bd migrate schema` for this migration.

The migration bead must execute these gates in order:

1. **Stop:** stop every writer to the shared server; quarantine the Homebrew/dev plane and the
   Beads 1.1.2 agent image; stop competing cache publishers. Prove no writer remains.
2. **Verify parity and rollback refs:** re-read every table row above, require all managed push
   rows to be complete, and prove ordinary and service contexts resolve Beads 1.3.0. A missing
   recorded pre-migration ref is a hard stop.
3. **Replace the daemon:** stop the Dolt 2.2.3 shared daemon through its managed lifecycle,
   start it once from the generation-3 profile, and prove the running process path is Dolt 2.3.5.
4. **Migrate once:** from this host/account/context only, run one explicit `bd migrate schema`.
   No second clone may independently consent.
5. **Verify:** run `bd doctor`, verify schema and issue-count invariants for every managed
   database, and keep writers stopped on any discrepancy.
6. **Publish:** publish every migrated managed database through `bh bd dolt push` (HQ through
   `bh hq bd dolt push`) and bind each successful command to a fresh
   `git ls-remote <remote> 'refs/dolt/*'` result.
7. **Resume:** resume only Beads 1.3.0-compatible clients. The quarantined 1.1.2 image remains
   intentionally unusable against v66 until its own bug is fixed.

## Publication execution notes

The operator explicitly approved publishing these 22 databases. All pushes ran through a
managed Beadhive route with `BEADS_FSCK_TIMEOUT=900s`; no raw Dolt push was used.

The final Beads 1.3.0 client correctly refused to open a v62 remote-backed database with four
pending migrations. The retained immutable Beads `1.1.0 (dev)` build
`/nix/store/1x65ynykhanfwgzcbyrkp71nlkgq88rx-beads-HEAD-50763fc/bin/bd`, which natively tops
out at v62, was therefore used for the pre-migration v62 pulls/pushes. Existing v66 databases
were pulled/pushed with Beads 1.3.0. This is the required old-client-first ordering, not a schema
override: neither `--ignore-schema-skew`, a migration environment override, nor a migration
command was used.

Several remotes were ahead. They were reconciled with normal managed `bh ... bd dolt pull`
operations; no force-push was used. `beads` and `bh_infra` thereby adopted v66 histories that
already existed remotely. `sgen` had no remote Dolt branch, so its managed push created the
first `refs/dolt/data`. All 22 pushes returned success and all 22 post-push `ls-remote` probes
returned a non-empty `refs/dolt/data` SHA.
