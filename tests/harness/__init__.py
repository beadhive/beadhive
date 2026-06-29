"""AGF test harness — simulate the ws work topology and validate the resulting git history.

Real `bd` (embedded Dolt) is the backend; FakeBd is intentionally NOT used here (it lives in
test_work.py as the fast unit layer). See the modules: world (env + keys), beads (bd seam),
rig (rig builder), graph (work shapes), modalities (developer roles), history (assertions).
"""
