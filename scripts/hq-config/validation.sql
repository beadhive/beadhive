-- SERVER-LOCAL root evidence capture; no mutation. Restrict stored report to0600.
SELECT DOLT_VERSION(), CURRENT_USER();
SHOW CREATE DATABASE beadhive_hq_config;
USE beadhive_hq_config;
SHOW CREATE TABLE hq_config_meta;
SHOW CREATE TABLE hq_config_documents;
SHOW CREATE TABLE hq_config_publications;
SELECT DOLT_HASHOF('HEAD') AS schema_commit;
SELECT * FROM dolt_status;
SELECT * FROM hq_config_meta AS OF 'HEAD';
SELECT * FROM dolt_branch_control;
SELECT * FROM dolt_branch_namespace_control;
-- Add exact configured host literals to SHOW GRANTS FOR each new principal.
-- Include unchanged SHOW GRANTS FOR frame_hq_proto using its actual account host.
-- Root captures these because USAGE-only frame account could not read mysql grants.
-- Then real separate reader/publisher sessions must prove effective behavior,
-- including forbidden SQL; grant text alone is insufficient. See README.md.
