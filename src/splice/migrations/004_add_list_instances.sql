CREATE TABLE IF NOT EXISTS list_instances (
  id BIGSERIAL PRIMARY KEY,
  device_id BIGINT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
  list_path TEXT NOT NULL,
  key_values JSONB NOT NULL,
  discovered_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_list_instances_device_list
  ON list_instances(device_id, list_path);

CREATE UNIQUE INDEX IF NOT EXISTS ix_list_instances_device_list_keys
  ON list_instances(device_id, list_path, key_values);
