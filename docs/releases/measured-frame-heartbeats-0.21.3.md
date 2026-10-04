# Measured frame heartbeats in 0.21.3

The old local sender embedded a stale failure and an earlier installed digest.
Replace it with the supported CLI after the operator has reviewed the installed
artifact and upgraded its enrollment epoch. Preserve the host UUID, signer,
canonical HQ identity, old registration and old observer evidence.

`bh host heartbeat-report` generates JSON only. It reads the current verified
SQL frame composite, committed manifest, operator release/profile and current
epoch, and uses the durable accepted observation sequence plus one. The release
ID is the operator's artifact label; the digest is independently measured from
installed package files using the original enrollment algorithm. Editable
installs without the installed package inventory cannot produce this evidence.
Installation/grant or manifest drift refuses generation and publication.

The report measures raw HOST schema and ownership validation, usable effective
configuration, host/frame identity, configured hive readiness and `bd ping`
reachability. Failed or unavailable checks produce `non-conformant`, with zero
free sessions. Probe exceptions and command output are omitted from evidence to
avoid exposing credentials. Capacity defaults to zero; an explicit
`--free-sessions N` assertion must fit the committed frame capabilities.

`bh host heartbeat-send` remeasures these facts and explicitly publishes one
signed bounded heartbeat using the recorded host key. It never admits a frame
or changes operator authority. Each invocation uses the latest durable sequence;
run a single sender per frame. Simultaneous publishers can race and should retry
by generating another fresh report, never by editing a sequence in saved JSON.

After the reviewed operator upgrade, publish the new signed registration using
the existing registration operation, then run `heartbeat-send` three times with
the receiver accepting each heartbeat before the next invocation. Check the
protected consecutive heartbeat count before normal operator admission. Check
hive eligibility and acquire its lease after admission. Keep recurring service
activation disabled until this one-shot recovery succeeds.
