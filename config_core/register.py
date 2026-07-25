"""Register the asset config RPCs on an injected ironflock SDK handle.

Requires ironflock >= 1.6.0 (proper error propagation on every call). The
registered URI is ``{swarm_key}.{device_key}.{app_key}.{env}.{topic}`` -- the
device key is part of the topic, so the platform routes an agent's tool call
to exactly one gateway's instance, and this instance only ever configures
itself. The SDK replays registrations on reconnect, so one call here is
durable.
"""

import os

from .handlers import ASSET_NAME_PATTERN, ToolConfig, rpc_handlers
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


async def register_asset_tools(ironflock, validate, topics=DEFAULT_TOPICS,
                               name_pattern=ASSET_NAME_PATTERN,
                               audit_column=None, max_assets=200,
                               device_key=None):
    """Register the asset CRUD endpoints. Returns the list of topics that
    FAILED to register (empty = success). Never raises: a config-tools
    outage must degrade the assistant, not the app.

    ``validate`` is the app's ONE contribution: (a)sync
    ``validate(config, existing) -> (valid_config, problems)`` -- it owns
    every app-specific rule (columns, drivers, required fields, datapoint
    spec grammar) and may normalize/default the config it returns. No valid
    config means no write.

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

    store = AssetStore(ironflock, device_key)
    cfg = ToolConfig(validate, name_pattern=name_pattern,
                     audit_column=audit_column, max_assets=max_assets)
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
