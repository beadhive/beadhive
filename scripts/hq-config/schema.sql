-- Fresh dedicated CONFIG database only. Execute only after operator review on
-- the server-local path. This resource does not contact or configure a server.
-- Never use IF NOT EXISTS: existing data/name collision must stop provisioning.
CREATE DATABASE beadhive_hq_config;
USE beadhive_hq_config;

CREATE TABLE hq_config_meta (
  singleton_id TINYINT NOT NULL PRIMARY KEY,
  schema_version INT NOT NULL,
  backend_identity VARCHAR(128) NOT NULL,
  generation VARCHAR(128) NOT NULL,
  publication_sequence BIGINT UNSIGNED NOT NULL,
  publication_id CHAR(36) NOT NULL,
  documents_sha256 CHAR(64) NOT NULL,
  document_count INT UNSIGNED NOT NULL,
  -- Different physical attempts sharing one durable logical publication UUID
  -- must not coalesce equal singleton writes under Dolt 2.3.5 concurrency.
  initial_attempt_nonce CHAR(36) NOT NULL
);
CREATE TABLE hq_config_documents (
  path VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL PRIMARY KEY,
  ordinal INT UNSIGNED NOT NULL UNIQUE,
  kind VARCHAR(32) NOT NULL,
  content LONGBLOB NOT NULL,
  content_sha256 CHAR(64) NOT NULL
);
CREATE TABLE hq_config_publications (
  publication_id CHAR(36) NOT NULL PRIMARY KEY,
  publication_sequence BIGINT UNSIGNED NOT NULL UNIQUE,
  generation VARCHAR(128) NOT NULL,
  expected_parent_revision VARCHAR(128) NOT NULL,
  documents_sha256 CHAR(64) NOT NULL,
  document_count INT UNSIGNED NOT NULL
);
-- No rows are seeded here: empty provisioned database is NOT attachable authority.
-- Initial seed is a separately reviewed, value-restricted migration manifest.
-- Metadata is separate from raw fleet/workspace documents, never config keys.
CALL DOLT_ADD('hq_config_meta', 'hq_config_documents', 'hq_config_publications');
CALL DOLT_COMMIT('-m', 'Initialize dedicated HQ config schema v3', '--author',
                 'HQ operator <hq-operator@localhost>');
