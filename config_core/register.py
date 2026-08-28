"""Register the config RPCs on an injected ironflock SDK handle.

Requires ironflock >= 1.6.0 (proper error propagation on every call). The
registered URI is ``{swarm_key}.{device_key}.{app_key}.{env}.{topic}`` -- the
device key is part of the topic, so the platform routes an agent's tool call
to exactly one gateway's instance, and this instance only ever configures
itself. The SDK replays registrations on reconnect, so one call here is
durable.

Two entry points, one wire contract:

* ``register_asset_tools`` -- the collector preset: the ``assets`` /
  ``datapoints`` / ``assetstatus`` tables, the collector_core contract
  columns seeded and coerced, the datapoint tools, collector prose.
* ``register_config_tools`` -- the generic form: any named-row config table,
  no seeding beyond what the app declares, no datapoint tools. The wire
  vocabulary (topics ``app_assets.*``, parameter ``asset_name``, response
  fields) is deliberately the same, so agent tool schemas are identical for
  both.
"""

import os

from .handlers import (
    ASSET_NAME_PATTERN,
    ToolConfig,
    _Entity,
    _generic_hints,
    rpc_handlers,
)
from .store import AssetStore

DEFAULT_TOPICS = {
    "list": "app_assets.list",
    "get": "app_assets.get",
    "create": "app_assets.create",
    "update": "app_assets.update",
    "delete": "app_assets.delete",
    "list_datapoints": "app_datapoints.list",
    "set_datapoints": "app_datapoints.set",
}

# The operations a generic entity has -- everything but the datapoint tools.
_CRUD_OPS = ("list", "get", "create", "update", "delete")


async def register_asset_tools(ironflock, validate, topics=DEFAULT_TOPICS,
                               name_pattern=ASSET_NAME_PATTERN,
                               audit_column=None, max_assets=200,
                               device_key=None):
    """Register the collector asset CRUD endpoints. Returns the list of
    topics that FAILED to register (empty = success). Never raises: a
    config-tools outage must degrade the assistant, not the app.

    ``validate`` is the app's ONE contribution: (a)sync
    ``validate(config, existing) -> (valid_config, problems)`` -- it owns
    every app-specific rule (columns, drivers, required fields, datapoint
    spec grammar) and may normalize/default the config it returns. No valid
    config means no write.

    What ``validate`` receives differs per path:

    * create -- ``config`` is ``CORE_DEFAULTS`` (the collector contract
      columns ``datapoint_spec``, ``collect_interval``, ``enabled``,
      ``demo_mode``) with the agent's fields on top, plus ``asset_name`` and
      ``gateway_id`` already stamped; ``existing`` is None. An app whose
      assets table does not have those columns is not a collector -- use
      ``register_config_tools`` instead of dropping them in validate.
    * update -- ``config`` is the stored row minus the platform columns
      (``tsp``, ``latest_flag``, ``authid``, ``device_key``) with the
      agent's changes on top; ``existing`` is the full stored row. Nothing
      is seeded here -- the echo-merge supplies the defaults.
    * both -- the agent's input has already been through the
      protected-column partition, the core-column coercion and the secret
      scan, so ``validate`` never sees a write to a protected column.

    Whatever ``validate`` returns is written verbatim once the protected
    columns are re-stamped (``asset_name``, ``gateway_id``, ``deleted``,
    audit column, fresh ``tsp``): anything left in the dict lands in the
    table, anything dropped from it does not.

    ``topics`` may be a partial dict -- missing operations use the defaults;
    an operation mapped to None is skipped entirely (e.g. an app that wants
    no delete tool). ``audit_column`` (e.g. "configured_by") is stamped
    "agent" on every write when the app's data-template declares the column.
    ``device_key`` defaults to the platform-injected DEVICE_KEY env var.

    All operator notification -- registration failures, applied-write audit
    toasts, failure warns -- goes through ``ironflock.report_error``
    directly; there is no report callback.
    """
    merged = dict(DEFAULT_TOPICS)
    merged.update(topics or {})
    wanted = {op: topic for op, topic in merged.items()
              if op in DEFAULT_TOPICS and topic}

    def make_tools(device_key_int):
        store = AssetStore(ironflock, device_key_int)
        cfg = ToolConfig(validate, name_pattern=name_pattern,
                         audit_column=audit_column, max_assets=max_assets)
        return store, cfg

    return await _register(ironflock, validate, wanted, device_key,
                           make_tools)


async def register_config_tools(ironflock, validate, table, *,
                                noun="entry", create_defaults=None,
                                status_table=None, topics=None,
                                name_pattern=ASSET_NAME_PATTERN,
                                audit_column=None, max_entries=200,
                                device_key=None):
    """Register generic config CRUD endpoints for one named-row table.
    Returns the list of topics that FAILED to register (empty = success).
    Never raises: a config-tools outage must degrade the assistant, not
    the app.

    The generic counterpart of ``register_asset_tools`` for apps whose
    config rows are not collector assets. The wire contract is identical --
    default topics ``app_assets.list/get/create/update/delete``, parameter
    ``asset_name``, the same response envelope -- so agent tool schemas and
    prompts written for one work for the other; ``noun`` (e.g.
    "connection") only shapes the prose in hints and operator toasts. There
    are no datapoint tools and none of the collector's column seeding or
    coercion:

    * ``table`` is the app's config table; rows are appended with identity
      ``(gateway_id, asset_name)`` exactly like collector assets.
    * ``create_defaults`` (dict, optional) is seeded UNDER the agent's
      fields on create -- declare only columns your table has; nothing else
      is ever added. On update the stored row is echo-merged instead.
    * ``status_table`` (optional) joins a live status row per entry into
      list/get responses (``live_status``); None omits the field and the
      read.
    * ``validate(config, existing)`` receives on create your
      ``create_defaults`` + the agent's fields + ``asset_name`` and
      ``gateway_id``; on update the stored row (minus platform columns) +
      the agent's changes. The agent's input has been through the
      protected-column partition and the secret scan, but NO type coercion
      -- a string "false" for a boolean column reaches you as-is; coerce in
      validate. The returned dict is written verbatim after the protected
      columns are re-stamped.

    ``topics`` may be a partial dict over list/get/create/update/delete --
    missing operations use the defaults; an operation mapped to None is
    skipped. ``audit_column`` and ``device_key`` behave as in
    ``register_asset_tools``; ``max_entries`` caps creations per gateway.
    """
    defaults = {op: DEFAULT_TOPICS[op] for op in _CRUD_OPS}
    merged = dict(defaults)
    merged.update(topics or {})
    wanted = {op: topic for op, topic in merged.items()
              if op in defaults and topic}

    entity = _Entity(
        noun=noun,
        create_defaults=dict(create_defaults or {}),
        coerce=None,
        hints=_generic_hints(noun),
    )

    def make_tools(device_key_int):
        store = AssetStore(ironflock, device_key_int, table=table,
                           datapoints_table=None, status_table=status_table)
        cfg = ToolConfig(validate, name_pattern=name_pattern,
                         audit_column=audit_column, max_assets=max_entries,
                         entity=entity)
        return store, cfg

    return await _register(ironflock, validate, wanted, device_key,
                           make_tools)


async def _register(ironflock, validate, wanted, device_key, make_tools):
    """Shared registration loop: resolve the device key, check validate,
    build the tools, register every wanted topic. Returns the failed
    topics."""
    if device_key is None:
        device_key = os.environ.get("DEVICE_KEY")
    try:
        device_key = int(device_key)
    except (TypeError, ValueError):
        failed = sorted(wanted.values())
        await _notify_failure(
            ironflock,
            f"config tools not registered: DEVICE_KEY unavailable "
            f"({device_key!r})",
        )
        return failed
    if not callable(validate):
        failed = sorted(wanted.values())
        await _notify_failure(
            ironflock,
            "config tools not registered: validate must be a callable "
            "(config, existing) -> (valid_config, problems)",
        )
        return failed

    store, cfg = make_tools(device_key)
    handlers = rpc_handlers(store, cfg)

    failed, reasons = [], []
    for op, topic in wanted.items():
        try:
            registration = await ironflock.register_device_function(
                topic, handlers[op]
            )
        except Exception as e:
            registration = None
            reasons.append(f"{topic}: {e}")
        else:
            # Defensive: SDKs <= 1.5.3 swallowed errors and returned None.
            if registration is None:
                reasons.append(f"{topic}: registration returned no handle")
        if registration is None:
            failed.append(topic)
    if failed:
        await _notify_failure(
            ironflock,
            "failed to register config RPC(s): " + "; ".join(reasons),
        )
    return failed


async def _notify_failure(ironflock, message):
    try:
        await ironflock.report_error(
            message,
            level="warn",
            user_message=(
                "The AI assistant's asset configuration tools could not be "
                "registered on this device - it can advise but not apply "
                "changes."
            ),
        )
    except Exception as e:
        print(f"config_core: registration failure toast failed ({e}): {message}")
