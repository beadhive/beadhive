"""Common backed membership reads, exercised with real Git and SQL carriers."""


def assert_common_membership_reads(plane, manifest, accepted_digest):
    """Both backends must expose one accepted beat through the same public ports."""
    revision, desired, observation = plane.read_eligibility(manifest)
    assert revision
    assert desired["state"] == "active"
    assert observation.verified and observation.fresh
    assert observation.sha == accepted_digest
    assert plane.fetch_config(manifest.frame_id, holder_identity=manifest.host_id) == desired
    assert plane.observe(manifest).sha == accepted_digest
    assert plane.watch_state(manifest.frame_id).sha == accepted_digest
