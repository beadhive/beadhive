# hq-service conformance/session/v1 fixtures

Copied verbatim from hq-service `conformance/session/v1` (renew and evidence cases and
`refusal-codes.json`) at commit 205fd5b. One case per distinct
`(status, code)` outcome of each route, plus `evidence-accepted-non-ascii` (pins the ASCII-escaped
canonical digest) and `evidence-key-order-irrelevant` (a differently ordered, pretty-printed body
with the same digest). They are hq-service's to change: refresh by recopying, do not edit. Used by
`tests/test_hq_service_renew_contract.py` (bh-nqr4b) and
`tests/test_hq_service_evidence_contract.py` (bh-xtznm).
