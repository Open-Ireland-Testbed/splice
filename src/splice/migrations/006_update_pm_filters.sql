ALTER TABLE pm_filters
  ADD COLUMN IF NOT EXISTS list_path TEXT,
  ADD COLUMN IF NOT EXISTS key_values JSONB;

DROP INDEX IF EXISTS ix_pm_filters_device_root;
CREATE INDEX IF NOT EXISTS ix_pm_filters_device_root
  ON pm_filters (device_id, root_path);

CREATE UNIQUE INDEX IF NOT EXISTS ix_pm_filters_device_root_keys
  ON pm_filters (device_id, root_path, list_path, key_values);
