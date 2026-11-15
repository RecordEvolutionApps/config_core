"""The collector preset: everything specific to the IronFlock collector app
family (assets + datapoints + datapoint_settings + assetstatus tables, the
collector_core contract columns, the datapoint tools, the collector hint
texts).

One writer per table (collector_core >= 3.0.0): ``datapoints`` is the
gateway's catalog and is only read here; the user-owned per-datapoint
settings live in ``datapoint_settings``, which the board and these tools
write as partial rows. The effective value of a setting is the user's
(non-null) setting, else the catalog's value, else the built-in default.

Internal module -- apps import ``register_asset_tools`` from the package and
nothing from here. The generic machinery lives in ``handlers``; this module
supplies the collector's entity (defaults, column hygiene, prose) and the two
datapoint operations that only exist for collector stores. ``handlers``
re-exports this module's constants under their historical
``config_core.handlers.*`` names via a lazy module ``__getattr__``.
"""

import json

from .handlers import (
    DATAPOINT_ID_PREVIEW,
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
from .store import normalized_equal, now_iso, strip_platform

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

# The datapoint settings a user/agent owns -- the value columns of
# datapoint_settings (collector_core DATAPOINT_SETTINGS_COLUMNS -- kept
# identical, order included, by a drift test in each consuming app; the name
# here stays for the apps' existing imports). Everything else
# about a datapoint is the gateway's catalog. The two switches gate what is
# collected and stored; the two demo settings shape what demo mode produces
# for the datapoint (the catalog may carry spec-derived demo defaults).
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
    "- assets (asset configuration), datapoints (the catalog the gateway "
    "derives from each asset: datapoint_id, name, units, path, default "
    "demo_value/demo_variance) and datapoint_settings (enabled, "
    "change_detection, demo_value, demo_variance, configured_by: the user's "
    "per-datapoint switches) keep every version of a row. Take the newest "
    "row per key FIRST (DISTINCT ON the key ORDER BY tsp DESC), and only "
    "then drop deleted ones with deleted IS NOT TRUE (board rows may leave "
    "deleted null; filtering before picking the newest brings back an older "
    "version of a deleted row). datapoint_settings has no deleted column; a "
    "missing row or a null column means the default: enabled, every value "
    "stored, the catalog's demo values. Every table holds all gateways' "
    "rows: filter gateway_id for one gateway. Prefer list_datapoints, which "
    "merges catalog and settings; prefer the asset tools for config reads.\n"
    "Caveats: change_detection datapoints only appear when their value "
    "changes; demo_mode values are synthetic; a gateway with store_data = "
    "false stores no measurements at all.\n"
    "Example - last errors for one asset:\n"
    "  SELECT tsp, level, msg FROM \"error-logs\" "
    "WHERE msg LIKE 'Press 1: %' ORDER BY tsp DESC LIMIT 20\n"
    "Example - the current, undeleted assets of gateway 42:\n"
    "  SELECT * FROM (SELECT DISTINCT ON (asset_name) * FROM assets "
    "WHERE gateway_id = 42 ORDER BY asset_name, tsp DESC) a "
    "WHERE deleted IS NOT TRUE\n"
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
        "Soft-deleted: the collector stops the asset's collection task, "
        "removes its datapoint catalog, and measurement history is "
        "retained. To undo, call create_asset with the values in "
        "'previous'."
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


# What a datapoint does when neither the user's setting nor the catalog says
# otherwise -- collector_core's own reading: collected unless enabled is
# False, every value stored unless change_detection is truthy. The demo pair
# has no built-in default: null means the demo generator's default range.
SETTING_DEFAULTS = {"enabled": True, "change_detection": False}


def effective_settings(catalog_row, setting_row):
    """(values, user_set) for one datapoint: the effective value of every
    USER_DATAPOINT_COLUMNS column, and the columns whose value comes from
    the user's datapoint_settings row (the rest come from the catalog or the
    built-in default). A null setting means "not set"; a null catalog value
    for the demo pair means the demo generator's default range."""
    values, user_set = {}, []
    setting_row = setting_row or {}
    for column in USER_DATAPOINT_COLUMNS:
        value = setting_row.get(column)
        if value is not None:
            values[column] = value
            user_set.append(column)
        elif column in SETTING_DEFAULTS:
            values[column] = SETTING_DEFAULTS[column]
        else:
            values[column] = catalog_row.get(column)
    return values, user_set


async def _read_catalog_and_settings(store, name):
    """(live_catalog_by_id, settings_by_id, error_response, warnings). A
    failed catalog read is a link error; a failed settings read too -- the
    effective values would silently be the defaults otherwise."""
    dp_rows, dp_err = await store.read_datapoints(name)
    if dp_err is not None:
        return None, None, _link_failed(dp_err)
    catalog = {
        str(r.get("datapoint_id")): r for r in dp_rows if not r.get("deleted")
    }
    settings = {}
    if store.settings_table:
        setting_rows, set_err = await store.read_settings(name)
        if set_err is not None:
            return None, None, _link_failed(set_err)
        settings = {str(r.get("datapoint_id")): r for r in setting_rows}
    return catalog, settings, None


async def datapoint_summary(store, name):
    """(summary, warnings) for get_asset: catalog count, effective
    disabled / change_detection counts, the first ids. Degrades to a
    warning when a read fails."""
    warnings = []
    dp_rows, dp_err = await store.read_datapoints(name)
    if dp_err is not None:
        warnings.append(f"datapoint catalog unavailable: {dp_err}")
    live = [r for r in dp_rows if not r.get("deleted")]
    settings = {}
    if store.settings_table and live:
        setting_rows, set_err = await store.read_settings(name)
        if set_err is not None:
            warnings.append(f"datapoint settings unavailable: {set_err}")
        settings = {str(r.get("datapoint_id")): r for r in setting_rows}
    disabled = change_detection = 0
    for row in live:
        values, _ = effective_settings(
            row, settings.get(str(row.get("datapoint_id")))
        )
        if values["enabled"] is False:
            disabled += 1
        if bool(values["change_detection"]):
            change_detection += 1
    return {
        "count": len(live),
        "disabled": disabled,
        "change_detection": change_detection,
        "ids": sorted(
            str(r.get("datapoint_id")) for r in live
        )[:DATAPOINT_ID_PREVIEW],
    }, warnings


def _live_asset_or_rejection(cfg, name, rows):
    """The asset's latest row, or a rejection when it is missing or
    deleted (a deleted asset's catalog is being swept by the gateway)."""
    existing = next((r for r in rows if r.get("asset_name") == name), None)
    if existing is None:
        return None, _not_found(cfg, name, rows)
    if existing.get("deleted"):
        return None, _rejected(
            "not_found", [f"asset {name!r} was deleted"],
            "A deleted asset has no datapoints. create_asset re-creates it.",
        )
    return existing, None


async def list_datapoints(store, cfg, asset_name):
    name, rows, early = await _mutation_prelude(store, cfg, asset_name)
    if early is not None:
        return early
    _, early = _live_asset_or_rejection(cfg, name, rows)
    if early is not None:
        return early
    catalog, settings, early = await _read_catalog_and_settings(store, name)
    if early is not None:
        return early
    entries = []
    for datapoint_id in sorted(catalog):
        entry = strip_platform(catalog[datapoint_id])
        values, user_set = effective_settings(
            catalog[datapoint_id], settings.get(datapoint_id)
        )
        entry.update(values)
        if user_set:
            entry["user_set"] = user_set
        entries.append(entry)
    return _ok(
        "ok",
        "enabled, change_detection, demo_value and demo_variance are the "
        "EFFECTIVE values: the columns named in user_set come from the "
        "user's datapoint settings, the rest are the catalog's (or "
        "built-in) defaults. Change them with set_datapoints; null clears a "
        "setting back to its default. Everything else is derived from the "
        "asset's configuration; change that instead.",
        count=len(entries), datapoints=entries,
    )


async def set_datapoints(store, cfg, asset_name, changes=None, dry_run=False):
    items, parse_problem = _parse_changes_param(changes)
    if parse_problem:
        return _rejected(
            "invalid_value", [parse_problem],
            "Pass changes as {datapoint_id, enabled?, change_detection?, "
            "demo_value?, demo_variance?} or an array of those.",
        )
    name, rows, early = await _mutation_prelude(store, cfg, asset_name)
    if early is not None:
        return early
    _, early = _live_asset_or_rejection(cfg, name, rows)
    if early is not None:
        return early
    catalog, settings, early = await _read_catalog_and_settings(store, name)
    if early is not None:
        return early

    results, written = [], {}
    for item in items:
        result, tsp = await _set_one_datapoint(
            store, cfg, name, catalog, settings, item, dry_run
        )
        results.append(result)
        if tsp is not None:
            written[result["datapoint_id"]] = tsp

    unverified = 0
    if written:
        verdicts = await store.verify_settings(name, written)
        for result in results:
            if result["status"] != "applied":
                continue
            verdict, row, detail = verdicts.get(
                result["datapoint_id"], ("unverified", None, None)
            )
            result["verified"] = verdict == "verified"
            if verdict == "superseded":
                result["superseded"] = True
                result["current"] = {
                    column: (row or {}).get(column)
                    for column in USER_DATAPOINT_COLUMNS
                }
                result["detail"] = detail
            elif verdict != "verified":
                unverified += 1
                if detail:
                    result["detail"] = detail

    applied = no_change = rejected = failed = 0
    for result in results:
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
            f"agent changed {applied} datapoint setting(s) on asset {name!r}",
            level="info",
            user_message=(
                f"AI assistant changed {applied} datapoint setting(s) on "
                f"asset '{name}'."
            ),
        )
    ok = rejected == 0 and failed == 0
    hint = (
        "Datapoint settings apply on the next publish cycle WITHOUT "
        "restarting the asset's connection."
    )
    if unverified:
        hint += (
            f" {unverified} write(s) could not be re-read yet - confirm with "
            "list_datapoints in a few seconds."
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


def _setting_changed(column, value, catalog_row, setting_row):
    """Whether writing ``value`` to ``column`` changes anything. A null
    clears the user's setting, so it changes something only when one is
    set; a value changes something when it differs from the EFFECTIVE
    value (setting, else catalog/built-in default)."""
    current_setting = (setting_row or {}).get(column)
    if value is None:
        return current_setting is not None
    effective, _ = effective_settings(catalog_row, setting_row)
    return not normalized_equal(effective[column], value)


async def _set_one_datapoint(store, cfg, name, catalog, settings, item,
                             dry_run):
    """(result, written_tsp_or_None) for one changes entry."""
    datapoint_id = str(item.get("datapoint_id") or "").strip()
    if not datapoint_id:
        return {
            "datapoint_id": item.get("datapoint_id"),
            "status": "rejected",
            "problems": ["datapoint_id is required"],
        }, None
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
                "parsing into the gateway's catalog; change the asset's "
                "datapoint spec or the device instead"
            ],
        }, None
    catalog_row = catalog.get(datapoint_id)
    if catalog_row is None:
        return {
            "datapoint_id": datapoint_id,
            "status": "rejected",
            "problems": [f"datapoint {datapoint_id!r} not found on this asset"],
            "suggestions": _suggest(datapoint_id, sorted(catalog)),
        }, None
    requested = {}
    for column in USER_DATAPOINT_COLUMNS:
        if column in item:
            value, problem = DATAPOINT_COERCERS[column](item[column])
            if problem:
                return {
                    "datapoint_id": datapoint_id,
                    "status": "rejected",
                    "problems": [f"{column}: {problem}"],
                }, None
            requested[column] = value
    if not requested:
        return {
            "datapoint_id": datapoint_id,
            "status": "rejected",
            "problems": [
                f"nothing to change - pass one of {', '.join(USER_DATAPOINT_COLUMNS)}"
            ],
        }, None
    setting_row = settings.get(datapoint_id)
    changes = {
        column: value for column, value in requested.items()
        if _setting_changed(column, value, catalog_row, setting_row)
    }
    if not changes:
        return {"datapoint_id": datapoint_id, "status": "no_change"}, None
    if dry_run:
        return {
            "datapoint_id": datapoint_id, "status": "dry_run",
            "would_write": changes,
        }, None
    # PARTIAL settings row: identity + the changed columns (+ audit). The
    # platform's carry-over merge keeps every setting this row omits.
    payload = {
        "tsp": now_iso(),
        "gateway_id": store.device_key,
        "asset_name": name,
        "datapoint_id": datapoint_id,
    }
    payload.update(changes)
    audit = cfg.audit()
    if audit:
        column, value = audit
        payload[column] = value
    acked, append_err = await store.append_setting(
        payload, audit_column=cfg.audit_column
    )
    if not acked:
        return {
            "datapoint_id": datapoint_id, "status": "failed",
            "error": append_err,
        }, None
    # A later entry for the same datapoint in this batch compares against
    # what we just wrote.
    settings[datapoint_id] = {**(setting_row or {}), **payload}
    return {"datapoint_id": datapoint_id, "status": "applied",
            "applied": changes}, payload["tsp"]
