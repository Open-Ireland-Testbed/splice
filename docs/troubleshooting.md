# Troubleshooting

## gNMI Get returns empty / timestamp 1970
Cause:
- No cached values yet (poller not running or no matching paths).

Fix:
- Ensure the server is running and polling.
- Run `build-catalog --include-config` and `build-aliases`.
- Use `gnmic get --path /` to confirm any values at all.
- Check logs for poller errors.

## Trigger request returns UNIMPLEMENTED
Cause:
- Trigger mode is not enabled.

Fix:
- Set `trigger.enabled: true`.
- For HTTP endpoint, also set `trigger_api.enabled: true` and `trigger_api.bearer_token`.
- Restart the bridge process.

## Trigger request returns INVALID_ARGUMENT
Cause:
- Invalid trigger payload or invalid path(s).

Fix:
- For gNMI extension (`id=61001`), payload must be JSON object with `{"mode":"TRIGGER"}`.
- Ensure optional fields use correct types:
  - `publish_kafka`: bool
  - `auto_onboard`: bool
  - `interval_sec`: integer > 0
- Ensure requested paths exist in catalog/aliases and are not empty strings.

## HTTP trigger endpoint returns 401
Cause:
- Missing/invalid bearer token.

Fix:
- Send `Authorization: Bearer <trigger_api.bearer_token>`.
- Confirm config token exactly matches request token.

## Kafka publishing not working
Cause:
- Kafka disabled, misconfigured brokers, or missing Kafka dependency.

Fix:
- Install optional dependency: `.venv/bin/pip install -e .[kafka]`
- Set `kafka.enabled: true` and non-empty `kafka.brokers`.
- Check broker reachability from bridge host/container.
- Confirm topic names:
  - `<topic_prefix>.<device>.poll`
  - `<topic_prefix>.<device>.trigger`

## Keyed list paths don’t match
Cause:
- Missing list instances or mismatched key order.

Fix:
- Run `refresh-list-instances` or enable `poller.list_refresh_sec`.
- Use `list-keyed-paths` to see expanded concrete paths.
- Key order is normalized by list key order; if a list has no catalog list_keys, request the list root instead of a leaf.

## PM / FEC‑BER values missing
Cause:
- PM models are vendor specific; the device may expose PM under different roots.
- PM filters may be pinned to a non‑active interface.

Fix:
- Enable PM discovery (`discovery.pm_discovery: true`) and rebuild catalog.
- Use `pm_fanout` or `pm_selector` to target the active interface.
- Run `gnmic get --path "/pm"` (PM paths are instance-keyed, e.g. `/pm/otnpm-data[interface=...]/FEC/...`; the unkeyed prefix matches everything below it).

## NETCONF auth failures in logs / Account lockout prevention
Cause:
- Wrong password or password file not readable.
- Too many concurrent NETCONF sessions.
- Repeated auth attempts triggering device lockout policies.

Fix:
- Confirm `password_file` contents and permissions.
- The server has built-in lockout-safety: auth failures trigger a 5-minute cooldown (configurable via `auth_failure_cooldown_sec`).
- Check pool status in health endpoint: `curl http://localhost:9809/health`
- Wait for the cooldown period before retrying.
- Reduce polling concurrency (increase intervals, reduce jobs).
- Restart the server after updating credentials.

## Circuit breaker open warnings
Cause:
- Too many consecutive NETCONF failures (network issues, auth failures, device overload).

Fix:
- Check logs for the specific failure reason.
- The circuit breaker pauses connections for `circuit_breaker_cooldown_sec` (default 60s).
- If auth-related, verify credentials and wait for the longer auth cooldown (default 300s).
- Monitor health endpoint for circuit breaker status.

## build-catalog seems slow or “hangs”
Cause:
- Large model catalog or list instance discovery in progress.

Fix:
- Use `discovery.resolve_all_lists: false` (default).
- Check logs for progress and batch counters.
- Increase `list_batch_size` and `list_sleep_sec` to throttle device load.

## setup/migrate hangs or fails around migrations
Cause:
- Another `setup`/`migrate` is running against the same PostgreSQL database.
- Long-lived `idle in transaction` sessions are holding locks.

Fix:
- Run preflight first (use `splice` if installed via `pip install -e .`, otherwise `PYTHONPATH=src python src/splice/cli.py`):
  - `splice --config <config> doctor`
  - `splice --config <config> migrate --dry-run`
- Inspect blockers in PostgreSQL:
```sql
SELECT pid, usename, state, wait_event_type, wait_event,
       now() - xact_start AS xact_age, query
FROM pg_stat_activity
WHERE datname = current_database()
ORDER BY xact_start NULLS LAST;
```
- If needed, clear stale blocker sessions (DBA/admin action):
```sql
SELECT pg_terminate_backend(<pid>);
```
- Rerun migrations explicitly, then setup without migration step:
```bash
splice --config <config> migrate
splice --config <config> setup --skip-migrate
```

Prevention:
- Do not run multiple `setup`/`migrate` commands concurrently for the same DB.
- Prefer one DB per environment/device pipeline.
- Keep migration fail-fast settings at defaults, or tune via:
  - `SPLICE_MIGRATION_LOCK_WAIT_SEC`
  - `SPLICE_MIGRATION_LOCK_TIMEOUT_MS`
  - `SPLICE_MIGRATION_STATEMENT_TIMEOUT_MS`
  - `SPLICE_MIGRATION_IDLE_TX_TIMEOUT_MS`

## gnmic fails with TLS handshake
Cause:
- Server is running without TLS (default).

Fix:
- Add `--insecure` to gnmic.

## Postgres tests skipped
Cause:
- `TEST_DATABASE_URL` not set.

Fix:
```bash
TEST_DATABASE_URL='postgresql+psycopg://postgres:postgres@localhost:5432/splice' \
  .venv/bin/python -m pytest
```
