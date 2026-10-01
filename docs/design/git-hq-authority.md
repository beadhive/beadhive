# Git HQ authority bootstrap

Approved batch: bh-z6l1o, bh-dd7z0, bh-llulj. Baseline checkpoint
1307874b91b644be63fe9dfeabc30b1b091c0504: 145 focused heartbeat, host publication,
and provision tests passed in 45.07 seconds before extraction.

The control plane owns registration persistence, candidate grants, admission and
observer receipts. Runtime keys publish authenticated facts; they cannot grant
authority or manufacture receipt timestamps. Existing host manifest publication
continues to copy only the caller's manifest and to retry concurrent main updates.
Heartbeat commits remain parentless and do not grow main.

The Git backend reads one signed authority commit with candidate records and
durable receipts. An operator installs an independently verified receive guard on
a controlled bare HQ, using a separate operator trust anchor and restore generation.
Every authority transaction advances its parent, revision and immutable witness
together. A reader checks the remote head against the highest witness. Signature,
CAS, sender timestamps, local inventory and local SQLite alone establish no authority.
Missing or unproven protection fails closed. Hosted providers require an explicitly
provisioned equivalent enforcement binding; this initial binding supports controlled
local bare repositories only.

Local checkout/observer loss cannot reset the receipt floor or first-seen time.
Restoring the remote's entire history **and** witnesses **and** independently
provisioned generation cannot be detected within that source. Such recovery requires
out-of-band generation rotation and candidate reenrollment before eligibility.

Strict extraction invariants: the four port operations preserve U4's existing
publication behavior; mode defaults to Git; unsupported modes fail explicitly;
all authoritative mutations use exact expected revisions and signed atomic updates;
candidate and operator credentials remain separate. Real bare-backend tests exercise
the receive hook, unauthorized writes, rollback, restored readers, and publication.
The canonical full gate runs only after committing a stable final tree.

## Provisioned custody and transport

The supported Linux server deployment uses an operator-owned bare repository, its copied
guard/broker snapshots and a pinned pure Python YAML parser/schema evaluator, a separate operator
checkout and private key, and a
fixed Unix socket broker. Operator policy pins guard/broker bytes, Python, Git,
OpenSSH executables and server PATH. Clients compare installed bytes to that policy,
independent of the client's checkout path. Hosted Git enforcement is not yet proven.

A frame must have neither server filesystem write access nor operator credentials.
Use different server/frame Unix identities with protected operator paths, or the
optional Linux namespace launcher. Same UID without enforced isolation is unsupported.
A frame-owned repository made unwritable with chmod is insufficient: the reader also
requires a read-only mount when frame and server ownership coincide.

Operator setup, using real local Git and git+file Dolt transport:

1. Create an empty bare Git HQ; configure `hq.mode=git`, `hq.remote=file:///absolute/hq.git`
   and run `bh hq init --auto`. Owner/repo forge remotes retain existing behavior.
2. From the separate operator HQ checkout run `bh hq authority install
   --public-key operator.pub --generation <recovery-generation> --interpreter <python>
   --confirm-server-custody --confirm`. Preserve the returned policy digest outside
   writable frame state. Install copies all server code and pins executable paths.
3. Start `<pinned-python> <hq.git>/bh-git-broker.py serve <hq.git> <protected-dir>/git.sock`
   under operator custody. Its optional socket group grants frame transport access;
   the socket directory must remain operator owned and unwritable to frames.
4. Provision separate `bh hq authority bind --server-root <hq.git> --socket-path <socket>
   --policy-digest <digest> --client-interpreter <frame-python> --role frame --confirm`
   and operator anchors. `BH_HQ` names each client checkout, which may not exist yet.
   Store each returned `hq.authority_anchor` in the protected platform descriptor.
   Binding does not modify Git origin or broadly enable ext transport.
5. A fresh frame clones the explicit file HQ through actual provision. Its port uses
   generated literal broker URLs with per-command ext permission for publication.
   Enrollment rejects arbitrary ext commands and unsupported URL schemes.
6. The operator grants an exact candidate incarnation/public key/epoch, desired
   release/capabilities/profile and bounded expiry. The frame publishes its own
   manifest and signed registration, then repeated signed beats. An independent
   operator observer accepts receipts before explicit admission.

The Linux launcher requires util-linux `unshare`, `mount`, and `setpriv`, in addition
to the installed bh runtime. `packages.bh` alone does not include these tools.
A NixOS service can explicitly include `pkgs.util-linux` in `service.path`, or use
system PATH. Missing tools produce a prerequisite diagnostic. Darwin bh packaging
is unaffected. Use the installed launcher module with `--server-root`, `--broker-dir`,
`--frame-home`, explicit operator `--deny-file` and `--hide-dir` exclusions, and only
explicit frame `--writable-path` mounts. It creates private user/PID/network/mount
namespaces and fresh proc, drops capabilities, sets no-new-privileges, and makes
visible host filesystems read-only. Inaccessible and obscured inherited mounts grant
no access. It passes a clean environment containing tooling and only approved
frame-scoped inputs from `--frame-env-file`. Operator SSH agent/environment secrets
are excluded. Other accessible host IPC endpoints must not grant operator privileges;
operator credential/IPC exclusions are a deployment responsibility.

## Active continuity and bounded candidates

Each frame has one active record and at most one pending candidate record, with
separate authoritative receipts and bounded parentless carriers. Active beats use
exactly `refs/bh/heartbeat/<frame_id>`; candidates use
`refs/bh/candidate-heartbeat/<frame_id>`. Granting a replacement leaves the active
record eligible and publishing. Explicit `admit --supersede` atomically retires its
key, transfers the candidate carrier to the active ref, removes the candidate ref,
and publishes the matching authority/receipt transaction. Candidate retire cleans
abandoned carrier/registration refs without disturbing active continuity. Old and
retired keys cannot be reenrolled. These refs implement the existing single-commit,
force-updated heartbeat acceptance without changing FrameLease or D16 transport.

Expired authority denies runtime eligibility. A separate operator can verify and
renew expired authority without resetting any incarnation, sequence, receipt or
first-seen floor. Polling the same beat never renews its receipt. Downstream U3/U7
must combine verified freshness with active authoritative lifecycle/eligibility and
their complete capability/release/conformance predicates; candidate presence alone
cannot authorize work.

## Receive authorization and serialization

The broker derives the receive principal from kernel peer credentials and user
namespace identity. Frames cannot select operator privileges through request data,
Git configuration or environment. Runtime writes must carry a current granted key
and exact incarnation binding. Frames cannot delete refs or write unknown refs,
authority or witnesses. Main publication permits one signed own host manifest with
the existing complete HostManifest schema, rejecting duplicate YAML keys and custom
tags. The copied parser/evaluator and schema bytes are operator-pinned; bytecode
writes are disabled before loading them.

A protected per-repository flock serializes each entire receive-pack transaction,
including hook authorization and ref commit, across broker threads and processes.
Lock acquisition and transfer each have a 120-second deadline. A stalled transfer
is killed as a process group and reaped before unlocking. Service selection has a
five-second deadline. Upload-pack remains available independently. Under the receive
lock, authority retirement/promotion checks actual carrier and registration refs
against its exact cleanup updates. Preparation racing with a first carrier is
rejected for retry, preserving cleanup and preventing post-retirement publication.

Backend `hq.mode` and manual `hq.admission_policy` belong to fleet configuration.
Each client's protected `hq.authority_anchor` path belongs to host configuration;
configuration splitting/reconciliation preserves it. Config reads select an exact
holder identity when active and candidate coexist, returning the authenticated
binding with its desired config. Omitting that selector while both are live fails
explicitly. Broker startup requires Linux peer credentials and proc namespace
visibility; Darwin bh packaging remains independent of this optional server binding.

Protected own-manifest publication selects the recorded runtime signing key and
operator-pinned SSH executable explicitly, including retry commits. It does not
require caller Git signing settings. Legacy nonframe publication retains its
existing transport and signing behavior.

The public heartbeat API composes the real protected authority binding above lower
heartbeat transport/types; the Git provider depends on that lower layer with its
explicit authority callback. Own-manifest Git publication and its error type live
in a lower adapter shared by HQ and the provider. This removes both dependency
cycles without widening the reviewed legacy cycle baseline. Config generation
uses the canonical current release renderer and atomic inventory writer; historical
v1 bundle bytes remain pinned and unchanged.

Frame retirement uses `bh host frame-retire plan|apply|check <frame_id>`.
The existing `bh host retire` keeps its four options and host-local decommission
behavior; the additive command owns the protected authority lifecycle.

## Committed fleet configuration port and observation boundary

The additive `FleetConfigRevisionPort` lives beside the existing configuration document
ports. It returns immutable raw ordered documents and separate backend identity, committed
revision, recovery generation, fetch time and validity metadata. The configuration module
remains the only owner of model validation, scope/override resolution and secret classification;
the store does not invent an effective-config resolver. Workspace documents preserve the full
selected set and original order, including provider group paths, filters and non-secret metadata.
No host roots, credentials or Beads attachment data belong in a fleet document snapshot.

Protected Git config publication uses its own operator-signed `bh-config` head and immutable
`config-witness` refs. It is separate from legacy main-layout writes and frame desired-config
reads, so enabling this API does not silently migrate a working checkout or change released
executors. Publication compares the originally loaded expected revision, atomically updates
head and witness, then rereads the exact committed revision. Stale writers, runtime frame keys,
missing witnesses, rollback, expired authority and mismatched recovery generation refuse.
Restoring all heads/witnesses/policy still requires independent recovery generation custody.

`attach_fleet_config` resolves explicit bootstrap inputs or `config.load_host()` only, before
fleet/effective loading can run. It attaches and reads back without source import, host identity
changes or Beads initialization/hydration/remote writes. The Git client uses its existing protected
binding and object workspace. SQL and unknown selectors fail closed until the separately owned
SQL binding is implemented and qualified; attachment never falls back to Git after SQL failure.
The subsequent fleet config consumer owns import validation, working-copy seed reconciliation,
cache/materialization and configuration authority switching.

Production frame observation now invokes the selected control-plane port, which authenticates
its own carrier. Git remains the SSH-signed parentless carrier adapter. The shared
`assess_authenticated_observation` policy consumes a verified lease/signer and opaque carrier
digest with one trusted authority/receipt; it performs no Git fetch or signature inference.
Durable receipt sequence, identity, first-seen and validity remain authoritative after local
SQLite loss. This extraction does not implement SQL signatures or qualify a fake SQL backend.
Legacy nonframe Git observation and explicit low-level transport test injection remain supported.
The authority CLI uses public `authority_status`, rather than a Git-private reader.
When Git protection is unavailable, its adapter may retain signed-carrier diagnostic age
from the existing Git source, with no trusted receipt and no fresh/eligible result. This
compatibility path exists only inside the selected Git binding; an unsupported or failed
SQL selector never invokes Git diagnostics.

The additive `load_config_authority_snapshot` port returns `ConfigAuthoritySnapshot` with
both immutable views and a provider-issued `HqConsistencyToken` (backend, generation and
combined-read revision). The separate Git config head and legacy frame-authority head do not
constitute an atomic combined read, so this capability explicitly refuses on the Git binding.
Consumers must not join independent `load_snapshot` and `watch_state` calls
to assert config-bound eligibility. The SQL consumer binding must expose a combined read in
one authoritative transaction: committed fleet revision, backend identity/recovery generation,
frame desired-policy revision and trusted observer receipt floors share an immutable binding
token. It must reject mixed revisions, changed/revoked authority or either expired validity.
Receipt `config_revision` remains the frame desired-policy revision; it must not be silently
relabelled as the fleet document commit. Existing Git frame behavior keeps its protected
authority snapshot contract until that explicit consumer migration is implemented.

Execution baseline is clean `fe07dc1d`, with signed U2 checkpoint `1307874b` retained as ancestor
and independently verified 116 focused tests green. Security/data-integrity changes use strict
validation: focused carrier/adapter tests at each coherent transition, then the configured
native gate and managed shared-group submission. SQL binding, hosted Git enforcement, U3 dispatch
eligibility and the NixOS migration smoke proof remain explicit independent qualification inputs.
The retained `fe07dc1d` NixOS/no-nix-ld fixture subsequently passed with actual KVM under its
unchanged init 180s/status 120s bounds (134.02s/2.13s); the original TCG failure was deadline
cancellation, not a reproduced migration deadlock. That proof qualifies the original packaged
closure only, not these new Python changes or hosted Git enforcement.
Current operator delivery is HQ configuration syncing only; existing Beads databases and remotes,
including HQ's, remain untouched.
