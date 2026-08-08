"""Agent-facing asset CRUD operations and their never-raise RPC wrappers.

Ownership split: the app owns the asset shape and validates it through ONE
injected function (``validate(config, existing) -> (valid_config, problems)``,
fail-closed); this module owns everything repetitive -- identity and
protected-column rules, hygiene of the collector_core contract columns,
echo-merge partial updates, idempotent no-change skips, soft delete with
datapoints cascade, read-back verification and the structured response
envelope.

Every RPC handler tolerates every WAMP calling convention, NEVER raises (an
exception would reach the AI agent as an opaque WAMP error, while a
structured dict tells it exactly how to recover), and returns
JSON-serializable dicts with a steering ``hint``.
"""

import asyncio
import difflib
import json
import re

from .store import (
    PLATFORM_COLUMNS,
    RUNTIME_KEYS,
    normalized_diff,
    now_iso,
    strip_platform,
)

# The canonical board asset-name rule; apps whose board form validates a
# different pattern override it via register_asset_tools(name_pattern=...).
# Rejection messages quote the active pattern, so overrides stay truthful.
ASSET_NAME_PATTERN = r"^[a-zA-Z0-9 ]{3,50}$"

# collector_core contract columns present in every collector app. The library
# owns their hygiene and create defaults; everything else in a row belongs to
# the app and passes through to its validate function untouched.
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

# Secrets never go into tables (platform contract). Column-name denylist plus
# cheap value heuristics; no entropy scoring (false-positive prone).
SECRET_NAME_RE = re.compile(
    r"(?i)(password|passwd|pwd|secret|token|api[_-]?key|credential"
    r"|private[_-]?key|passphrase|certificate)"
)
SECRET_VALUE_RES = (
    (re.compile(r"-----BEGIN "), "a PEM certificate/key block"),
    (re.compile(r"://[^/\s@]+:[^/\s@]+@"), "a URL with embedded credentials"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ"), "a JWT token"),
)
SECRET_HINT = (
    "Secrets never go into config tables (boards can read them). Use the "
    "platform secret store / device environment instead."
)

MAX_SPEC_CHARS = 65536
MAX_ROW_CHARS = 131072
MAX_SPEC_RETURN_CHARS = 32768
DATAPOINT_BATCH_CAP = 100
DATAPOINT_ID_PREVIEW = 50

TRUE_STRINGS = ("true", "yes", "1")
FALSE_STRINGS = ("false", "no", "0")

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


class ToolConfig:
    """Internal holder for the per-registration settings (built by
    register_asset_tools, never by the app)."""

    def __init__(self, validate, name_pattern=ASSET_NAME_PATTERN,
                 audit_column=None, max_assets=200):
        self.validate = validate
        self.name_pattern = re.compile(name_pattern)
        self.audit_column = audit_column
        self.max_assets = max_assets
        # One lock per registration: serializes read->merge->append->verify
        # so two concurrent agent calls cannot lose each other's writes.
        self.lock = asyncio.Lock()

    def audit(self):
        return (self.audit_column, "agent") if self.audit_column else None


# --------------------------------------------------------------------------
# WAMP argument coercion
# --------------------------------------------------------------------------

def coerce_rpc_args(args, kwargs, positional=()):
    """Normalize WAMP call args into one dict: keyword args, a single
    positional dict, or bare positional values mapped onto ``positional`` --
    the platform agent runtime's calling convention is not pinned down, so
    tolerate all of them.

    ``device_key`` is dropped: the agent must send it to address the call at a
    gateway, the platform strips it before dispatch, and this process already
    knows its own key. Tolerating a leaked one keeps every tool from failing
    as "unexpected parameters"."""
    if kwargs:
        params = dict(kwargs)
    elif len(args) == 1 and isinstance(args[0], dict):
        params = dict(args[0])
    else:
        params = {name: value for name, value in zip(positional, args)}
    params.pop("device_key", None)
    return params


def _parse_object_param(value, param_name):
    """(value, problem) -- accept a dict, a JSON-string dict (agents
    sometimes stringify nested objects), or None."""
    if value is None or isinstance(value, dict):
        return value, None
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return None, f"{param_name} must be an object (got unparsable string)"
        if isinstance(parsed, dict):
            return parsed, None
        return None, f"{param_name} must be an object, got {type(parsed).__name__}"
    return None, f"{param_name} must be an object, got {type(value).__name__}"


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


def _parse_bool_param(value, param_name, default=False):
    """(bool, problem) -- strict boolean request parameters."""
    if value is None:
        return default, None
    coerced, problem = _coerce_bool(value)
    if problem:
        return default, f"{param_name} must be a boolean"
    return coerced, None


# --------------------------------------------------------------------------
# Response builders
# --------------------------------------------------------------------------

def _finish(response, warnings=None, ignored=None):
    if warnings:
        response["warnings"] = list(warnings)
    if ignored:
        response["ignored"] = list(ignored)
    return response


def _ok(status, hint, **extra):
    response = {"ok": True, "status": status, "hint": hint}
    response.update(extra)
    return response


def _rejected(code, problems, hint, **extra):
    response = {
        "ok": False,
        "status": "rejected",
        "code": code,
        "problems": list(problems),
        "hint": hint,
    }
    response.update(extra)
    return response


def _failed(code, error, hint, **extra):
    response = {
        "ok": False,
        "status": "failed",
        "code": code,
        "error": error,
        "hint": hint,
    }
    response.update(extra)
    return response


def _not_found(asset_name, rows, extra_hint=""):
    names = sorted(
        {r.get("asset_name") for r in rows if not r.get("deleted")} - {None}
    )
    suggestions = _suggest(asset_name, names)
    hint = (
        f"No asset named {asset_name!r} on this gateway."
        + (f" Closest matches: {', '.join(suggestions)}." if suggestions else "")
        + " Call list_assets to see what exists."
        + (f" {extra_hint}" if extra_hint else "")
    )
    return _rejected(
        "not_found", [f"asset {asset_name!r} not found"], hint,
        suggestions=suggestions, available=names,
    )


def _link_failed(error):
    return _failed(
        "link_error", error,
        "The gateway could not reach the platform data service - nothing was "
        "changed. Retry shortly.",
    )


def _suggest(name, candidates):
    matches = difflib.get_close_matches(name, candidates, n=3, cutoff=0.4)
    lowered = name.lower()
    for candidate in candidates:
        if candidate in matches:
            continue
        if lowered in candidate.lower() or candidate.lower() in lowered:
            matches.append(candidate)
    return matches[:5]


# --------------------------------------------------------------------------
# Pipeline pieces
# --------------------------------------------------------------------------

def _check_name(asset_name, cfg):
    """(stripped_name, problems)"""
    if not isinstance(asset_name, str) or not asset_name.strip():
        return "", ["asset_name is required"]
    stripped = asset_name.strip()
    if not cfg.name_pattern.fullmatch(stripped):
        return stripped, [
            f"asset_name {stripped!r} is invalid: it must match "
            f"{cfg.name_pattern.pattern}"
        ]
    return stripped, []


def _partition_fields(fields, asset_name, device_key):
    """P1: strip protected columns out of the agent's fields.

    Returns (clean_fields, problems, ignored). Platform/runtime keys are
    dropped silently (agents echo whole rows from get) and listed in
    ``ignored``; identity/managed columns are rejected unless they merely
    echo the current values."""
    clean, problems, ignored = {}, [], []
    for key, value in fields.items():
        if key in PLATFORM_COLUMNS or key in RUNTIME_KEYS:
            ignored.append(key)
            continue
        if key == "asset_name":
            if isinstance(value, str) and value.strip() == asset_name:
                ignored.append(key)
            else:
                problems.append(
                    "asset_name is an immutable identity - to rename, create "
                    "the new asset and delete the old one (history stays "
                    "under the old name)"
                )
            continue
        if key == "gateway_id":
            if _same_gateway(value, device_key):
                ignored.append(key)
            else:
                problems.append(
                    f"this tool configures gateway {device_key} only - to "
                    "move an asset, create it on the target gateway's tools "
                    "and delete it here"
                )
            continue
        if key == "deleted":
            problems.append(
                "deleted cannot be set through create/update - use the "
                "delete_asset tool"
            )
            continue
        clean[key] = value
    return clean, problems, ignored


def _same_gateway(value, device_key):
    try:
        return int(value) == device_key
    except (TypeError, ValueError):
        return False


def _coerce_bool(value):
    """(bool_or_None, problem_or_None) -- strict: only real booleans and the
    unambiguous spellings coerce. The collector's pause switch is null-safe
    (only ``is False`` pauses), so a truthy STRING like "false" reaching the
    table would keep an asset running."""
    if isinstance(value, bool):
        return value, None
    if isinstance(value, int) and value in (0, 1):
        return bool(value), None
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in TRUE_STRINGS:
            return True, None
        if lowered in FALSE_STRINGS:
            return False, None
    return None, f"{value!r} is not a boolean"


def _coerce_number(value):
    """(number_or_None, problem_or_None). An explicit null clears the setting.

    Booleans are rejected rather than read as 0/1: the caller that means a
    state says so on ``demo_value``, and silently turning True into 1.0 there
    would swap a resting state for a constant reading."""
    if value is None:
        return None, None
    if isinstance(value, bool):
        return None, f"{value!r} is not a number"
    if isinstance(value, (int, float)):
        return float(value), None
    if isinstance(value, str):
        try:
            return float(value.strip()), None
        except ValueError:
            pass
    return None, f"{value!r} is not a number"


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
    """P2: hygiene of the collector_core contract columns. Returns
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


def _secret_scan(fields):
    """P3: reject secret-looking column names and values in the agent's
    input (echoed existing columns are not re-scanned)."""
    problems = []
    for key, value in fields.items():
        if SECRET_NAME_RE.search(key):
            problems.append(f"column {key!r} looks like a secret. {SECRET_HINT}")
            continue
        if isinstance(value, str):
            for pattern, label in SECRET_VALUE_RES:
                if pattern.search(value):
                    problems.append(
                        f"the value of {key!r} contains {label}. {SECRET_HINT}"
                    )
                    break
    return problems


async def _run_validate(cfg, candidate, existing):
    """P4: the app's validate function. Returns (config, problems).
    Fail-closed: a raise rejects (its message is the problem), a malformed
    return rejects -- no valid config means no write."""
    try:
        result = cfg.validate(dict(candidate), existing)
        if asyncio.iscoroutine(result):
            result = await result
    except Exception as e:
        return None, [f"validation failed: {e}"]
    if (
        not isinstance(result, tuple)
        or len(result) != 2
        or not isinstance(result[1], (list, tuple))
    ):
        return None, [
            "the app's validate function must return (config, problems)"
        ]
    config, problems = result
    if problems:
        return None, [str(p) for p in problems]
    if not isinstance(config, dict):
        return None, [
            "the app's validate function returned no config and no problems"
        ]
    return dict(config), []


def _restamp(config, asset_name, device_key, audit, deleted=False):
    """P5: force the protected columns onto the validate-returned config --
    the app function must not be able to smuggle identity changes or poison
    values -- and stamp a fresh tsp."""
    row = {
        k: v
        for k, v in config.items()
        if k not in PLATFORM_COLUMNS and k not in RUNTIME_KEYS
    }
    row["asset_name"] = asset_name
    row["gateway_id"] = device_key
    row["deleted"] = deleted
    if audit:
        column, value = audit
        row[column] = value
    row["tsp"] = now_iso()
    return row


def _row_size_problem(row):
    try:
        encoded = json.dumps(row, default=str)
    except (TypeError, ValueError):
        return "the configuration is not JSON-serializable"
    if len(encoded) > MAX_ROW_CHARS:
        return (
            f"the configuration is too large ({len(encoded)} chars, max "
            f"{MAX_ROW_CHARS})"
        )
    return None


def _spec_for_response(row):
    """Cap datapoint_spec in response payloads; writes are never truncated."""
    spec = row.get("datapoint_spec")
    if isinstance(spec, str) and len(spec) > MAX_SPEC_RETURN_CHARS:
        row = dict(row)
        row["datapoint_spec"] = (
            spec[:MAX_SPEC_RETURN_CHARS]
            + f"... [truncated, {len(spec)} chars total]"
        )
    return row


def _split_rows(rows):
    live = [r for r in rows if not r.get("deleted")]
    deleted = [r for r in rows if r.get("deleted")]
    return live, deleted


def _status_map(status_rows):
    statuses = {}
    for row in status_rows:
        if row.get("deleted"):
            continue
        statuses[row.get("asset_name")] = {
            "status": row.get("status"),
            "detail": row.get("detail"),
            "since": row.get("tsp"),
        }
    return statuses


async def _mutation_prelude(store, cfg, asset_name):
    """Shared start of every mutation: name check + the one assets read.
    Returns (name, rows, error_response_or_None)."""
    name, problems = _check_name(asset_name, cfg)
    if problems:
        return name, [], _rejected(
            "invalid_name", problems,
            f"Asset names must match {cfg.name_pattern.pattern}.",
        )
    rows, err = await store.read_assets()
    if err is not None:
        return name, [], _link_failed(err)
    return name, rows, None


# --------------------------------------------------------------------------
# Operations
# --------------------------------------------------------------------------

async def list_assets(store, cfg, include_deleted=False):
    rows, err = await store.read_assets()
    if err is not None:
        return _link_failed(err)
    live, deleted = _split_rows(rows)
    warnings = []
    status_rows, status_err = await store.read_statuses()
    statuses = _status_map(status_rows)
    if status_err is not None:
        warnings.append(f"live status unavailable: {status_err}")

    def entry(row):
        stripped = strip_platform(row)
        spec = stripped.pop("datapoint_spec", None)
        if isinstance(spec, str) and spec:
            stripped["datapoint_spec"] = {
                "chars": len(spec),
                "preview": spec[:200],
            }
        else:
            stripped["datapoint_spec"] = {"chars": 0, "preview": ""}
        stripped["live_status"] = statuses.get(
            row.get("asset_name"), {"status": "unknown"}
        )
        return stripped

    assets = sorted(
        (entry(r) for r in live), key=lambda e: e.get("asset_name") or ""
    )
    if include_deleted:
        assets += sorted(
            (entry(r) for r in deleted), key=lambda e: e.get("asset_name") or ""
        )
    if assets:
        hint = (
            "Fetch one asset's full configuration, live status and datapoint "
            "summary with get_asset. status online = data is flowing; "
            "offline shows the last connection error in detail."
        )
    else:
        hint = (
            "No assets configured on this gateway yet - create one with "
            "create_asset (dry_run first)."
        )
    return _finish(
        _ok("ok", hint, count=len(assets), assets=assets), warnings=warnings
    )


async def get_asset(store, cfg, asset_name):
    name, problems = _check_name(asset_name, cfg)
    if problems:
        return _rejected(
            "invalid_name", problems,
            f"Asset names must match {cfg.name_pattern.pattern}.",
        )
    rows, err = await store.read_assets()
    if err is not None:
        return _link_failed(err)
    row = next((r for r in rows if r.get("asset_name") == name), None)
    if row is None:
        return _not_found(name, rows)
    if row.get("deleted"):
        return _ok(
            "ok",
            f"Asset {name!r} was deleted. create_asset re-creates it under "
            "the same name (measurement history under this name resumes).",
            found=True, deleted=True, asset=_spec_for_response(strip_platform(row)),
        )

    warnings = []
    status_rows, status_err = await store.read_statuses()
    if status_err is not None:
        warnings.append(f"live status unavailable: {status_err}")
    live_status = _status_map(status_rows).get(name)

    dp_rows, dp_err = await store.read_datapoints(name)
    if dp_err is not None:
        warnings.append(f"datapoint catalog unavailable: {dp_err}")
    live_dps = [r for r in dp_rows if not r.get("deleted")]
    datapoints = {
        "count": len(live_dps),
        "disabled": sum(1 for r in live_dps if r.get("enabled") is False),
        "change_detection": sum(
            1 for r in live_dps if bool(r.get("change_detection"))
        ),
        "ids": sorted(
            str(r.get("datapoint_id")) for r in live_dps
        )[:DATAPOINT_ID_PREVIEW],
    }

    meta = {"tsp": row.get("tsp"), "last_changed_by": row.get("authid")}
    if "auto_registered" in row:
        meta["auto_registered"] = row.get("auto_registered")
    if cfg.audit_column and cfg.audit_column in row:
        meta[cfg.audit_column] = row.get(cfg.audit_column)

    hint = (
        "To modify, call update_asset with ONLY the fields to change and "
        f"expected_tsp={row.get('tsp')!r} to guard against concurrent edits."
    )
    status_value = (live_status or {}).get("status")
    if status_value == "offline":
        hint += (
            " The asset is offline: query the error-logs table for its "
            "recent errors (see your diagnostics instructions) and propose "
            "a remedy."
        )
    elif status_value == "paused":
        hint += " The asset is paused - update_asset with enabled=true resumes it."
    return _finish(
        _ok(
            "ok", hint,
            found=True,
            asset=_spec_for_response(strip_platform(row)),
            live_status=live_status,
            datapoints=datapoints,
            _meta=meta,
        ),
        warnings=warnings,
    )


async def create_asset(store, cfg, asset_name, fields=None, dry_run=False):
    fields, parse_problem = _parse_object_param(fields, "fields")
    if parse_problem:
        return _rejected("invalid_value", [parse_problem],
                         "Pass the connection fields as an object.")
    fields = fields or {}
    name, rows, early = await _mutation_prelude(store, cfg, asset_name)
    if early is not None:
        return early

    live, _ = _split_rows(rows)
    existing = next((r for r in rows if r.get("asset_name") == name), None)
    warnings, ignored = [], []
    if existing is not None and not existing.get("deleted"):
        return _rejected(
            "already_exists",
            [f"asset {name!r} already exists on this gateway"],
            "Asset names are unique per gateway - use update_asset to change "
            "it, or choose a different name.",
            asset=_spec_for_response(strip_platform(existing)),
        )
    if existing is not None and existing.get("deleted"):
        warnings.append(
            f"{name!r} previously existed and was deleted - re-creating "
            "revives the name and measurement history under it resumes"
        )
    for row in live:
        other = row.get("asset_name") or ""
        if other != name and other.casefold() == name.casefold():
            warnings.append(f"a similarly named asset {other!r} already exists")
    if len(live) >= cfg.max_assets:
        return _rejected(
            "limit_exceeded",
            [f"this gateway already has {len(live)} assets (limit "
             f"{cfg.max_assets})"],
            "Delete unused assets first, or raise the limit in the app.",
        )

    clean, problems, ignored = _partition_fields(fields, name, store.device_key)
    if problems:
        return _finish(
            _rejected("protected_column", problems,
                      "Remove the protected fields and retry."),
            ignored=ignored,
        )
    candidate = dict(CORE_DEFAULTS)
    candidate.update(clean)
    candidate, problems = _coerce_core(candidate)
    if problems:
        return _finish(
            _rejected("invalid_value", problems, "Fix the listed values."),
            ignored=ignored,
        )
    problems = _secret_scan(clean)
    if problems:
        return _finish(
            _rejected("secret_rejected", problems, SECRET_HINT),
            ignored=ignored,
        )
    candidate["asset_name"] = name
    candidate["gateway_id"] = store.device_key
    config, problems = await _run_validate(cfg, candidate, None)
    if problems:
        return _finish(
            _rejected("validation_error", problems,
                      "Fix every listed problem and retry (dry_run first)."),
            ignored=ignored,
        )
    row = _restamp(config, name, store.device_key, cfg.audit())
    coerced_row, problems = _coerce_core(row)
    if problems:
        return _rejected(
            "validation_error",
            [f"the app's validate function returned an invalid value - {p}"
             for p in problems],
            "This is an app-side validator bug; report it.",
        )
    row = coerced_row
    size_problem = _row_size_problem(row)
    if size_problem:
        return _rejected("limit_exceeded", [size_problem],
                         "Reduce the configuration size.")

    if dry_run:
        preview = dict(row)
        preview["tsp"] = "<stamped at write>"
        return _finish(
            _ok(
                "dry_run",
                "Valid - nothing was written. Show this to the user, then "
                "re-call with dry_run=false to apply.",
                changed=True, would_write=preview,
            ),
            warnings=warnings, ignored=ignored,
        )

    acked, append_err = await store.append_asset(row)
    if not acked:
        await store.notify(
            f"create asset {name!r} failed: {append_err}",
            level="warn",
            user_message=(
                f"The AI assistant could not create asset '{name}' - "
                "platform connection problem."
            ),
        )
        return _failed(
            "write_failed", append_err,
            "The write was not acknowledged - it may rarely have landed "
            "anyway; call get_asset before retrying.",
        )
    verdict, latest, detail = await store.verify_asset(name, row["tsp"])
    await store.notify(
        f"agent created asset {name!r}",
        level="info",
        user_message=f"AI assistant created asset '{name}'.",
    )
    return _finish(
        _build_write_response(
            "applied", name, row, verdict, latest, detail,
            created=True,
            success_hint=(
                "Created. The collector starts this asset now - call "
                "get_asset in ~15 seconds: status online means data is "
                "flowing, offline shows the connection error. Consider "
                "demo_mode=true to validate dashboards before touching "
                "real hardware."
            ),
        ),
        warnings=warnings, ignored=ignored,
    )


async def update_asset(store, cfg, asset_name, changes=None,
                       expected_tsp=None, dry_run=False):
    changes, parse_problem = _parse_object_param(changes, "changes")
    if parse_problem:
        return _rejected("invalid_value", [parse_problem],
                         "Pass only the fields to change as an object.")
    if not changes:
        return _rejected(
            "invalid_value", ["changes must contain at least one field"],
            "Pass ONLY the fields to change, e.g. {\"collect_interval\": 30}.",
        )
    name, rows, early = await _mutation_prelude(store, cfg, asset_name)
    if early is not None:
        return early
    existing = next((r for r in rows if r.get("asset_name") == name), None)
    if existing is None:
        return _not_found(name, rows)
    if existing.get("deleted"):
        return _rejected(
            "not_found",
            [f"asset {name!r} was deleted"],
            "Use create_asset to re-create it - 'previous' holds its last "
            "configuration.",
            previous=strip_platform(existing),
        )
    if expected_tsp is not None and str(expected_tsp) != str(existing.get("tsp")):
        return {
            "ok": False,
            "status": "conflict",
            "code": "conflict",
            "problems": [
                f"the configuration changed since you read it (your "
                f"expected_tsp {expected_tsp!r}, current {existing.get('tsp')!r})"
            ],
            "current": _spec_for_response(strip_platform(existing)),
            "current_tsp": existing.get("tsp"),
            "last_changed_by": existing.get("authid"),
            "hint": (
                "Re-read with get_asset and re-apply your change on the "
                "fresh values if still wanted."
            ),
        }

    warnings = []
    if existing.get("auto_registered"):
        warnings.append(
            "this asset is maintained by network discovery - the scanner "
            "may re-write identity fields"
        )
    clean, problems, ignored = _partition_fields(changes, name, store.device_key)
    if problems:
        return _finish(
            _rejected("protected_column", problems,
                      "Remove the protected fields and retry."),
            ignored=ignored,
        )
    if not clean:
        return _finish(
            _ok(
                "no_change",
                "Every field you sent was a protected echo - nothing to "
                "change.",
                changed=False,
            ),
            warnings=warnings, ignored=ignored,
        )
    clean, problems = _coerce_core(clean)
    if problems:
        return _finish(
            _rejected("invalid_value", problems, "Fix the listed values."),
            ignored=ignored,
        )
    problems = _secret_scan(clean)
    if problems:
        return _finish(
            _rejected("secret_rejected", problems, SECRET_HINT),
            ignored=ignored,
        )

    base = strip_platform(existing)
    candidate = dict(base)
    candidate.update(clean)  # explicit null blanks; omitted keys stay
    config, problems = await _run_validate(cfg, candidate, dict(existing))
    if problems:
        return _finish(
            _rejected("validation_error", problems,
                      "Fix every listed problem and retry (dry_run first)."),
            ignored=ignored,
        )
    row = _restamp(config, name, store.device_key, cfg.audit())
    coerced_row, problems = _coerce_core(row)
    if problems:
        return _rejected(
            "validation_error",
            [f"the app's validate function returned an invalid value - {p}"
             for p in problems],
            "This is an app-side validator bug; report it.",
        )
    row = coerced_row
    size_problem = _row_size_problem(row)
    if size_problem:
        return _rejected("limit_exceeded", [size_problem],
                         "Reduce the configuration size.")

    changed_fields = normalized_diff(row, existing)
    if not changed_fields:
        return _finish(
            _ok(
                "no_change",
                "The asset is already configured this way - nothing was "
                "written (a write would restart its collection task).",
                changed=False,
            ),
            warnings=warnings, ignored=ignored,
        )

    if dry_run:
        preview = dict(row)
        preview["tsp"] = "<stamped at write>"
        return _finish(
            _ok(
                "dry_run",
                "Valid - nothing was written. Show the user the change, "
                "then re-call with dry_run=false to apply.",
                changed=True, changed_fields=changed_fields,
                would_write=preview,
            ),
            warnings=warnings, ignored=ignored,
        )

    acked, append_err = await store.append_asset(row)
    if not acked:
        await store.notify(
            f"update asset {name!r} failed: {append_err}",
            level="warn",
            user_message=(
                f"The AI assistant could not update asset '{name}' - "
                "platform connection problem."
            ),
        )
        return _failed(
            "write_failed", append_err,
            "The write was not acknowledged - it may rarely have landed "
            "anyway; call get_asset before retrying.",
        )
    verdict, latest, detail = await store.verify_asset(name, row["tsp"])
    summary = ", ".join(
        f"{col} {change['from']!r} -> {change['to']!r}"
        for col, change in sorted(changed_fields.items())
    )
    await store.notify(
        f"agent updated asset {name!r}: {summary}",
        level="info",
        user_message=f"AI assistant updated asset '{name}': {summary}"[:500],
    )
    return _finish(
        _build_write_response(
            "applied", name, row, verdict, latest, detail,
            updated=True, changed_fields=changed_fields,
            previous=strip_platform(existing),
            success_hint=(
                "Updated and the collection task restarts with the new "
                "configuration - a brief offline/online flicker is normal. "
                "Verify with get_asset in ~15 seconds."
            ),
        ),
        warnings=warnings, ignored=ignored,
    )


def _build_write_response(status, asset_name, row, verdict, latest, detail,
                          success_hint, **extra):
    applied = _spec_for_response(dict(row))
    if verdict == "verified":
        return _ok(status, success_hint, asset_name=asset_name,
                   changed=True, verified=True, applied=applied, **extra)
    if verdict == "superseded":
        return _ok(
            status,
            f"The write landed, but {detail} - re-read with get_asset and "
            "re-apply on top if your change is still wanted.",
            asset_name=asset_name, changed=True, verified=False,
            superseded=True,
            current=_spec_for_response(strip_platform(latest or {})),
            applied=applied, **extra,
        )
    return _ok(
        "unverified",
        "The write was acknowledged but could not be re-read "
        + (f"({detail}) " if detail else "")
        + "- call get_asset in a few seconds to confirm it applied.",
        asset_name=asset_name, changed=True, verified=False,
        applied=applied, **extra,
    )


async def delete_asset(store, cfg, asset_name, expected_tsp=None,
                       dry_run=False):
    name, rows, early = await _mutation_prelude(store, cfg, asset_name)
    if early is not None:
        return early
    existing = next((r for r in rows if r.get("asset_name") == name), None)
    if existing is None:
        return _not_found(
            name, rows, extra_hint="Nothing was deleted."
        )

    dp_rows, dp_err = await store.read_datapoints(name)
    live_dps = [r for r in dp_rows if not r.get("deleted")]
    warnings = []
    if dp_err is not None:
        warnings.append(f"datapoint catalog unavailable: {dp_err}")

    if existing.get("deleted"):
        # Convergent: finish whatever a partial earlier delete left behind.
        if not live_dps:
            return _ok(
                "no_change",
                f"Asset {name!r} is already deleted.",
                changed=False,
            )
        if dry_run:
            return _ok(
                "dry_run",
                f"Asset {name!r} is already deleted, but "
                f"{len(live_dps)} orphaned datapoint entries remain - "
                "re-calling without dry_run cleans them up.",
                changed=True, datapoints_to_delete=len(live_dps),
            )
        deleted_count, failures = await store.cascade_delete_datapoints(
            name, audit=cfg.audit()
        )
        return _finish(
            _ok(
                "deleted",
                "Cleaned up the orphaned datapoint entries of the already-"
                "deleted asset."
                + (" Some entries failed - call delete_asset again to "
                   "finish." if failures else ""),
                changed=True, datapoints_deleted=deleted_count,
                datapoints_failed=len(failures),
            ),
            warnings=warnings + failures,
        )

    if expected_tsp is not None and str(expected_tsp) != str(existing.get("tsp")):
        return {
            "ok": False,
            "status": "conflict",
            "code": "conflict",
            "problems": [
                "the configuration changed since you read it - re-check "
                "before deleting"
            ],
            "current": _spec_for_response(strip_platform(existing)),
            "current_tsp": existing.get("tsp"),
            "last_changed_by": existing.get("authid"),
            "hint": "Re-read with get_asset, confirm with the user, then delete.",
        }

    if dry_run:
        return _finish(
            _ok(
                "dry_run",
                f"Would soft-delete asset {name!r} and its "
                f"{len(live_dps)} datapoint entries. Measurement history "
                "is retained; the name becomes reusable. Confirm with the "
                "user, then re-call with dry_run=false.",
                changed=True, datapoints_to_delete=len(live_dps),
                previous=_spec_for_response(strip_platform(existing)),
            ),
            warnings=warnings,
        )

    tombstone = strip_platform(existing)
    tombstone["deleted"] = True
    audit = cfg.audit()
    if audit:
        column, value = audit
        tombstone[column] = value
    tombstone["tsp"] = now_iso()
    acked, append_err = await store.append_asset(tombstone)
    if not acked:
        await store.notify(
            f"delete asset {name!r} failed: {append_err}",
            level="warn",
            user_message=(
                f"The AI assistant could not delete asset '{name}' - "
                "platform connection problem."
            ),
        )
        return _failed(
            "write_failed", append_err,
            "The delete was not acknowledged - call get_asset to check, "
            "then retry.",
        )

    deleted_count, failures = await store.cascade_delete_datapoints(
        name, audit=cfg.audit()
    )
    verdict, _, detail = await store.verify_asset(name, tombstone["tsp"])
    toast = f"AI assistant deleted asset '{name}'"
    if deleted_count:
        toast += f" ({deleted_count} datapoint entries removed)"
    await store.notify(
        f"agent deleted asset {name!r} "
        f"(datapoints removed: {deleted_count}, failed: {len(failures)})",
        level="warn" if failures else "info",
        user_message=toast + ".",
    )
    hint = (
        "Soft-deleted: the collector stops the asset's collection task and "
        "measurement history is retained. To undo, call create_asset with "
        "the values in 'previous'."
    )
    if failures:
        hint += (
            f" {len(failures)} datapoint entries could not be cleaned up - "
            "call delete_asset again to finish."
        )
    if verdict != "verified":
        hint += (
            " The delete could not be re-read"
            + (f" ({detail})" if detail else "")
            + " - confirm with get_asset."
        )
    return _finish(
        _ok(
            "deleted", hint,
            asset_name=name, changed=True,
            verified=verdict == "verified",
            datapoints_deleted=deleted_count,
            datapoints_failed=len(failures),
            previous=_spec_for_response(strip_platform(existing)),
        ),
        warnings=warnings + failures,
    )


async def list_datapoints(store, cfg, asset_name):
    name, rows, early = await _mutation_prelude(store, cfg, asset_name)
    if early is not None:
        return early
    existing = next((r for r in rows if r.get("asset_name") == name), None)
    if existing is None:
        return _not_found(name, rows)
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
        return _not_found(name, rows)
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
    payload = strip_platform(row)
    payload.update(changes)
    if not normalized_diff(payload, row):
        return {"datapoint_id": datapoint_id, "status": "no_change"}
    if dry_run:
        return {
            "datapoint_id": datapoint_id, "status": "dry_run",
            "would_write": changes,
        }
    payload["deleted"] = bool(row.get("deleted", False))
    payload["tsp"] = now_iso()
    acked, append_err = await store.append_datapoint(payload)
    if not acked:
        return {
            "datapoint_id": datapoint_id, "status": "failed",
            "error": append_err,
        }
    return {"datapoint_id": datapoint_id, "status": "applied",
            "applied": changes}


# --------------------------------------------------------------------------
# RPC wrapping
# --------------------------------------------------------------------------

def rpc_handlers(store, cfg):
    """The WAMP endpoints, keyed by operation name. Every handler tolerates
    every calling convention, serializes mutations behind the registration's
    lock, and NEVER raises -- any unexpected error becomes an internal_error
    response plus an error-level operator toast."""

    def wrap(op, positional, mutating):
        async def handler(*args, **kwargs):
            try:
                params = coerce_rpc_args(args, kwargs, positional)
                if mutating:
                    async with cfg.lock:
                        return await op(**params)
                return await op(**params)
            except TypeError as e:
                return _rejected(
                    "invalid_value",
                    [f"unexpected or missing parameters: {e}"],
                    "Check the tool's parameter schema and retry.",
                )
            except Exception as e:  # never surface a raw WAMP error
                await store.notify(
                    f"config tool internal error: {type(e).__name__}: {e}",
                    level="error",
                    user_message=(
                        "The AI assistant's configuration tool hit an "
                        "internal error."
                    ),
                )
                return _failed(
                    "internal_error", f"{type(e).__name__}: {e}",
                    "This is a tool bug, not your input - you may retry "
                    "once; report it if it persists.",
                )
        return handler

    async def _list(include_deleted=False):
        value, problem = _parse_bool_param(include_deleted, "include_deleted")
        if problem:
            return _rejected("invalid_value", [problem], "Pass a boolean.")
        return await list_assets(store, cfg, include_deleted=value)

    async def _get(asset_name=None):
        return await get_asset(store, cfg, asset_name)

    async def _create(asset_name=None, fields=None, dry_run=False):
        value, problem = _parse_bool_param(dry_run, "dry_run")
        if problem:
            return _rejected("invalid_value", [problem], "Pass a boolean.")
        return await create_asset(store, cfg, asset_name, fields=fields,
                                  dry_run=value)

    async def _update(asset_name=None, changes=None, expected_tsp=None,
                      dry_run=False):
        value, problem = _parse_bool_param(dry_run, "dry_run")
        if problem:
            return _rejected("invalid_value", [problem], "Pass a boolean.")
        return await update_asset(store, cfg, asset_name, changes=changes,
                                  expected_tsp=expected_tsp, dry_run=value)

    async def _delete(asset_name=None, expected_tsp=None, dry_run=False):
        value, problem = _parse_bool_param(dry_run, "dry_run")
        if problem:
            return _rejected("invalid_value", [problem], "Pass a boolean.")
        return await delete_asset(store, cfg, asset_name,
                                  expected_tsp=expected_tsp, dry_run=value)

    async def _list_datapoints(asset_name=None):
        return await list_datapoints(store, cfg, asset_name)

    async def _set_datapoints(asset_name=None, changes=None, dry_run=False):
        value, problem = _parse_bool_param(dry_run, "dry_run")
        if problem:
            return _rejected("invalid_value", [problem], "Pass a boolean.")
        return await set_datapoints(store, cfg, asset_name, changes=changes,
                                    dry_run=value)

    return {
        "list": wrap(_list, ("include_deleted",), mutating=False),
        "get": wrap(_get, ("asset_name",), mutating=False),
        "create": wrap(_create, ("asset_name", "fields", "dry_run"),
                       mutating=True),
        "update": wrap(_update, ("asset_name", "changes", "expected_tsp",
                                 "dry_run"), mutating=True),
        "delete": wrap(_delete, ("asset_name", "expected_tsp", "dry_run"),
                       mutating=True),
        "list_datapoints": wrap(_list_datapoints, ("asset_name",),
                                mutating=False),
        "set_datapoints": wrap(_set_datapoints, ("asset_name", "changes",
                                                 "dry_run"), mutating=True),
    }
