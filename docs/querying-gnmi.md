# Querying Telemetry

Once the bridge is running (`splice --config <cfg> run`), it answers standard gNMI/gRPC on the configured port (default `9339`). Any gNMI client works — the examples below use [gnmic](https://gnmic.openconfig.net/), the most common one. This page covers the three RPCs the server implements (Capabilities, Get, Subscribe), the path forms it accepts, and how prefix matching decides what a query returns.

!!! note "What the server reports"
    `splice` advertises gNMI version **0.8.0** and supported encodings **JSON_IETF** and **PROTO**. Values are served from the poller's cache, so a query returns whatever the last completed poll cycle wrote — it does not trigger a fresh NETCONF read (see [Trigger reads](#on-demand-trigger-reads) for that).

## TLS / `--insecure`

The server binds a plain (non-TLS) gRPC port via `add_insecure_port`. Every gnmic call must therefore pass `--insecure`, and clients should target it over a trusted network (localhost or a private management LAN), or front it with a TLS-terminating proxy.

```bash
gnmic -a 127.0.0.1:9339 --insecure capabilities
```

## Capabilities

Capabilities is the cheapest way to confirm the server is alive and see which YANG models back the catalog.

```bash
gnmic -a 127.0.0.1:9339 --insecure capabilities
```

```text
gNMI version: 0.8.0
supported models:
  - org-openroadm-device, 2023-05-26
  - ...
supported encodings:
  - json_ietf
  - proto
```

The model list mirrors the YANG modules discovered from the device, so it is also a quick sanity check that discovery ran.

## Get a path

`Get` returns the current cached value(s) at or below a path. Use it for one-off reads and scripting.

```bash
# Everything in the catalog (large — use to explore)
gnmic -a 127.0.0.1:9339 --insecure get --path /

# A single subtree
gnmic -a 127.0.0.1:9339 --insecure get --path /org-openroadm-device/node-id
```

You can pass multiple `--path` flags in one call; each is matched independently and all results come back in one response.

!!! example "Optical power and frequency"
    ```bash
    # Output optical power for a component
    gnmic -a 127.0.0.1:9339 --insecure \
      get --path '/components/component[name=port-1]/optical-channel/state/output-power'

    # Carrier frequency on a keyed optical channel
    gnmic -a 127.0.0.1:9339 --insecure \
      get --path '/components/component[name=port-1]/optical-channel/state/frequency'
    ```

## Subscribe

`Subscribe` is the streaming RPC. The server supports all three subscription modes.

### `once` — snapshot then done

Returns the current values for the path, a `sync_response`, then closes. Equivalent to `Get` but in the subscription envelope that collectors like Telegraf expect.

```bash
gnmic -a 127.0.0.1:9339 --insecure \
  subscribe --mode once --path '/components/component[name=port-1]/optical-channel/state/output-power'
```

### `stream` — continuous sampling

Streams an initial snapshot and `sync_response`, then re-emits values on an interval. The default streaming mode is **SAMPLE**; control the cadence with `--sample-interval`.

```bash
gnmic -a 127.0.0.1:9339 --insecure \
  subscribe --mode stream --stream-mode sample --sample-interval 10s \
  --path '/components/component[name=port-1]/optical-channel/state/output-power'
```

!!! note "Sampling defaults and cadence"
    If the client does not specify a sample interval, the server samples every **5 seconds**. Sampled values come from the poller cache, so setting `--sample-interval` faster than your `poller` interval just re-emits the same cached value until the next poll lands. The server also emits an empty heartbeat notification roughly every `heartbeat_sec` (default 10s) to keep the stream alive. `ON_CHANGE` and `TARGET_DEFINED` stream modes are accepted; `ON_CHANGE` emits only when the cached value version changes.

### `poll` — client-driven

The client opens the stream, gets the initial snapshot + sync, then pulls fresh snapshots by sending poll requests.

```bash
gnmic -a 127.0.0.1:9339 --insecure \
  subscribe --mode poll \
  --path '/components/component[name=port-1]/optical-channel/state/output-power'
```

## Path forms

`splice` accepts three equivalent ways to write a path. They are normalised internally, so a query in any form matches catalog entries stored in canonical form.

| Form | Example | Notes |
|------|---------|-------|
| **Canonical** (module-qualified) | `/p1:components/p1:component` | YANG module prefix on each element. The form the catalog stores. |
| **Alias** (no prefix) | `/components/component` | Namespace prefixes stripped. Convenient for typing. |
| **Keyed** (list predicate) | `/components/component[name=port-1]` | Selects an instance of a keyed list. |

Normalisation strips the module prefix before each `/`-separated element (`/p1:components/p1:component` → `/components/component`), so canonical and alias queries are treated the same. You can mix them freely.

### Keyed predicates

A list element is selected with a `[key=value]` predicate. Multiple keys go in one bracket, comma-separated:

```bash
gnmic -a 127.0.0.1:9339 --insecure \
  get --path '/pm/otnpm-data[interface=1/2/n1/ot400,managed-element=1]/FEC/acor-factt:fec-ber'
```

!!! tip "Key order and encoding don't matter"
    The server canonicalises predicates before matching: keys are sorted (by the model's key order when known, else alphabetically) and values are percent-decoded then re-encoded. So `[managed-element=1,interface=x]` and `[interface=x,managed-element=1]` match the same instance, and a value with reserved characters like `/` works whether you wrote it raw or percent-encoded. Quote the whole path in your shell so `[`, `]`, and `/` survive.

## How prefix matching works

Every Get/Subscribe path is treated as a **prefix**. The server returns every catalog leaf at or below it. Matching is boundary-aware: a stored path counts as a descendant only when it continues with `/` (the next path element) or `[` (a predicate on the prefix's last element).

This boundary rule prevents false positives — querying `/system` will **not** match `/system-backup`, because the stored path must continue with `/` or `[`, not just any character.

!!! warning "Query the list path, not a leaf below an unkeyed list"
    Because matching is prefix-based against stored (often keyed) catalog entries, the path you query matters:

    - **Querying a keyed list path returns all its instances.** `/components/component` (no predicate) matches every `/components/component[name=...]/...` leaf — one prefix, all instances.
    - **A deep leaf template under an *unkeyed* list element matches nothing.** A path like `/components/component/optical-channel/state/output-power` (no `[name=...]`) is not a prefix of the stored keyed paths `/components/component[name=port-1]/optical-channel/...`, because after `component` the stored path continues with `[`, not `/optical-channel`. The unkeyed deep-leaf form has no boundary-correct match.

    **Fix:** either query the **list path** (`/components/component`) to get every instance, or query a **keyed path** (`/components/component[name=port-1]/optical-channel/state/output-power`) to get one. Don't write an unkeyed path that descends *through* a list element.

### Finding the right path

If you're unsure of the exact string, use the CLI to search the catalog before querying:

```bash
splice --config <cfg> find-paths --pattern output-power
splice --config <cfg> find-paths --pattern frequency
```

Run `splice --help` for the full command list, including `list-jobs` and semantic `search`.

## On-demand trigger reads

Standard `Get` returns cached values. To force a fresh NETCONF read at query time, the bridge supports a gNMI extension (the optional Trigger API, off by default — see `trigger`/`trigger_api` in [Configuration](configuration.md)). When enabled, a Get carrying the trigger extension makes the server poll the device immediately and return live values, optionally onboarding the path as a new job. When the trigger handler is disabled, a trigger-tagged Get is rejected with `UNIMPLEMENTED`.

## Troubleshooting queries

| Symptom | Likely cause / fix |
|---------|--------------------|
| Empty Get/Subscribe response | The poller hasn't completed a cycle yet — wait one `poller` interval. Or the path doesn't match any catalog leaf. |
| A deep leaf path returns nothing | You queried through an unkeyed list element. Query the list path or add the `[key=value]` predicate (see warning above). |
| `Set is disabled` / `Trigger is disabled` | The corresponding handler isn't enabled in config; these RPCs return `UNIMPLEMENTED` by default. |
| Connection refused | Server not running, wrong port, or you targeted the wrong host. Confirm with `capabilities`. |
| TLS handshake errors | The port is plaintext gRPC — add `--insecure`. |

See [Troubleshooting](troubleshooting.md) for device-side issues (session limits, auth lockouts, circuit breaker).

## See also

- [Quickstart](quickstart.md) — get a server running end to end
- [Configuration](configuration.md) — `gnmi`, `poller`, `trigger` settings
- [Python API](python-api.md) — read telemetry programmatically without gnmic
