# Dedicated HQ configuration database (fresh v3)

These resources describe a **new, empty** Dolt configuration database. They do not
change an existing database, Beads store, frame account, or HOST selector. The
three configuration tables and their grants are the contract used by the public
first-publication initializer and its isolated integration fixture.

Run `uv run --no-sync python ops/hq-config/provision.py --reader-host <exact-client-ip> --local-port <server-local-port>` to review the rendered SQL. On the Dolt server, an operator can use `--apply --server-hostname <this-hostname> --reader-password-file <mode-0600-file> --publisher-password-file <mode-0600-file> --root-password-file <mode-0600-file>` after reviewing the listener, accounts, and output. `--root-passwordless` is an explicit server-local alternative when that root account is already configured; the script never guesses it. The CLI suppresses captured SQL output on failure because account creation statements contain credentials. An existing database causes refusal, including partially provisioned state: inspect and repair it separately.

The publisher account can modify the three versioned tables and invoke `DOLT_ADD` and `DOLT_COMMIT`. Those procedures give it branch-wide staging/commit power, so it is a trusted operator capability and must never be given to frames. The reader receives only `SELECT` on the three tables. The publisher additionally reads the exact status/history relations needed to prove an immutable original seed receipt. The main branch-control grants do not extend to the existing `frame_hq_proto` account. Verify effective positive and negative operations with separate accounts; `validation.sql` names the server-local evidence to capture.

The v3 `initial_attempt_nonce` is a physical-attempt discriminator. A synchronized Dolt 2.3.5 first-publication race with v2 equal singleton writes produced two commits despite one logical publication UUID; a different nonce in each physical attempt makes the loser conflict. The public initializer verifies the exact v3 type, length, nullability, collation, and single-column uniqueness contract **before** any first DML. Existing v1/v2 stores are readable and ordinary publication preserves their storage version; there is no automatic `ALTER` or migration here. Storage v3 is independent of settings `SCHEMA_VERSION = 1`.

The empty committed schema is not usable configuration authority. Publication needs a reviewed source plan, stable journal UUID, exact schema HEAD, and committed readback. A later rollback needs the **latest** current SQL snapshot exported to a signed Git revision before the HOST selector changes. Keep one backend writable at a time; a local lock cannot freeze another host's publisher. The server-local writer freeze and grant custody, signed export, selector transition, and final readback are separate operator steps. Nothing in this directory deploys a receiver or contacts the production endpoint.

The application connector requires native MySQL TLS advertisement before authentication, a trusted CA, and a matching server name. A raw SSH tunnel to a listener that does not advertise TLS does not satisfy that connector contract. `listener-tls.fragment.yaml` is a reviewed example, not an applied configuration.

The application seed, guarded HOST switch, fresh-host path and latest signed Git export are described in the [migration runbook](../../docs/design/dolt-hq-config-migration-runbook.md).
