# Fleet eligibility native validation scheduling

The `bh-38h7p` final native partition retains the complete native collection,
sixteen workers, the 900-second watchdog, all test assertions, and the existing
integration/Pants and root-composition partition boundaries. Only distribution
of unstarted tests changes from pytest-xdist's default `load` to `worksteal`.

Two managed clean-checkout submissions using `load` reached the unchanged
watchdog limit. Their stateful keys ended red with exit 124 after 1025.153 and
1025.239 seconds, respectively. The second candidate was `6071234a`; neither run
submitted a bead or provided a green native receipt. Other validation keys in
the second submission completed green.

Owned active-test registrations in the second run recorded only one worker
executing the signed HQ tail: at elapsed 485 seconds, operator renewal after
expiry; at 619 seconds, authorized raw-frame receive; at 745 seconds, complete
receive-session serialization; and at 867 seconds, real signed hive-lease
adopt/renew/cordoned release. The same HQ test file independently completed all
69 tests in 150.93 seconds with sixteen workers and the same watchdog budget.
This supports redistributing queued work instead of increasing a timeout.

The pinned pytest-xdist 3.8.0 `load` scheduler assigns contiguous batches and
does not steal batches already assigned to another worker. Its `worksteal`
scheduler validates identical collections and moves only unstarted queued test
indices to idle workers. Running tests keep their fixture cleanup and reporting
semantics. Production fixture slot limits remain unchanged.

A worksteal diagnostic with `-x` localized a stale live config dependency
artifact. It was gracefully interrupted to obtain its stored report and ended
non-green: one failed, 7546 passed, eleven skipped in 298.29 seconds. That
diagnostic does not qualify the full partition or scheduling change. The exact
config measurement closure subsequently reproduced the second stale live
count. Both were corrected from the canonical current inventory, with its
historical baseline unchanged.

Required qualification is a normal managed clean-checkout submission with the
complete native partition and without `-x`. Its actual terminal verdict is the
submission authority; these diagnostic results do not replace it.
