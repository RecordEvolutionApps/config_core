"""Agent-facing config CRUD operations and their never-raise RPC wrappers.

Ownership split: the app owns the row shape and validates it through ONE
injected function (``validate(config, existing) -> (valid_config, problems)``,
fail-closed); this module owns everything repetitive -- identity and
protected-column rules, echo-merge partial updates, idempotent no-change
skips, soft delete, read-back verification and the structured response
envelope. What KIND of row is being managed comes in as an ``_Entity``
(defaults, column hygiene, prose); the collector family's entity and its
datapoint tools live in ``collector.py``, whose historical constants this
module still re-exports via a lazy ``__getattr__`` at the bottom.

Every RPC handler tolerates every WAMP calling convention, NEVER raises (an
exception would reach the AI agent as an opaque WAMP error, while a
structured dict tells it exactly how to recover), and returns
JSON-serializable dicts with a steering ``hint``.
"""

import asyncio
import difflib
import json
import re
from dataclasses import dataclass

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

# Secrets never go into tables (platform contract). Column-name denylist plus
# cheap value heuristics; no entropy scoring (false-positive prone).
SECRET_NAME_RE = re.compile(
    r"(?i)(password|passwd|pwd|secret|token|api[_-]?key|credential"
    r"|private[_-]?key|passphrase|certificate)"
)
# The PEM rule is deliberately block-type-blind: a public certificate is
# refused exactly like a private key, even though SECRET_NAME_RE lets column
# names like tls_ca and tls_client_cert through. Decided 2026-08-28 for
# datarelay: the USER pastes certificates into the app's own form, the agent
# never writes them. Splitting CERTIFICATE off from PRIVATE KEY here is a
# product change, not a bug fix -- datarelay's assistant prompt and a drift
# test in its suite pin this behaviour and have to move with it.
SECRET_VALUE_RES = (
    (re.compile(r"-----BEGIN "), "a PEM certificate/key block"),
    (re.compile(r"://[^/\s@]+:[^/\s@]+@"), "a URL with embedded credentials"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ"), "a JWT token"),
)
SECRET_HINT = (
    "Secrets never go into config tables (boards can read them). Use the "
    "platform secret store / device environment instead."
)

MAX_ROW_CHARS = 131072
MAX_SPEC_RETURN_CHARS = 32768
DATAPOINT_ID_PREVIEW = 50

TRUE_STRINGS = ("true", "yes", "1")
FALSE_STRINGS = ("false", "no", "0")


@dataclass(frozen=True)
class _Hints:
    """The per-entity hint texts -- the only strings that differ between the
    collector preset and a generic entity beyond the noun. Hint strings are
    explicitly NOT part of the semver contract. Templates take ``{name}``
    (pre-repr'd) and, for delete_dry_run, ``{dp_clause}``."""

    list_nonempty: str
    list_empty: str
    get_deleted: str
    get_offline: str        # suffix appended to the get hint; "" = never
    get_paused: str         # suffix appended to the get hint; "" = never
    create_revive: str      # warning when re-creating a deleted name
    create_success: str
    update_no_change: str
    update_success: str
    delete_dry_run: str
    delete_success: str


@dataclass(frozen=True)
class _Entity:
    """What kind of row the tools manage (internal; built by the register
    functions, never by the app). ``noun`` shapes prose only -- the wire
    vocabulary (``asset_name``, ``get_asset``, response fields) is identical
    for every entity, so deployed agent prompts and tool schemas keep
    working. ``create_defaults`` is seeded UNDER the agent's fields on
    create. ``coerce`` is ``(fields) -> (fields, problems)`` column hygiene
    run on the agent's input and on validate's returned row; None = validate
    owns all typing."""

    noun: str
    create_defaults: dict
    coerce: object          # callable or None
    hints: _Hints


def _generic_hints(noun):
    """Neutral hint texts for an entity without collector semantics."""
    return _Hints(
        list_nonempty=(
            f"Fetch one {noun}'s full configuration with get_asset. To "
            "modify, use update_asset with ONLY the fields to change."
        ),
        list_empty=(
            f"No {noun}s configured on this gateway yet - create one with "
            "create_asset (dry_run first)."
        ),
        get_deleted=(
            f"{noun.capitalize()} {{name}} was deleted. create_asset "
            "re-creates it under the same name."
        ),
        get_offline=(
            f" The {noun} is offline - 'detail' holds the last error."
        ),
        get_paused="",
        create_revive=(
            "{name} previously existed and was deleted - re-creating "
            "revives the name"
        ),
        create_success="Created. Verify with get_asset in a few seconds.",
        update_no_change=(
            f"The {noun} is already configured this way - nothing was "
            "written."
        ),
        update_success="Updated. Verify with get_asset in a few seconds.",
        delete_dry_run=(
            f"Would soft-delete {noun} {{name}}. The name becomes reusable. "
            "Confirm with the user, then re-call with dry_run=false."
        ),
        delete_success=(
            "Soft-deleted. To undo, call create_asset with the values in "
            "'previous'."
        ),
    )


def _entity_coerce(cfg, fields):
    """Run the entity's column hygiene, or pass through when it has none."""
    if cfg.entity.coerce is None:
        return dict(fields), []
    return cfg.entity.coerce(fields)


class ToolConfig:
    """Internal holder for the per-registration settings (built by
    register_asset_tools / register_config_tools, never by the app)."""

    def __init__(self, validate, name_pattern=ASSET_NAME_PATTERN,
                 audit_column=None, max_assets=200, entity=None):
        self.validate = validate
        self.name_pattern = re.compile(name_pattern)
        self.audit_column = audit_column
        self.max_assets = max_assets
        # None = the collector preset entity: keeps ToolConfig(validate)
        # meaning exactly what it did before entities existed. Imported
        # lazily -- collector.py imports this module at its top.
        if entity is None:
            from .collector import _COLLECTOR_ENTITY
            entity = _COLLECTOR_ENTITY
        self.entity = entity
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


def _not_found(cfg, asset_name, rows, extra_hint=""):
    noun = cfg.entity.noun
    names = sorted(
        {r.get("asset_name") for r in rows if not r.get("deleted")} - {None}
    )
    suggestions = _suggest(asset_name, names)
    hint = (
        f"No {noun} named {asset_name!r} on this gateway."
        + (f" Closest matches: {', '.join(suggestions)}." if suggestions else "")
        + " Call list_assets to see what exists."
        + (f" {extra_hint}" if extra_hint else "")
    )
    return _rejected(
        "not_found", [f"{noun} {asset_name!r} not found"], hint,
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
            f"{cfg.entity.noun.capitalize()} names must match "
            f"{cfg.name_pattern.pattern}.",
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
    statuses = {}
    if store.status_table:
        status_rows, status_err = await store.read_statuses()
        statuses = _status_map(status_rows)
        if status_err is not None:
            warnings.append(f"live status unavailable: {status_err}")

    def entry(row):
        stripped = strip_platform(row)
        if store.datapoints_table:
            spec = stripped.pop("datapoint_spec", None)
            if isinstance(spec, str) and spec:
                stripped["datapoint_spec"] = {
                    "chars": len(spec),
                    "preview": spec[:200],
                }
            else:
                stripped["datapoint_spec"] = {"chars": 0, "preview": ""}
        if store.status_table:
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
    hint = (cfg.entity.hints.list_nonempty if assets
            else cfg.entity.hints.list_empty)
    return _finish(
        _ok("ok", hint, count=len(assets), assets=assets), warnings=warnings
    )


async def get_asset(store, cfg, asset_name):
    name, problems = _check_name(asset_name, cfg)
    if problems:
        return _rejected(
            "invalid_name", problems,
            f"{cfg.entity.noun.capitalize()} names must match "
            f"{cfg.name_pattern.pattern}.",
        )
    rows, err = await store.read_assets()
    if err is not None:
        return _link_failed(err)
    row = next((r for r in rows if r.get("asset_name") == name), None)
    if row is None:
        return _not_found(cfg, name, rows)
    if row.get("deleted"):
        return _ok(
            "ok",
            cfg.entity.hints.get_deleted.format(name=repr(name)),
            found=True, deleted=True, asset=_spec_for_response(strip_platform(row)),
        )

    warnings = []
    extra = {}
    live_status = None
    if store.status_table:
        status_rows, status_err = await store.read_statuses()
        if status_err is not None:
            warnings.append(f"live status unavailable: {status_err}")
        live_status = _status_map(status_rows).get(name)
        extra["live_status"] = live_status

    if store.datapoints_table:
        dp_rows, dp_err = await store.read_datapoints(name)
        if dp_err is not None:
            warnings.append(f"datapoint catalog unavailable: {dp_err}")
        live_dps = [r for r in dp_rows if not r.get("deleted")]
        extra["datapoints"] = {
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
    if status_value == "offline" and cfg.entity.hints.get_offline:
        hint += cfg.entity.hints.get_offline
    elif status_value == "paused" and cfg.entity.hints.get_paused:
        hint += cfg.entity.hints.get_paused
    return _finish(
        _ok(
            "ok", hint,
            found=True,
            asset=_spec_for_response(strip_platform(row)),
            _meta=meta,
            **extra,
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

    noun = cfg.entity.noun
    live, _ = _split_rows(rows)
    existing = next((r for r in rows if r.get("asset_name") == name), None)
    warnings, ignored = [], []
    if existing is not None and not existing.get("deleted"):
        return _rejected(
            "already_exists",
            [f"{noun} {name!r} already exists on this gateway"],
            f"{noun.capitalize()} names are unique per gateway - use "
            "update_asset to change it, or choose a different name.",
            asset=_spec_for_response(strip_platform(existing)),
        )
    if existing is not None and existing.get("deleted"):
        warnings.append(cfg.entity.hints.create_revive.format(name=repr(name)))
    for row in live:
        other = row.get("asset_name") or ""
        if other != name and other.casefold() == name.casefold():
            warnings.append(
                f"a similarly named {noun} {other!r} already exists"
            )
    if len(live) >= cfg.max_assets:
        return _rejected(
            "limit_exceeded",
            [f"this gateway already has {len(live)} {noun}s (limit "
             f"{cfg.max_assets})"],
            f"Delete unused {noun}s first, or raise the limit in the app.",
        )

    clean, problems, ignored = _partition_fields(fields, name, store.device_key)
    if problems:
        return _finish(
            _rejected("protected_column", problems,
                      "Remove the protected fields and retry."),
            ignored=ignored,
        )
    candidate = dict(cfg.entity.create_defaults)
    candidate.update(clean)
    candidate, problems = _entity_coerce(cfg, candidate)
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
    coerced_row, problems = _entity_coerce(cfg, row)
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
            f"create {noun} {name!r} failed: {append_err}",
            level="warn",
            user_message=(
                f"The AI assistant could not create {noun} '{name}' - "
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
        f"agent created {noun} {name!r}",
        level="info",
        user_message=f"AI assistant created {noun} '{name}'.",
    )
    return _finish(
        _build_write_response(
            "applied", name, row, verdict, latest, detail,
            created=True,
            success_hint=cfg.entity.hints.create_success,
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
    noun = cfg.entity.noun
    existing = next((r for r in rows if r.get("asset_name") == name), None)
    if existing is None:
        return _not_found(cfg, name, rows)
    if existing.get("deleted"):
        return _rejected(
            "not_found",
            [f"{noun} {name!r} was deleted"],
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
            f"this {noun} is maintained by network discovery - the scanner "
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
    clean, problems = _entity_coerce(cfg, clean)
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
    coerced_row, problems = _entity_coerce(cfg, row)
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
                cfg.entity.hints.update_no_change,
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
            f"update {noun} {name!r} failed: {append_err}",
            level="warn",
            user_message=(
                f"The AI assistant could not update {noun} '{name}' - "
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
        f"agent updated {noun} {name!r}: {summary}",
        level="info",
        user_message=f"AI assistant updated {noun} '{name}': {summary}"[:500],
    )
    return _finish(
        _build_write_response(
            "applied", name, row, verdict, latest, detail,
            updated=True, changed_fields=changed_fields,
            previous=strip_platform(existing),
            success_hint=cfg.entity.hints.update_success,
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
    noun = cfg.entity.noun
    existing = next((r for r in rows if r.get("asset_name") == name), None)
    if existing is None:
        return _not_found(
            cfg, name, rows, extra_hint="Nothing was deleted."
        )

    live_dps, warnings = [], []
    if store.datapoints_table:
        dp_rows, dp_err = await store.read_datapoints(name)
        live_dps = [r for r in dp_rows if not r.get("deleted")]
        if dp_err is not None:
            warnings.append(f"datapoint catalog unavailable: {dp_err}")

    if existing.get("deleted"):
        # Convergent: finish whatever a partial earlier delete left behind.
        if not live_dps:
            return _ok(
                "no_change",
                f"{noun.capitalize()} {name!r} is already deleted.",
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
        extra = {}
        dp_clause = ""
        if store.datapoints_table:
            dp_clause = f" and its {len(live_dps)} datapoint entries"
            extra["datapoints_to_delete"] = len(live_dps)
        return _finish(
            _ok(
                "dry_run",
                cfg.entity.hints.delete_dry_run.format(
                    name=repr(name), dp_clause=dp_clause
                ),
                changed=True,
                previous=_spec_for_response(strip_platform(existing)),
                **extra,
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
            f"delete {noun} {name!r} failed: {append_err}",
            level="warn",
            user_message=(
                f"The AI assistant could not delete {noun} '{name}' - "
                "platform connection problem."
            ),
        )
        return _failed(
            "write_failed", append_err,
            "The delete was not acknowledged - call get_asset to check, "
            "then retry.",
        )

    extra = {}
    deleted_count, failures = 0, []
    if store.datapoints_table:
        deleted_count, failures = await store.cascade_delete_datapoints(
            name, audit=cfg.audit()
        )
        extra["datapoints_deleted"] = deleted_count
        extra["datapoints_failed"] = len(failures)
    verdict, _, detail = await store.verify_asset(name, tombstone["tsp"])
    toast = f"AI assistant deleted {noun} '{name}'"
    if deleted_count:
        toast += f" ({deleted_count} datapoint entries removed)"
    if store.datapoints_table:
        message = (
            f"agent deleted {noun} {name!r} "
            f"(datapoints removed: {deleted_count}, failed: {len(failures)})"
        )
    else:
        message = f"agent deleted {noun} {name!r}"
    await store.notify(
        message,
        level="warn" if failures else "info",
        user_message=toast + ".",
    )
    hint = cfg.entity.hints.delete_success
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
            previous=_spec_for_response(strip_platform(existing)),
            **extra,
        ),
        warnings=warnings + failures,
    )


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

    handlers = {
        "list": wrap(_list, ("include_deleted",), mutating=False),
        "get": wrap(_get, ("asset_name",), mutating=False),
        "create": wrap(_create, ("asset_name", "fields", "dry_run"),
                       mutating=True),
        "update": wrap(_update, ("asset_name", "changes", "expected_tsp",
                                 "dry_run"), mutating=True),
        "delete": wrap(_delete, ("asset_name", "expected_tsp", "dry_run"),
                       mutating=True),
    }

    if store.datapoints_table:
        from .collector import list_datapoints, set_datapoints

        async def _list_datapoints(asset_name=None):
            return await list_datapoints(store, cfg, asset_name)

        async def _set_datapoints(asset_name=None, changes=None,
                                  dry_run=False):
            value, problem = _parse_bool_param(dry_run, "dry_run")
            if problem:
                return _rejected("invalid_value", [problem], "Pass a boolean.")
            return await set_datapoints(store, cfg, asset_name,
                                        changes=changes, dry_run=value)

        handlers["list_datapoints"] = wrap(
            _list_datapoints, ("asset_name",), mutating=False
        )
        handlers["set_datapoints"] = wrap(
            _set_datapoints, ("asset_name", "changes", "dry_run"),
            mutating=True,
        )

    return handlers


# Historical import paths: these names lived here before the collector
# preset moved to collector.py, and consuming apps pin them in drift tests
# (e.g. config_core.handlers.USER_DATAPOINT_COLUMNS). Lazy so that
# collector.py can import this module at its top without a cycle.
_COLLECTOR_COMPAT = (
    "CORE_DEFAULTS",
    "BOOL_COLUMNS",
    "USER_DATAPOINT_COLUMNS",
    "DATAPOINT_COERCERS",
    "MAX_SPEC_CHARS",
    "DATAPOINT_BATCH_CAP",
    "RECOMMENDED_PROMPT_GUIDANCE",
    "SQL_DIAGNOSTICS_GUIDANCE",
)


def __getattr__(name):
    if name in _COLLECTOR_COMPAT:
        from . import collector
        return getattr(collector, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
