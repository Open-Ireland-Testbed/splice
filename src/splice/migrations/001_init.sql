-- Initial schema for Splice (vendor-agnostic gNMI server)

CREATE TABLE IF NOT EXISTS devices (
  id BIGSERIAL PRIMARY KEY,
  name VARCHAR(255),
  host VARCHAR(255) NOT NULL,
  port INTEGER NOT NULL DEFAULT 830,
  username VARCHAR(255) NOT NULL,
  password_ref VARCHAR(255),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS ix_devices_host_port ON devices (host, port);

CREATE TABLE IF NOT EXISTS modules (
  id BIGSERIAL PRIMARY KEY,
  device_id BIGINT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
  name VARCHAR(255) NOT NULL,
  revision VARCHAR(64),
  namespace TEXT,
  format VARCHAR(32),
  source VARCHAR(64),
  conformance VARCHAR(64),
  features JSONB,
  deviations JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS ix_modules_device_name_rev
  ON modules (device_id, name, revision);

CREATE TABLE IF NOT EXISTS yang_files (
  id BIGSERIAL PRIMARY KEY,
  module_id BIGINT NOT NULL REFERENCES modules(id) ON DELETE CASCADE,
  path TEXT NOT NULL,
  checksum VARCHAR(128),
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_yang_files_module ON yang_files (module_id);

CREATE TABLE IF NOT EXISTS catalog_paths (
  id BIGSERIAL PRIMARY KEY,
  device_id BIGINT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
  module_id BIGINT REFERENCES modules(id) ON DELETE SET NULL,
  canonical_path TEXT NOT NULL,
  filter_xml TEXT NOT NULL,
  namespace_map JSONB,
  list_keys JSONB,
  keyword VARCHAR(32) NOT NULL,
  config BOOLEAN NOT NULL DEFAULT FALSE,
  data_type VARCHAR(128),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS ix_catalog_paths_device_path
  ON catalog_paths (device_id, canonical_path);

CREATE TABLE IF NOT EXISTS path_aliases (
  id BIGSERIAL PRIMARY KEY,
  device_id BIGINT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
  canonical_path TEXT NOT NULL,
  alias_path TEXT NOT NULL,
  source VARCHAR(64) NOT NULL DEFAULT 'generated',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS ix_path_aliases_device_alias
  ON path_aliases (device_id, alias_path);

CREATE INDEX IF NOT EXISTS ix_path_aliases_device_canon
  ON path_aliases (device_id, canonical_path);

CREATE TABLE IF NOT EXISTS jobs (
  id BIGSERIAL PRIMARY KEY,
  device_id BIGINT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
  canonical_path TEXT NOT NULL,
  interval_sec INTEGER NOT NULL DEFAULT 30,
  enabled BOOLEAN NOT NULL DEFAULT TRUE,
  mode VARCHAR(32) NOT NULL DEFAULT 'sample',
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS ix_jobs_device_path
  ON jobs (device_id, canonical_path);

CREATE TABLE IF NOT EXISTS last_value (
  id BIGSERIAL PRIMARY KEY,
  device_id BIGINT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
  canonical_path TEXT NOT NULL,
  value_json JSONB,
  timestamp_ns BIGINT NOT NULL DEFAULT 0,
  meta JSONB,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS ix_last_value_device_path
  ON last_value (device_id, canonical_path);

CREATE TABLE IF NOT EXISTS telemetry_history (
  id BIGSERIAL PRIMARY KEY,
  device_id BIGINT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
  canonical_path TEXT NOT NULL,
  value_json JSONB,
  timestamp_ns BIGINT NOT NULL,
  meta JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_hist_device_path_ts ON telemetry_history (device_id, canonical_path, timestamp_ns);
