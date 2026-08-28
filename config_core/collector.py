"""The collector preset: everything specific to the IronFlock collector app
family (assets + datapoints + assetstatus tables, the collector_core contract
columns, the datapoint tools, the collector hint texts).

Internal module -- apps import ``register_asset_tools`` from the package and
nothing from here. The generic machinery lives in ``handlers``; this module
supplies the collector's entity (defaults, column hygiene, prose) and the two
datapoint operations that only exist for collector stores. ``handlers``
re-exports this module's constants under their historical
``config_core.handlers.*`` names via a lazy module ``__getattr__``.
"""

import json

from .handlers import (
    _Entity,
    _Hints,
    _coerce_bool,
    _coerce_number,
    _link_failed,
    _mutation_prelude,
    _not_found,
    _ok,
    _rejected,
    _suggest,
)
from .store import normalized_diff, now_iso, strip_platform

# collector_core contract columns present in every collector app. The preset
# owns their hygiene and create defaults; everything else in a row belongs to
# the app and passes through to its validate function untouched.
# Seeded into the create candidate before validate runs (never on update,
# which echo-merges the stored row instead). A non-collector app has no
# business receiving these -- that is what register_config_tools is for.
CORE_DEFAULTS = {
    "datapoint_spec": "",
    "collect_interval": 5,
    "enabled": True,
    "demo_mode": False,
}
BOOL_COLUMNS = ("enabled", "demo_mode")

# The only datapoint columns a user/agent owns; everything else is written by
# discovery/spec parsing and would be overwritten (collector_core
# USER_DATAPOINT_COLUMNS -- kept identical, order included, by a drift test in
# each consuming app). The two switches gate what is collected and stored; the
# two demo settings shape what demo mode produces for the datapoint.
USER_DATAPOINT_COLUMNS = ("enabled", "change_detection", "demo_value", "demo_variance")

MAX_SPEC_CHARS = 65536
DATAPOINT_BATCH_CAP = 100

RECOMMENDED_PROMPT_GUIDANCE = (
    "You can list, inspect, create, update and delete assets yourself with "
    "the asset tools. Every one of them runs ON a gateway and needs its "
    "device_key: take it from list_devices, ask the user which gateway if "
    "several run this app, and keep the same one all session - assets are "
    "per gateway. Before any create/update/delete: run it with dry_run "
    "true, show the user exactly what will change and apply only after they "
    "agree. After applying, verify with get_asset a few seconds later - "
    "status online means data flows. Renaming means create new + delete old."
)

SQL_DIAGNOSTICS_GUIDANCE = (
    "Diagnose problems with SQL on this app's own tables (read access):\n"
    "- error-logs (tsp, level, msg, user_message): recent errors, newest "
    "first. There is no asset column - the asset name is prefixed into msg, "
    "filter with msg LIKE 'AssetName: %'.\n"
    "- measurements (tsp, asset_name, data): the collected batches; data is "
    "JSON keyed by datapoint id. No errors does NOT mean correct data - "
    "check for nulls and implausible magnitudes; those usually mean a wrong "
    "address, data_type, scale or byte order in the datapoint spec.\n"
    "- assetstatus (tsp, asset_name, status, detail): status history.\n"
    "- assets/datapoints hold config history: always add the latest filter "
    "and deleted = false; prefer the asset tools for config reads.\n"
    "Caveats: change_detection datapoints only appear when their value "
    "changes; demo_mode values are synthetic; a gateway with store_data = "
    "false stores no measurements at all.\n"
    "Example - last errors for one asset:\n"
    "  SELECT tsp, level, msg FROM \"error-logs\" "
    "WHERE msg LIKE 'Press 1: %' ORDER BY tsp DESC LIMIT 20\n"
    "Example - latest data batches:\n"
    "  SELECT tsp, data FROM measurements "
    "WHERE asset_name = 'Press 1' ORDER BY tsp DESC LIMIT 5"
)


def _coerce_demo_value(value):
    """(setting_or_None, problem_or_None) -- a number, or a boolean for a
    datapoint that rests in a state. Numbers win where both parse (``1`` is a
    reading of one, not "on"); an explicit null restores the default range."""
    if value is None or isinstance(value, bool):
        return value, None
    number, problem = _coerce_number(value)
    if problem is None:
        return number, None
    boolean, _ = _coerce_bool(value)
    if boolean is not None:
        return boolean, None
    return None, f"{value!r} is not a number or a boolean"


# Per-column coercion for the user-owned datapoint columns: the switches are
# strict booleans, the demo settings numbers (demo_value also takes a boolean).
DATAPOINT_COERCERS = {
    "enabled": _coerce_bool,
    "change_detection": _coerce_bool,
    "demo_value": _coerce_demo_value,
    "demo_variance": _coerce_number,
}


def _coerce_core(fields):
    """Hygiene of the collector_core contract columns. Returns
    (coerced_fields, problems). Explicit None passes through (null semantics
    are handled by the merge); app columns pass through untouched."""
    coerced, problems = dict(fields), []
    for column in BOOL_COLUMNS:
        if column in coerced and coerced[column] is not None:
            value, problem = _coerce_bool(coerced[column])
            if problem:
                problems.append(f"{column}: {problem}")
            else:
                coerced[column] = value
    if "collect_interval" in coerced and coerced["collect_interval"] is not None:
        value = coerced["collect_interval"]
        if isinstance(value, bool):
            problems.append("collect_interval: must be a number of seconds")
        else:
            try:
                interval = int(round(float(value)))
            except (TypeError, ValueError):
                problems.append(
                    f"collect_interval: {value!r} is not a number of seconds"
                )
            else:
                if interval < 1:
                    problems.append("collect_interval: must be >= 1 second")
                else:
                    coerced["collect_interval"] = interval
    if "datapoint_spec" in coerced and coerced["datapoint_spec"] is not None:
        spec = coerced["datapoint_spec"]
        if not isinstance(spec, str):
            problems.append("datapoint_spec: must be a string")
        elif len(spec) > MAX_SPEC_CHARS:
            problems.append(
                f"datapoint_spec: too large ({len(spec)} chars, max "
                f"{MAX_SPEC_CHARS})"
            )
    return coerced, problems


# The collector family's hint texts -- the pre-entity library's exact
# strings, so the preset behaves byte-identically.
_COLLECTOR_HINTS = _Hints(
    list_nonempty=(
        "Fetch one asset's full configuration, live status and datapoint "
        "summary with get_asset. status online = data is flowing; "
        "offline shows the last connection error in detail."
    ),
    list_empty=(
        "No assets configured on this gateway yet - create one with "
        "create_asset (dry_run first)."
    ),
    get_deleted=(
        "Asset {name} was deleted. create_asset re-creates it under "
        "the same name (measurement history under this name resumes)."
    ),
    get_offline=(
        " The asset is offline: query the error-logs table for its "
        "recent errors (see your diagnostics instructions) and propose "
        "a remedy."
    ),
    get_paused=" The asset is paused - update_asset with enabled=true resumes it.",
    create_revive=(
        "{name} previously existed and was deleted - re-creating "
        "revives the name and measurement history under it resumes"
    ),
    create_success=(
        "Created. The collector starts this asset now - call "
        "get_asset in ~15 seconds: status online means data is "
        "flowing, offline shows the connection error. Consider "
        "demo_mode=true to validate dashboards before touching "
        "real hardware."
    ),
    update_no_change=(
        "The asset is already configured this way - nothing was "
        "written (a write would restart its collection task)."
    ),
    update_success=(
        "Updated and the collection task restarts with the new "
        "configuration - a brief offline/online flicker is normal. "
        "Verify with get_asset in ~15 seconds."
    ),
    delete_dry_run=(
        "Would soft-delete asset {name}{dp_clause}. Measurement history "
        "is retained; the name becomes reusable. Confirm with the "
        "user, then re-call with dry_run=false."
    ),
    delete_success=(
        "Soft-deleted: the collector stops the asset's collection task and "
        "measurement history is retained. To undo, call create_asset with "
        "the values in 'previous'."
    ),
)

_COLLECTOR_ENTITY = _Entity(
    noun="asset",
    create_defaults=CORE_DEFAULTS,
    coerce=_coerce_core,
    hints=_COLLECTOR_HINTS,
)


# --------------------------------------------------------------------------
# Datapoint operations (collector stores only)
# --------------------------------------------------------------------------

def _parse_changes_param(value):
    """(list_of_dicts, problem) for set_datapoints ``changes``: one dict, a
    list of dicts, or their JSON-string forms."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None, "changes must be an object or array (got unparsable string)"
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list) or not value:
        return None, "changes must be a non-empty object or array of objects"
    if not all(isinstance(item, dict) for item in value):
        return None, "every changes entry must be an object"
    if len(value) > DATAPOINT_BATCH_CAP:
        return None, f"changes is capped at {DATAPOINT_BATCH_CAP} entries per call"
    return value, None


async def list_datapoints(store, cfg, asset_name):
    name, rows, early = await _mutation_prelude(store, cfg, asset_name)
    if early is not None:
        return early
    existing = next((r for r in rows if r.get("asset_name") == name), None)
    if existing is None:
        return _not_found(cfg, name, rows)
    dp_rows, dp_err = await store.read_datapoints(name)
    if dp_err is not None:
        return _link_failed(dp_err)
    live_dps = sorted(
        (strip_platform(r) for r in dp_rows if not r.get("deleted")),
        key=lambda r: str(r.get("datapoint_id")),
    )
    return _ok(
        "ok",
        "Editable per-datapoint switches: enabled (false stops collecting "
        "it) and change_detection (true stores only value changes) - use "
        "set_datapoints. Everything else is derived from the asset's "
        "configuration; change that instead.",
        count=len(live_dps), datapoints=live_dps,
    )


async def set_datapoints(store, cfg, asset_name, changes=None, dry_run=False):
    items, parse_problem = _parse_changes_param(changes)
    if parse_problem:
        return _rejected(
            "invalid_value", [parse_problem],
            "Pass changes as {datapoint_id, enabled?, change_detection?} or "
            "an array of those.",
        )
    name, rows, early = await _mutation_prelude(store, cfg, asset_name)
    if early is not None:
        return early
    existing = next((r for r in rows if r.get("asset_name") == name), None)
    if existing is None:
        return _not_found(cfg, name, rows)
    dp_rows, dp_err = await store.read_datapoints(name)
    if dp_err is not None:
        return _link_failed(dp_err)
    live_dps = {
        str(r.get("datapoint_id")): r for r in dp_rows if not r.get("deleted")
    }

    results, applied, no_change, rejected, failed = [], 0, 0, 0, 0
    for item in items:
        result = await _set_one_datapoint(store, cfg, live_dps, item, dry_run)
        results.append(result)
        status = result["status"]
        if status in ("applied", "dry_run"):
            applied += 1
        elif status == "no_change":
            no_change += 1
        elif status == "rejected":
            rejected += 1
        else:
            failed += 1

    if applied and not dry_run:
        await store.notify(
            f"agent changed {applied} datapoint switch(es) on asset {name!r}",
            level="info",
            user_message=(
                f"AI assistant changed {applied} datapoint switch(es) on "
                f"asset '{name}'."
            ),
        )
    ok = rejected == 0 and failed == 0
    hint = (
        "Datapoint switches apply on the next publish cycle WITHOUT "
        "restarting the asset's connection."
    )
    if dry_run:
        hint = "Nothing was written (dry_run). " + hint
    if rejected or failed:
        hint = "Some entries were not applied - see results. " + hint
    return {
        "ok": ok,
        "status": "ok" if ok else "rejected",
        "results": results,
        "applied": applied,
        "no_change": no_change,
        "rejected": rejected,
        "failed": failed,
        "hint": hint,
    }


async def _set_one_datapoint(store, cfg, live_dps, item, dry_run):
    datapoint_id = str(item.get("datapoint_id") or "").strip()
    if not datapoint_id:
        return {
            "datapoint_id": item.get("datapoint_id"),
            "status": "rejected",
            "problems": ["datapoint_id is required"],
        }
    extras = [
        k for k in item
        if k != "datapoint_id" and k not in USER_DATAPOINT_COLUMNS
    ]
    if extras:
        return {
            "datapoint_id": datapoint_id,
            "status": "rejected",
            "problems": [
                f"only {', '.join(USER_DATAPOINT_COLUMNS)} are editable - "
                f"{', '.join(sorted(extras))} is written by discovery/spec "
                "parsing and would be overwritten; change the asset's "
                "datapoint spec or the device instead"
            ],
        }
    row = live_dps.get(datapoint_id)
    if row is None:
        return {
            "datapoint_id": datapoint_id,
            "status": "rejected",
            "problems": [f"datapoint {datapoint_id!r} not found on this asset"],
            "suggestions": _suggest(datapoint_id, sorted(live_dps)),
        }
    changes = {}
    for column in USER_DATAPOINT_COLUMNS:
        if column in item:
            value, problem = DATAPOINT_COERCERS[column](item[column])
            if problem:
                return {
                    "datapoint_id": datapoint_id,
                    "status": "rejected",
                    "problems": [f"{column}: {problem}"],
                }
            changes[column] = value
    if not changes:
        return {
            "datapoint_id": datapoint_id,
            "status": "rejected",
            "problems": [
                f"nothing to change - pass one of {', '.join(USER_DATAPOINT_COLUMNS)}"
            ],
        }
    merged = strip_platform(row)
    merged.update(changes)
    if not normalized_diff(merged, row):
        return {"datapoint_id": datapoint_id, "status": "no_change"}
    if dry_run:
        return {
            "datapoint_id": datapoint_id, "status": "dry_run",
            "would_write": changes,
        }
    # Partial write: the platform's carry-over merge preserves the rest of
    # the row (discovery-written columns included).
    payload = {
        "asset_name": row.get("asset_name"),
        "datapoint_id": row.get("datapoint_id"),
        "gateway_id": store.device_key,
        "deleted": bool(row.get("deleted", False)),
        "tsp": now_iso(),
    }
    payload.update(changes)
    acked, append_err = await store.append_datapoint(payload)
    if not acked:
        return {
            "datapoint_id": datapoint_id, "status": "failed",
            "error": append_err,
        }
    return {"datapoint_id": datapoint_id, "status": "applied",
            "applied": changes}
