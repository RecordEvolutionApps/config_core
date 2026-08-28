# config_core

Agent-facing **configuration tools** for IronFlock apps: WAMP RPCs that let
the platform's AI agents list, inspect, create, update and delete an app's
config rows on the gateway the agent targets.

Two entry points, one wire contract:

- **`register_asset_tools`** — the collector preset (MTConnect, Modbus,
  BACnet, IO-Link, PLC, …): manages the `assets` / `datapoints` /
  `assetstatus` tables, seeds and coerces the collector_core contract
  columns, and adds the per-datapoint switch tools.
- **`register_config_tools`** — the generic form for any other app: manages
  ONE named-row config table of the app's choosing, seeds nothing the app
  did not declare, and has no datapoint tools.

Both register the same topics with the same parameter names and response
envelope, so agent tool schemas and prompts written for one work for the
other.

## Ownership principle

The app owns its config table. Its shape and configuration parameters
differ per app and are decided by the app, not by this library.
`config_core` encapsulates only the repetitive parts that are identical in
every app: the reads, the append/echo-merge/soft-delete write mechanics,
idempotent no-change skips, read-back verification and the never-raise
agent responses. The app contributes exactly two things:

1. **One validation function** — receives a candidate asset config and
   returns either problems or the valid (normalized, defaults applied)
   config. Every app-specific rule (columns, drivers, required fields,
   datapoint-spec grammar) lives inside it.
2. **Its ai-template tool declarations** — the app documents its config
   fields to the agent in its own tool descriptions and system prompt
   (copy-paste blocks below).

Diagnostics are deliberately **not** tools: enable `data_access: true` on
the agent in the app's ai-template so it inspects `error-logs`,
`measurements` and `assetstatus` with SQL, guided by
`SQL_DIAGNOSTICS_GUIDANCE` (below).

## Wiring (the whole app-side integration)

`requirements.txt` (git tag pin, no PyPI; the image needs the `git` binary):

```
ironflock>=1.6.0
ironflock-config-core @ git+https://github.com/RecordEvolutionApps/config_core.git@v1.0.0
```

In the app's `ProtocolAdapter.start_background(collector)` — the seam where
a live WAMP session exists (import inside the method so a broken package
degrades the assistant, never collection):

```python
try:
    from config_core import register_asset_tools
    from protocols.asset_validation import validate_my_asset
except Exception as e:
    await collector.report_error(
        f"config tools unavailable: {e}", level="warn",
        user_message="The AI assistant's configuration tools could not be "
                     "loaded - it can advise but not apply changes.")
else:
    await register_asset_tools(collector.ironflock, validate_my_asset)
```

`register_asset_tools(ironflock, validate, topics=DEFAULT_TOPICS,
name_pattern=..., audit_column=None, max_assets=200, device_key=None)`
returns the list of topics that FAILED to register (empty = success) and
never raises. Optional knobs: `topics` may be a partial dict (an operation
mapped to `None` is skipped, e.g. no delete tool), `audit_column` (e.g.
`"configured_by"`) is stamped `"agent"` on every write when the app's
data-template declares that column, `max_assets` caps creations per gateway,
`device_key` defaults to the platform-injected `DEVICE_KEY` env var.

**Gateway scoping is automatic.** The RPC URIs are device-scoped
(`{swarm}.{device_key}.{app}.{env}.{topic}`), so the agent targets one
gateway's instance and that instance only ever configures itself:
`gateway_id` is hard-coded to the local device key on every read and write,
and no tool accepts a gateway parameter.

## Generic config tools (`register_config_tools`)

For apps whose config rows are not collector assets — an egress connection,
a target definition, any named-row config table:

```python
await register_config_tools(
    ironflock, validate_my_row,
    "connections",                     # the app's config table
    noun="connection",                 # prose in hints and toasts
    create_defaults={"enabled": True}, # seeded UNDER agent fields on create
    status_table="assetstatus",        # or None: no live_status join
    # topics=, name_pattern=, audit_column=, max_entries=200, device_key=
)
```

Same return convention as `register_asset_tools` (failed topics, never
raises) and the same wire contract: default topics
`app_assets.list/get/create/update/delete`, parameter `asset_name`, identity
`(gateway_id, asset_name)`, identical response envelope. The differences:

- **Nothing is seeded beyond `create_defaults`.** A validate function that
  rejects unknown columns works — an empty create passes it exactly
  `asset_name` and `gateway_id` (plus your own declared defaults).
- **No column coercion.** The collector's `enabled`/`demo_mode`/
  `collect_interval`/`datapoint_spec` hygiene does not run; a string
  `"false"` for a boolean column reaches your validate as-is. Coerce there.
- **No datapoint tools**, no datapoint summary in `get`, no
  `datapoints_deleted`/`datapoints_failed` fields on `delete`, no
  `datapoint_spec` preview in `list`.
- **`status_table=None`** also drops `live_status` from `list`/`get`.

Everything else in this README — the validate contract, response envelope,
write semantics, secret scan, concurrency — applies to both entry points;
collector-only behavior is marked as such.

## The validate function (the app contract)

```python
async def validate_my_asset(config, existing):   # sync also accepted
    """Return (valid_config, problems).

    config:   the full candidate row. On update it is the existing row
              echo-merged with the agent's changes; on create it is the
              core defaults plus the agent's fields. The collector_core
              contract columns (enabled, demo_mode, collect_interval,
              datapoint_spec) arrive pre-normalized: real booleans, int
              interval >= 1.
    existing: the current latest row, or None on create.
    """
```

Return the valid — possibly normalized, defaults filled — config with an
empty problems list, or any problems as a list of strings (return all of
them at once so the agent fixes everything in one round). Raising also
rejects, with the exception message as the problem. **Fail-closed:** no
valid config means no write. After your function returns, the library
re-stamps the protected columns (`asset_name`, `gateway_id`, `deleted`,
audit column, fresh `tsp`) — a validate function cannot rename, move or
tombstone an asset.

### What `config` holds at call time

| | create | update |
|---|---|---|
| base | the create defaults — preset: `CORE_DEFAULTS` (`datapoint_spec=""`, `collect_interval=5`, `enabled=True`, `demo_mode=False`); generic: your `create_defaults` or nothing | the stored row minus the platform columns `tsp`/`latest_flag`/`authid`/`device_key` |
| on top | the agent's fields | the agent's changes (explicit `null` blanks, omitted keys stay) |
| identity | `asset_name` and `gateway_id` already stamped | already in the stored row |
| `existing` | `None` | the full stored row |

Both paths run the protected-column partition and the secret scan on the
agent's input **before** calling you — plus, on the collector preset only,
the core-column coercion — so `config` never contains a write to a
protected column. What you return is written verbatim after the re-stamp:
anything left in the dict lands in the table, anything you drop does not.

**Non-collector apps: do not use `register_asset_tools`.** Its create path
seeds `CORE_DEFAULTS` whether or not your table has those columns, so a
validate function that rejects unknown column names — the right behaviour
against a hallucinating agent — rejects *every* create, an empty one
included, naming `datapoint_spec`, `collect_interval` and `demo_mode`.
`register_config_tools` exists precisely for this: it seeds only your own
`create_defaults`. (It also seeds no `enabled=True` — an app whose contract
is "missing `enabled` means OFF" simply leaves it out of its defaults.)

## Tools and topics

| Operation        | Default topic          | Purpose |
|------------------|------------------------|---------|
| `list`           | `app_assets.list`      | all assets of this gateway + live status |
| `get`            | `app_assets.get`       | one asset: full config, live status, datapoint summary |
| `create`         | `app_assets.create`    | new asset (collection starts immediately) |
| `update`         | `app_assets.update`    | partial update via echo-merge |
| `delete`         | `app_assets.delete`    | soft delete, cascades to datapoint rows |
| `list_datapoints`| `app_datapoints.list`  | *(preset only)* the live datapoint catalog of one asset |
| `set_datapoints` | `app_datapoints.set`   | *(preset only)* per-datapoint `enabled` / `change_detection` switches + `demo_value` / `demo_variance` |

All apps share these topic names — the platform scopes the URIs per device.
`register_config_tools` registers only the five `app_assets.*` operations.

## Response contract

Every response is a JSON-serializable dict and every handler is never-raise
(an exception would reach the agent as an opaque WAMP error; a structured
dict tells it how to recover). Envelope:

```
{"ok": bool,
 "status": "ok|applied|deleted|no_change|dry_run|rejected|conflict|failed|unverified",
 "code":   when not ok: not_found | already_exists | invalid_name |
           protected_column | invalid_value | secret_rejected |
           limit_exceeded | validation_error | conflict | link_error |
           write_failed | internal_error,
 "problems": [...], "warnings": [...], "ignored": [...],
 "suggestions": [...], "hint": "..."}
```

Notable per-tool fields:

- **list** — `count`, `assets` (platform columns stripped; preset adds
  `datapoint_spec` as `{chars, preview}`; `live_status` joined from the
  status table when one is configured).
- **get** — `asset` (full row, spec capped at 32 KB in the response),
  `_meta` `{tsp, last_changed_by, auto_registered?, <audit_column>?}`;
  `live_status` when a status table is configured; preset adds the
  `datapoints` summary `{count, disabled, change_detection, ids}`.
  Unknown names return difflib `suggestions` plus the `available` names.
- **create / update** — `applied` (the written row) or `would_write`
  (dry_run), `changed_fields` `{col: {from, to}}`, `previous` (pre-write row
  → one-call undo), `verified` (read-back confirmed our row is latest);
  `superseded: true` + `current` when a concurrent writer landed after us
  (`ok` stays true — our row is in the append-only history).
- **delete** — `previous`; preset adds `datapoints_deleted`,
  `datapoints_failed` (call again to finish a partial cascade).
- **set_datapoints** *(preset)* — per-item `results`,
  `applied`/`no_change`/`rejected`/`failed` counts.

Failure classes: `rejected` (your input; fix and retry), `conflict`
(`expected_tsp` mismatch; re-read), `failed` (`link_error` /
`write_failed` / `internal_error` — the platform link or the tool itself;
the `error` field carries the SDK's real message). Hint strings are
steering text for the agent and are NOT part of the semver contract.

## Write semantics

- **Append-only tables.** Assets live in an append-only table with identity
  `(gateway_id, asset_name)`; the platform serves the latest row per
  identity. An update is a full-row append: the library echo-merges the
  existing row (minus the platform columns `tsp`/`latest_flag`/`authid`/
  `device_key` and the runtime `datapoint_list` key) with your changes, so
  **omitted fields are preserved and an explicit `null` blanks a field**.
- **Idempotence.** Every applied asset write restarts that asset's
  collection task, so writes that change nothing are skipped
  (`no_change`, numeric/boolean-normalized comparison) — re-applying a
  config is free.
- **Live pickup.** The running collector subscribes to the assets table:
  a write reconfigures the asset immediately, and the adapter's
  `prepare_datapoints` regenerates the datapoints catalog from the asset
  config. No restart, no extra plumbing, **no app-side extraction function**.
- **Datapoints.** Only `enabled`, `change_detection`, `demo_value` and
  `demo_variance` are user-owned (preserved across rediscovery); everything
  else is machine-written and `set_datapoints` rejects it. All four apply on
  the next publish cycle WITHOUT restarting the connection. The demo pair
  shapes what demo mode emits for the datapoint — `demo_value` is the value it
  sits at in engineering units (a boolean instead gives a resting state),
  `demo_variance` how far it may wander; null on either restores the default.
- **Delete** is a soft delete (measurement history is retained, the name
  becomes reusable) and cascades tombstones to the asset's datapoint rows.
  It is convergent: calling it again finishes a partial cascade.
- **Concurrency.** Mutations are serialized per instance with a lock;
  concurrent board/discovery writers are handled by optional `expected_tsp`
  (compare-and-set against the tsp from `get`) and by `superseded`
  detection on the read-back. Editing an `auto_registered` asset warns that
  the network scanner may rewrite identity fields.
- **Secrets never go into tables.** Column names matching
  password/token/key/certificate patterns and values containing PEM blocks,
  URL-embedded credentials or JWTs are rejected — use the platform secret
  store / device environment. The PEM rule is block-type-blind on purpose: a
  **public certificate is refused too**, whatever the column is called, so
  the agent cannot write a CA or client certificate and a create carrying one
  fails as a whole. TLS apps let the user paste certificates into the app's
  own form; editing such an asset later still works, because the scan only
  sees the agent's changed fields, never the echo-merged row.
- **Operator visibility.** Applied mutations emit an info toast (a live
  audit feed on the board); write failures and partial cascades warn;
  internal errors report as errors. Rejections and no-ops stay silent.

## ai-template blocks (copy-paste per app)

Paste into your configuring agent's `tools`, edit only the two spots marked
`app-specific`, set `data_access: true` on the agent, append the two
guidance texts to its system prompt and give it a few extra
`max_iterations` for the list → dry_run → apply → verify loop.

Every block declares `device_key`. The platform adds that parameter to any
tool with a `topic` and strips it before your handler runs — it addresses
the call to one gateway, and the agent resolves it with the built-in
`list_devices` tool. Declaring it is what gives it a useful description,
and it is what keeps it legal under the `additionalProperties: false` these
blocks set. Keep it in `required`.

```yaml
    list_assets:
      description: >-
        Lists every configured asset on the given gateway with its fields,
        enabled/paused state, demo mode, collect interval and live status
        (online/offline/paused plus last error detail). Call it first to
        see what exists before creating, updating, deleting or
        troubleshooting anything.
      topic: app_assets.list
      parameters:
        type: object
        properties:
          device_key:
            type: integer
            description: >-
              Gateway to act on, from list_devices. Assets live per
              gateway - use the same one all session.
          include_deleted:
            type: boolean
            description: Also list soft-deleted assets. Default false.
        required: [device_key]
        additionalProperties: false

    get_asset:
      description: >-
        Fetches one asset by name: every configured field, the live status
        (online/offline/paused with error detail) and a datapoint summary.
        ALWAYS call it a few seconds after create_asset/update_asset to
        verify the collector accepted the change (status online = data is
        flowing). Unknown names return closest-match suggestions. Pass the
        returned _meta.tsp as expected_tsp on later updates to guard
        against concurrent edits.
      topic: app_assets.get
      parameters:
        type: object
        properties:
          device_key:
            type: integer
            description: >-
              Gateway to act on, from list_devices. Assets live per
              gateway - use the same one all session.
          asset_name:
            type: string
            description: Asset name from list_assets.
        required: [device_key, asset_name]
        additionalProperties: false

    create_asset:
      description: >-
        Creates a new asset on the given gateway and starts collecting
        immediately. asset_name must be unique, 3-50 chars, letters, digits
        and spaces only. Pass the connection fields in fields (see the
        field reference in your instructions). Always call with dry_run
        true first, show the user exactly what will be created and apply
        only after they confirm.
      topic: app_assets.create
      parameters:
        type: object
        properties:
          device_key:
            type: integer
            description: >-
              Gateway to act on, from list_devices. Assets live per
              gateway - use the same one all session.
          asset_name:
            type: string
            description: Unique name, 3-50 chars, letters/digits/spaces.
          fields:
            type: object
            # app-specific: document your columns here
            description: >-
              Connection fields for this app, e.g. {"host": "192.168.0.10"}.
              Optional core fields: collect_interval (seconds), enabled,
              demo_mode, datapoint_spec.
          dry_run:
            type: boolean
            description: Validate and preview without writing.
        required: [device_key, asset_name, fields]
        additionalProperties: false

    update_asset:
      description: >-
        Partially updates one asset: send ONLY the fields to change in
        changes; omitted fields keep their values; an explicit null clears
        a field. asset_name and gateway cannot change (rename = create new
        + delete old). An applied update briefly restarts the asset's
        collection; unchanged values are skipped without a restart. Use
        dry_run true to preview, confirm with the user, then apply and
        verify with get_asset.
      topic: app_assets.update
      parameters:
        type: object
        properties:
          device_key:
            type: integer
            description: >-
              Gateway to act on, from list_devices. Assets live per
              gateway - use the same one all session.
          asset_name:
            type: string
            description: Asset to update (from list_assets).
          changes:
            type: object
            # app-specific: document your columns here
            description: >-
              Only the fields to change, e.g. {"collect_interval": 30}.
          expected_tsp:
            type: string
            description: _meta.tsp from get_asset; rejects if changed since.
          dry_run:
            type: boolean
            description: Validate and preview without writing.
        required: [device_key, asset_name, changes]
        additionalProperties: false

    delete_asset:
      description: >-
        Soft-deletes one asset and its datapoint catalog; collection stops
        immediately, measurement history is kept and the name becomes
        reusable. This is destructive: state exactly what will be deleted
        and get the user's explicit confirmation before calling. The
        response includes the previous config so an accidental delete can
        be undone via create_asset.
      topic: app_assets.delete
      parameters:
        type: object
        properties:
          device_key:
            type: integer
            description: >-
              Gateway to act on, from list_devices. Assets live per
              gateway - use the same one all session.
          asset_name:
            type: string
            description: Asset to delete (from list_assets).
          expected_tsp:
            type: string
            description: _meta.tsp from get_asset; rejects if changed since.
        required: [device_key, asset_name]
        additionalProperties: false

    list_datapoints:
      description: >-
        Lists the live datapoint catalog of one asset: id, name, units,
        address details and the per-datapoint enabled / change_detection
        switches. The catalog itself is derived from the asset's
        configuration (or device discovery) - to add or remove datapoints,
        update the asset instead.
      topic: app_datapoints.list
      parameters:
        type: object
        properties:
          device_key:
            type: integer
            description: >-
              Gateway to act on, from list_devices. Assets live per
              gateway - use the same one all session.
          asset_name:
            type: string
            description: Asset whose datapoints to list.
        required: [device_key, asset_name]
        additionalProperties: false

    set_datapoints:
      description: >-
        Sets the user-owned fields of one asset's datapoints: enabled (false
        stops collecting that datapoint), change_detection (true stores only
        value changes), and for demo mode demo_value (the value it should
        sit at, in engineering units - or a boolean for a resting state) and
        demo_variance (how far it may wander). Applies on the next cycle
        WITHOUT restarting the asset's connection. Everything else about a
        datapoint (address, type, scale) is defined by the asset's
        configuration - change that instead.
      topic: app_datapoints.set
      parameters:
        type: object
        properties:
          device_key:
            type: integer
            description: >-
              Gateway to act on, from list_devices. Assets live per
              gateway - use the same one all session.
          asset_name:
            type: string
            description: Asset whose datapoints to change.
          changes:
            type: array
            maxItems: 100
            items:
              type: object
              properties:
                datapoint_id:
                  type: string
                  description: Id from list_datapoints.
                enabled:
                  type: boolean
                change_detection:
                  type: boolean
                demo_value:
                  description: >-
                    Demo mode: the value this datapoint should sit at, in
                    engineering units, or a boolean for a resting state.
                    null restores the default range.
                demo_variance:
                  type: number
                  description: >-
                    Demo mode: how far the reading may wander from
                    demo_value (same units); for a boolean demo_value, the
                    per-sample chance (0-1) of blipping out of that state.
              required: [datapoint_id]
              additionalProperties: false
        required: [device_key, asset_name, changes]
        additionalProperties: false
```

## Prompt guidance (system-prompt text, shipped as constants)

Both constants are written for the **collector preset** ("data flows",
datapoint diagnostics); a `register_config_tools` app writes its own prompt
text, reusing the write-discipline ideas as it sees fit.

`config_core.RECOMMENDED_PROMPT_GUIDANCE` — the write discipline:

> You can list, inspect, create, update and delete assets yourself with the
> asset tools. Every one of them runs ON a gateway and needs its device_key:
> take it from list_devices, ask the user which gateway if several run this
> app, and keep the same one all session - assets are per gateway. Before
> any create/update/delete: run it with dry_run true, show the user exactly
> what will change and apply only after they agree. After applying, verify
> with get_asset a few seconds later - status online means data flows.
> Renaming means create new + delete old.

`config_core.SQL_DIAGNOSTICS_GUIDANCE` — for agents with `data_access`
enabled: the table schema (`error-logs` with the `msg LIKE 'AssetName: %'`
prefix convention, `measurements.data` keyed by datapoint id,
`assetstatus`), the latest/deleted filtering rule on config tables, the
change_detection / demo_mode / store_data caveats and canned example
queries. Compress both texts as needed to fit your agent's 5000-char
system-prompt budget — the constants are the uncompressed reference.

## Testing your integration

The library's own suite runs with `just test`. For the app side, import
your validate function in the app's pytest suite and drive the registered
handlers against a fake SDK handle (see `tests/_fakes.py` for the model:
latest-per-identity reads, raising failures, report_error recording). Keep
a drift test asserting your ai-template's config-tool topics are a subset
of `config_core.DEFAULT_TOPICS` values, that every tool with a `topic`
declares a required integer `device_key`, and that your board's asset-name
validation equals `config_core.ASSET_NAME_PATTERN`.

## Versioning and releases

There is no PyPI package: apps install straight from this repo and pin a
tag, so a release is a version bump committed on `main` plus a matching
`vX.Y.Z` tag (`just release 1.1.0` / `just release-patch` automate it —
keep the git tag and `pyproject.toml` version identical). After a release,
re-pin each consuming app.

Semver is judged by the app- and agent-facing contract:

- **MAJOR** — removing/renaming a response field or default topic, changing
  the `register_asset_tools` / `register_config_tools` or validate-function
  signature, tightening core-column validation so previously-valid writes
  reject.
- **MINOR** — new optional response fields, parameters, tools or topics.
- **PATCH** — hint/toast wording, suggestion quality, bug fixes.

The `ironflock` SDK is **not** a dependency — the session handle is
injected. Apps must pin `ironflock >= 1.6.0` (proper error propagation on
every call; this library surfaces the SDK's error text to the agent and
treats a legacy `None` return as failure defensively).
