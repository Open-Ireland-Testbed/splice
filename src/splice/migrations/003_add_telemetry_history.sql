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
