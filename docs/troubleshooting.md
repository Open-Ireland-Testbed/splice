# Troubleshooting

A symptom-driven catalogue of the failure modes you are most likely to hit with
Splice, plus a reference for the `splice doctor` pre-flight checks. Each entry
follows the same shape: **symptom -> diagnosis -> fix**.

!!! tip "Start here"
    When something is wrong, run the pre-flight checks first — they pinpoint most
    issues in one shot:

    ```bash
    splice --config config.yaml doctor
    ```

    If `splice` is not installed via `pip install -e .`, prefix commands with
    `PYTHONPATH=src python src/splice/cli.py`.

See also: [Configuration](configuration.md) · [CLI Reference](cli.md).

## `splice doctor` — pre-flight checks

`doctor` runs a sequence of read-mostly checks and prints `[PASS]`/`[FAIL]` per
check, with a `Fix:` hint on failure. It exits `0` when all pass, `1` otherwise.
The checks (in order) are defined in `doctor.py`:

| Check | What it verifies | Typical fix on failure |
|-------|------------------|------------------------|
| `config` | `device.host` and `db.url` are set | Edit `config.yaml` and set the missing fields |
| `database` | PostgreSQL is reachable (`SELECT 1`) | Check `db.url`; verify PostgreSQL is running |
| `db_lock_blockers` | No long-lived `idle in transaction` sessions (>= 30s) that would block migrations | Inspect/clear stale sessions in `pg_stat_activity` |
| `migrations` | All migrations applied (none pending) | `splice --config <config> migrate` |
| `yang_dir` | `yang_dir` exists and contains `.yang` files | `splice --config <config> setup` (or `fetch-yang`) |
| `device_port` | NETCONF TCP port is reachable (5s connect) | Confirm the device is powered on and network-reachable |
| `netconf_auth` | NETCONF login succeeds (15s connect) | Check username/password; verify NETCONF is enabled on the port |
| `catalog` | `catalog_paths` has rows for this device | `splice --config <config> setup` (or `build-catalog`) |
| `jobs` | `jobs` has rows for this device | `splice --config <config> setup` (or `init-jobs`) |
| `pm_filters` | Instance-keyed PM filters still match live device instances (only runs when `discovery.pm_discovery` is enabled) | Re-run `setup`, or enable `discovery.pm_refresh_on_run` |

!!! note "db_lock_blockers needs privileges"
    The lock-blocker check reads `pg_stat_activity`. If the DB user lacks
    permission, the check is **skipped** (reported as PASS with a note), not
    failed.

---

## Connectivity & authentication

### NETCONF auth failures / device account lockout

**Symptom.** Logs show `AUTH FAILURE — Circuit breaker OPEN for auth failure`,
or `doctor`'s `netconf_auth` check fails. Empty telemetry follows.

**Diagnosis.** Wrong password, an unreadable `password_file`, or too many
concurrent NETCONF sessions. Optical gear (ADVA/ConfD-based devices in
particular) enforces lockout policies after repeated auth attempts. Splice
treats auth errors specially: on any auth failure the pool **immediately** opens
the circuit breaker for the long `auth_failure_cooldown_sec` window (default
**300s**) to avoid hammering a device into a lockout. The auth detector
(`NetconfClientPool._is_auth_error`) also treats session-limit and access-denied
conditions as auth-class — matching phrases like `access denied`,
`too many sessions`, `session limit`, `permission denied`, `lock-denied`,
`license limit` — because retrying those makes them worse.

**Fix.**

- Confirm `device.password_file` contents and file permissions, or that
  `NETCONF_PASSWORD` is set.
- **Wait out the cooldown** before retrying (default 300s; ADVA devices may need
  ~5 minutes). Tune with `poller.auth_failure_cooldown_sec`.
- Reduce polling concurrency: increase poll intervals and/or reduce the number
  of jobs so fewer sessions are opened.
- Restart the server after updating credentials (the cooldown timer is
  in-process).
- Check pool state via the health endpoint (default port `9809`):

  ```bash
  curl http://localhost:9809/health
  ```

### Concurrent-session limits

**Symptom.** Intermittent RPC failures, "too many sessions" / "session limit" in
logs, sporadic empty responses — especially while a long discovery is running.

**Diagnosis.** Splice uses a **single shared NETCONF session** per device
(`NetconfClientPool` is a single-session pool). But running `discovery`/`setup`
at the same time as `run`, or pointing multiple Splice instances at one device,
multiplies sessions. Devices like the ADVA Teraflex cap concurrent NETCONF
sessions and reject the surplus.

**Fix.**

- Don't run `setup`/discovery against a device that an active `run` is already
  polling; sequence them.
- One Splice `run` instance per device.
- Increase `poller.task_throttle_ms` (default **50** ms) to space RPCs out.
- Because session-limit errors are classified as auth-class, they trip the long
  cooldown — wait it out rather than restarting in a tight loop.

### paramiko 4.x — SSH key-exchange incompatibility

**Symptom.** Connections to optical/legacy NETCONF gear fail at the SSH layer
(key-exchange negotiation error) even though credentials and reachability are
correct.

**Diagnosis.** paramiko **>= 4** removed the SHA-1 key-exchange algorithms
(`diffie-hellman-group14-sha1`, `diffie-hellman-group1-sha1`) that many
optical/NETCONF devices still require. Splice pins `paramiko>=3,<4` in
`pyproject.toml` precisely so the SSH transport can still negotiate with those
devices.

**Fix.** Keep paramiko on the 3.x line. If a stray upgrade slipped in, reinstall
the pin:

```bash
.venv/bin/pip install 'paramiko>=3,<4'
```

Then confirm:

```bash
.venv/bin/pip show paramiko | grep -i version
```

### gnmic fails with a TLS handshake error

**Symptom.** `gnmic` reports a TLS handshake failure.

**Diagnosis.** The Splice gNMI server runs without TLS by default.

**Fix.** Add `--insecure` to gnmic:

```bash
gnmic -a 127.0.0.1:9339 --insecure get --path /
```

---

## Runtime / serving

### gNMI Get returns empty / timestamp 1970

**Symptom.** `gnmic get` returns no values, or a timestamp of `0`/1970.

**Diagnosis.** No cached values yet — the poller isn't running, hasn't completed
a sweep, or nothing matched the requested path. This is also the downstream
effect of auth failures or an open circuit breaker (see above).

**Fix.**

- Ensure the server is running and actively polling.
- Build the catalog and aliases: `build-catalog --include-config` then
  `build-aliases`.
- Confirm any values exist at all: `gnmic -a 127.0.0.1:9339 --insecure get --path /`.
- Check the health endpoint and logs for poller/auth/circuit-breaker errors.

### Circuit breaker open

**Symptom.** Logs show `Circuit breaker OPEN — N consecutive failures`, and new
RPCs are refused (`CircuitBreakerOpen`). The health endpoint reports
`circuit_breaker_open: true` with `circuit_breaker_seconds_remaining`.

**Diagnosis.** Too many consecutive NETCONF failures tripped the breaker. There
are two cooldown regimes:

- **Generic failures** (network blips, device overload): after
  `circuit_breaker_threshold` consecutive failures (default **5**) the pool
  pauses for `circuit_breaker_cooldown_sec` (default **60s**).
- **Auth-class failures**: open the breaker **immediately** for
  `auth_failure_cooldown_sec` (default **300s**).

When the cooldown expires, the breaker lets **one** attempt through; success
resets it, another failure re-opens it.

**Fix.**

- Read the logs for the specific failure reason before restarting.
- If auth-related, verify credentials and wait the full (longer) cooldown.
- Monitor the breaker via the health endpoint rather than restart-looping —
  restarting resets in-process timers and can re-trigger device lockouts.
- Tune thresholds/cooldowns under the `poller` config section if your device is
  flakier or stricter than the defaults.

!!! warning "Don't fight the breaker"
    The breaker exists to protect the device from session storms that cause
    account lockouts. Repeatedly restarting Splice to "force a reconnect"
    defeats it. Fix the root cause (credentials, reachability, session limits)
    and let the cooldown elapse.

### Tasks silently dropping out of the poll rotation

**Symptom.** A path that used to update goes stale, with no hard error.

**Diagnosis.** The poller auto-drops a task from rotation after
`poller.task_failure_limit` consecutive failures (default **5**) so one bad path
can't stall the sweep.

**Fix.** Check logs for the failing task, fix the underlying path/filter, and
restart `run` (or re-run `setup`) to re-add it. Raise `task_failure_limit` if
your device is transiently slow on specific subtrees.

### gNMI port already in use

**Symptom.** `run` fails to bind; the gRPC server can't start on its port
(default **9339**).

**Diagnosis.** Another process (a stale Splice instance, or another device's
server) already holds the port. In multi-device setups each device needs its own
gNMI port (9339, 9340, ...).

**Fix.**

- Find the holder and stop it:

  ```bash
  ss -ltnp | grep 9339
  ```

- Or assign a free port via `gnmi.port` in the device's config. The health
  (`9809`) and trigger-API (`9812`) ports can clash the same way — give each
  device distinct values.

### Reconnect-once behaviour

**Symptom.** A single transient RPC failure recovers after a brief pause; logs
show `Reconnecting in 2.0s...`.

**Diagnosis.** This is expected. On RPC failure the pool closes the dead session,
waits `poller.reconnect_delay_sec` (default **2.0s**), reconnects, and retries
the RPC **once**. If the retry also fails, the error propagates immediately — no
retry loops. Auth-class failures skip the retry entirely (straight to cooldown).

**Fix.** None needed for isolated occurrences. Frequent reconnects point to
network instability or device load — increase `task_throttle_ms` and intervals.

---

## Catalog, paths & PM

### Keyed list paths don't match

**Symptom.** A query against a keyed list leaf returns nothing.

**Diagnosis.** Missing list instances or mismatched key order.

**Fix.**

- Run `refresh-list-instances`, or enable `poller.list_refresh_sec`.
- Use `list-keyed-paths` to see the expanded concrete paths.
- Key order is normalized by list key order. If a list has no catalog
  `list_keys`, request the list **root** rather than a leaf.

### PM / FEC-BER values missing

**Symptom.** PM telemetry (e.g. FEC-BER) is absent.

**Diagnosis.** PM models are vendor-specific and may live under different roots,
or PM filters may be pinned to a non-active interface.

**Fix.**

- Enable PM discovery (`discovery.pm_discovery: true`) and rebuild the catalog.
- Use `pm_fanout` / `pm_selector` to target the active interface.
- Query the unkeyed PM prefix (it matches everything below it):

  ```bash
  gnmic -a 127.0.0.1:9339 --insecure get --path "/pm"
  ```

  PM paths are instance-keyed, e.g.
  `/pm/otnpm-data[interface=...]/FEC/...`.

### PM ghost instances after re-provisioning

**Symptom.** PM data for a port goes empty (not error) after the device was
re-provisioned — for example a line port re-provisioned to a different optical
type. `doctor`'s `pm_filters` check fails with "PM filter(s) reference instances
no longer on the device".

**Diagnosis.** Devices answer a subtree filter for a **non-existent** instance
with *empty data and no error*. So an instance-keyed PM filter that points at a
key the device no longer has silently returns nothing. The `pm_filters` doctor
check connects to the device, resolves the live list instances, and flags any
stored filter whose keys are now "ghosts". It also flags a `pm_selector` that
matches **0** current instances.

**Fix.**

- Re-run `setup` (or `build-catalog`) to rebuild PM filters against the current
  instances, **or**
- Start `run` with `discovery.pm_refresh_on_run` enabled — it re-resolves the
  fanout list at startup and rebuilds filters on drift automatically.

### `build-catalog` seems slow or "hangs"

**Symptom.** Catalog build appears stuck.

**Diagnosis.** A large model catalog or in-progress list-instance discovery —
not a true hang.

**Fix.**

- Keep `discovery.resolve_all_lists: false` (the default).
- Watch logs for batch/progress counters.
- Throttle device load with `list_batch_size` and `list_sleep_sec`.

---

## Database & migrations

### Pending or drifted migrations

**Symptom.** `doctor`'s `migrations` check reports `N pending migrations`, or
commands error referencing missing tables/columns.

**Diagnosis.** The database schema is behind the code. Migrations live in
`migrations/` and are applied by `setup` or the `migrate` command.

**Fix.**

```bash
splice --config config.yaml migrate
# then, if you want to skip the migrate step during a full pipeline:
splice --config config.yaml setup --skip-migrate
```

### setup/migrate hangs or fails around migrations

**Symptom.** `setup` or `migrate` blocks or fails while applying migrations.
`doctor`'s `db_lock_blockers` check may report blocking PIDs.

**Diagnosis.** Another `setup`/`migrate` is running against the same database, or
long-lived `idle in transaction` sessions are holding locks.

**Fix.**

1. Run pre-flight checks first:

   ```bash
   splice --config config.yaml doctor
   splice --config config.yaml migrate --dry-run
   ```

2. Inspect blockers directly:

   ```sql
   SELECT pid, usename, state, wait_event_type, wait_event,
          now() - xact_start AS xact_age, query
   FROM pg_stat_activity
   WHERE datname = current_database()
   ORDER BY xact_start NULLS LAST;
   ```

3. If needed, clear a stale blocker (DBA/admin action):

   ```sql
   SELECT pg_terminate_backend(<pid>);
   ```

4. Re-run migrations, then setup without the migrate step:

   ```bash
   splice --config config.yaml migrate
   splice --config config.yaml setup --skip-migrate
   ```

**Prevention.**

- Never run multiple `setup`/`migrate` commands concurrently against the same
  DB.
- Prefer one DB per environment/device pipeline.
- Keep migration fail-fast settings at defaults, or tune via env vars:
  - `SPLICE_MIGRATION_LOCK_WAIT_SEC`
  - `SPLICE_MIGRATION_LOCK_TIMEOUT_MS`
  - `SPLICE_MIGRATION_STATEMENT_TIMEOUT_MS`
  - `SPLICE_MIGRATION_IDLE_TX_TIMEOUT_MS`

### PostgreSQL tests skipped

**Symptom.** Tests with the `_pg` suffix are skipped.

**Diagnosis.** `TEST_DATABASE_URL` is not set; those tests need a live database.

**Fix.**

```bash
TEST_DATABASE_URL='postgresql+psycopg://postgres:postgres@localhost:5432/splice' \
  .venv/bin/python -m pytest
```

---

## Trigger API & Kafka

### Trigger request returns UNIMPLEMENTED

**Symptom.** A trigger request is rejected as `UNIMPLEMENTED`.

**Diagnosis.** Trigger mode isn't enabled.

**Fix.**

- Set `trigger.enabled: true`.
- For the HTTP endpoint, also set `trigger_api.enabled: true` and
  `trigger_api.bearer_token`.
- Restart the bridge process.

### Trigger request returns INVALID_ARGUMENT

**Symptom.** A trigger request is rejected as `INVALID_ARGUMENT`.

**Diagnosis.** Invalid payload or invalid path(s).

**Fix.**

- For the gNMI extension (`id=61001`), the payload must be a JSON object with
  `{"mode":"TRIGGER"}`.
- Use the correct field types: `publish_kafka` (bool), `auto_onboard` (bool),
  `interval_sec` (integer > 0).
- Ensure requested paths exist in catalog/aliases and aren't empty strings.

### HTTP trigger endpoint returns 401

**Symptom.** The HTTP trigger endpoint (default port `9812`) returns `401`.

**Diagnosis.** Missing or invalid bearer token.

**Fix.**

- Send `Authorization: Bearer <trigger_api.bearer_token>`.
- Confirm the config token exactly matches the request token.

### Kafka publishing not working

**Symptom.** No telemetry/trigger events reach Kafka.

**Diagnosis.** Kafka disabled, misconfigured brokers, or the optional dependency
isn't installed.

**Fix.**

- Install the extra: `.venv/bin/pip install -e .[kafka]`
- Set `kafka.enabled: true` and a non-empty `kafka.brokers`.
- Verify broker reachability from the bridge host/container.
- Confirm topic names: `<topic_prefix>.<device>.poll` and
  `<topic_prefix>.<device>.trigger`.
