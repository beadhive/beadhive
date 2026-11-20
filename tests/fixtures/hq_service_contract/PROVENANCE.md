# hq-service contract fixtures (bh-21vpt)

Pinned to hq-service v0.1.0 (commit 4e0fc6b, which includes the bh-hq-91a molecule).
`tests/test_hq_service_contract.py` uses them.

- `admission_v1/`, `operator_v1/` and `http_v1/`: conformance cases and registries copied
  verbatim from hq-service `conformance/admission/v1`, `conformance/operator/v1` and
  `conformance/http/v1` at v0.1.0 (with each `refusal-codes.json`). The admission cases carry the
  exact request of POST /incarnations, PUT and DELETE /manifests/{host_id}, POST
  /admissions/{frame} and GET /release-catalog/{catalog} (list and verify); the operator cases
  carry each plan request body and the plan's record. Recopy to refresh, do not edit.
- `reads_v0.1.0.json`: hand-built from hq-service `store/reads.go` (the JSON tags of
  `ConfigRead`, `HistoryRead`, `EligibilityRead`, `Liveness`, `ReleaseRead`, `HiveLeaseRead`) and
  `docs/DESIGN.md` "Reads"; the config document digest is `ordered_documents_digest` of the
  documents. hq-service has no corpus for these reads, so they are re-derived by hand when
  `store/reads.go` changes.
- Caller binding (hq-service 6fc3ec8) and the attested history query (44ce49a, `target_sha256`)
  are Gateway-side: the adapter sends the same request, so no body fixture changes with them.
- `responses.json` (f69779c) keeps the authority, submission and error-body shapes of the first
  four routes; `session_v1/` has its own PROVENANCE.md.
