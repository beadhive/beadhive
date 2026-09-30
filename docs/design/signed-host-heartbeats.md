# Signed host heartbeat Git binding

Implementation bead: `bh-z6l1o` (U2). Canonical design source:
`beadhive/frame` `docs/design/frame-platform.md`, sections 6/P9/P10 and 7.

## Carrier and observation API

`bh host heartbeat <record.json>` publishes runtime observations for this machine's
recorded host identity, signed with its recorded SSH signing key. The record includes
frame identity, host incarnation (`holderIdentity`), substrate instance reference,
key fingerprint, authoritative epoch, fleet audience, configuration revision,
monotonic sequence, sender time, bounded interval/TTL, installed release/runtime,
observed state, conformance, available sessions and report digest. A caller supplies
observed runtime facts; the command never changes lifecycle/admission authority.

The carrier is a parentless SSH-signed commit with exactly `heartbeat.json`, at
`refs/bh/heartbeat/<frame_id>` (legacy identity may use host ID). Publication uses
compare-and-swap against the observed remote SHA. Replacing an existing ref requires
matching current operator authority and a new signature verified to its approved
fingerprint. A revoked incarnation's old signature need not remain trusted to let
the authorized replacement publish. Unknown predecessor signatures, or known D16 signatures on ungranted epoch or
incarnation metadata, cannot establish an epoch/sequence floor. The authoritative
accepted receipt sets that floor; attacker-chosen huge values do not advance it. Replacement
requires a current trusted authority snapshot and verifies the new signer against
that authority; a local authority file alone cannot authorize replacing a beat. Verified predecessor records enforce sequence
monotonicity; incarnation/key/instance changes require an epoch advance. Repeated
beats replace the ref;
its reachable history has one commit. No HQ main commit, index edit, ordinary
branch update or configuration publication occurs. Old unreachable objects can
remain until ordinary Git garbage collection; constant reachable history is not
an assertion that the object database never grows between collections.

`beadhive.host_heartbeat.observe(hq_dir, manifest, now=..., observer_dir=...,
authority_lookup=...)` returns a `VerifiedObservation`: status, verified, fresh,
age, lease, reason, SHA and candidate flag. Signature verification is necessary
but does not imply valid identity, freshness, conformance or admission. Missing,
unreachable, unsigned, unknown-key, malformed, identity-mismatched, expired and
replayed evidence all have `fresh=False`. U3 must require admitted authority in
addition to this result; in particular `candidate=True` never authorizes work.

## Authority binding and candidate observation trust

`ObservationAuthority` is a typed read-only seam. U2 does not write its records.
Admission/P6 owns authoritative identity and must supply the same current authority
to heartbeat validation and eligibility. Fields are frame ID, holder/incarnation,
instance reference, approved key fingerprint, epoch, fleet audience and configuration
revision. Candidate trust also has a bounded expiry.

The diagnostic Git inventory reader reads provisional operator-owned
`heartbeat-authorities/<frame_id>.json`, or (only when no admitted record exists)
`heartbeat-candidates/<frame_id>.json`. Admitted signatures use HQ `allowed_signers`;
candidates use separate `candidate_signers`. These files are configuration authority,
not sender-populated runtime facts. Frames must not have authority to update them.
A deployment that lets a frame publish arbitrary HQ configuration cannot claim
fail-closed admission merely because these observation checks pass. The authority
provider must establish current config and custody; this reader does not authenticate
an arbitrary local checkout or prove storage-side configuration write fencing.

The observed key fingerprint and every identity/epoch/audience/revision field must
match current authority. Candidate observation trust permits only bounded pending
evidence, never admission. Revoked and superseded authority must be rejected by the
same authority provider used for routing. Nothing silently falls back to a second
HQ mode or an old authority source.

## Time, replay and restore

A sender timestamp more than 30 seconds into the observer's future is rejected;
expired sender evidence is classified stale. A timestamp in the past cannot by
itself distinguish a slow sender clock from ordinary delivery or polling delay.
A legitimate first poll 60 seconds after a beat remains bounded by that beat's
original TTL. This reader does not claim to detect negative sender-clock skew
without separately trusted clock evidence. TTL is at least three intervals and
at most 900 seconds (defaults 60/300). Freshness expires at the earlier bound from
sender time and trusted first-seen time. Polling the same signed commit cannot
advance first-seen time or renew the lease.

The trusted authority port returns `AuthoritySnapshot`: current admitted or bounded
candidate authority, authority read time/expiry, exact accepted SHA, monotonic sequence
floor, and trusted first-observation timestamp. The provider must obtain these from
the current authority source and durable observer receipts. Runtime signed payloads
cannot supply any of these inputs. Epoch/incarnation matching uses that current
snapshot. A beat below its floor is replay; different bytes at its floor are replay;
a later beat without its own trusted receipt is unavailable. Expired snapshots and
observer clock rollback deny freshness. Restore uses the authoritative first-seen
stamp even when the SQLite file is deleted.

A newly granted epoch can have a zero sequence floor and no receipt. This permits
its first authorized publication but never observation freshness until an external
trusted observer accepts the exact new bytes and records first-seen time.

No production trusted-authority binding is implemented in this checkpoint: `load_trusted_authority`
returns unavailable, so ordinary local Git files can prove signature/identity only
and never yield a fresh observation. HQ/admission beads must supply this port using
their explicit authority and observer contract. A local checkout, even fetched,
is not a global currentness or durable-observer proof. Candidate trust remains
separate from admitted authority and cannot authorize work. Contract fixtures model
an independent authority/observer service and exercise restore and expiry behavior.

Observer-local SQLite state persists the highest epoch/sequence and first-seen
stamp under `BH_HOME/heartbeat-observations`. Updates take an immediate SQLite
transaction, preventing concurrent readers from losing their high-water marks.
Lower sequence, same sequence with different signed bytes, lower epoch, a changed
incarnation/key/instance binding without epoch advance, and observer
clock rollback fail closed. Keep this state across restarts and include it in
restore policy. Deletion or restore of SQLite cannot grant freshness: the trusted authority floor
and receipt are mandatory on every observation. Restoring a file is not authority
reconstruction. The port implementation must quarantine when its durable authority
or observer state cannot be reconstructed.

`bh host list` renders signature verification, observation status, heartbeat age,
age basis and liveness source. `sender-diagnostic` age remains visible when current
authority/observer receipts are unavailable; it never sets fresh or eligible. A
signed record provably expired by sender TTL is stale even without authoritative
observer state. Only `trusted-observer` age can establish bounded observation
freshness, which remains distinct from admission. Mtime fallback is only for a legacy host with no heartbeat, labeled
`legacy-mtime`. A frame without a heartbeat is stale. A present invalid heartbeat
cannot become live by touching the manifest. Network uncertainty cannot turn
existing heartbeat evidence into a fresh mtime fallback.

## Canonical contract consumption

U2 consumes the independent `FrameLease` schema owned by `beadhive/frame`,
vendored at `src/beadhive/schemas/frame/v1alpha1/framelease.schema.json` with its
source commit and byte digest recorded in adjacent `provenance.json`. The positive
fixture is copied from that same upstream artifact. The approved upstream revision is
`acf056630709dce340402e0613ac1792c4ce1a28`, merged into the published frame contract
container; the schema digest is recorded in provenance.

The stable Git carrier remains a parentless SSH-signed commit containing the flat
sender record. `framelease_envelope` projects its verified bytes into the canonical
FrameLease envelope. `signature.scope=git-commit`, `algorithm=ssh`, and
`signedObject` identify the exact Git object; `value` is base64 SSHSIG container
bytes. No JSON signature is generated or claimed. The source fingerprint must
match `key_id`; metadata name derives from authenticated frame ID. The adapter
validates schema and requires complete canonical sender bytes, including installed
release, typed conformance, report digest, explicit sequence/epoch and interval/TTL.
`verify_framelease_envelope` rebuilds the projection from verified source bytes and
compares every field; added conditions, metadata overrides, signer/provenance edits,
and identity/epoch/sequence/time/TTL changes are rejected. This transport projection
never supplies authority or first-observer receipts and never grants admission.

D16 escrowed per-frame SSH transport and manual registration remain unchanged.
Incarnation revocation requires authoritative epoch verification; this operation
is not a migration to a separate signing-key protocol.

## Required authority foundation

U2 is not acceptance-complete. The real default CLI can publish an initial signed
observation, but repeated publication cannot safely replace an existing ref without
current trusted authority. P6/admission must implement an authenticated current
operator/candidate source and durable accepted replay/first-observer receipts. The
canonical design lists those authority transitions as planned; the provisional local
JSON inventory cannot satisfy that source contract. The authority-first foundation
replan must supply a usable production binding before U2 can complete its repeated
publication acceptance. Injected contract fixtures are validation of the port only,
not a production authority implementation. Signature/age diagnostics remain usable;
all routing freshness remains denied until the foundation is supplied.
