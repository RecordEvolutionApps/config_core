"""All platform I/O for the asset config tools, scoped to one gateway.

The store is internal: it is constructed by ``register_asset_tools`` and never
by the app. Every method catches SDK errors (ironflock >= 1.6.0 raises proper
exceptions; a ``None`` return -- the pre-1.6 failure style -- is defensively
treated as failure too) and translates them into ``(value, error)`` results
carrying the SDK's error text, so the handlers stay never-raise while agent
responses cite the real cause.

Reads use the 1.6.0 ``{"latest": True}`` filterAnd marker: the data backend
derives the latest row per entity (the table's ``maintainLatestFlagFor`` key)
in SQL. There is no physical ``latest_flag`` column anymore. Reads never
filter on ``deleted`` -- the latest row of a deleted asset has
``deleted=true``, and the handlers need to see it (re-create hints,
'previous' undo), so live/deleted partitioning happens in the handlers.

One writer per table (collector_core >= 3.0.0): this library writes the
entity table (``assets``) and the per-datapoint user settings
(``datapoint_settings``). The datapoint catalog (``datapoints``) belongs to
the gateway -- it is read here, never written; the gateway itself
soft-deletes a deleted asset's catalog rows.

Writes rely on the fleetdb carry-over merge for entity tables: an appended
payload is merged onto the previous latest row of its identity -- columns
present in the payload (explicit null included) overwrite, absent columns
carry over, across tombstones too. Mutations therefore send only the
changed columns plus the identity/protected ones; full rows are written
only on create (which explicitly nulls a revived name's stale columns).
"""

import asyncio
from datetime import datetime, timezone

# Columns the platform stamps onto rows it returns; never echoed back on
# writes. Mirrors collector_core.adapter.PLATFORM_COLUMNS (duplicated by
# design: this library imports nothing; a change there is a coordinated
# release here). latest_flag no longer physically exists on >= 1.6.0
# platforms but stays in the strip list for rows from older deployments.
PLATFORM_COLUMNS = ("tsp", "latest_flag", "authid", "device_key")

# Runtime-only key collector_core caches on asset dicts; never persisted.
RUNTIME_KEYS = ("datapoint_list",)

# What getHistory returns for a non-null value of a ``secret: true`` column
# (ironflock.types.SECRET_PLACEHOLDER; hard-coded -- this library imports
# nothing). It is never written back: every append drops a column holding it,
# so the platform's carry-over merge keeps the stored secret. Responses show
# a set secret as SECRET_DISPLAY instead.
SECRET_PLACEHOLDER = "__secret__"
SECRET_DISPLAY = "(set)"


def is_secret_placeholder(value):
    return isinstance(value, str) and value == SECRET_PLACEHOLDER


def drop_secret_placeholders(row):
    """Row minus every column whose value is the secret placeholder."""
    return {k: v for k, v in row.items() if not is_secret_placeholder(v)}


def mask_secrets(value):
    """Deep copy of a response with every secret placeholder replaced by
    SECRET_DISPLAY -- the agent sees that a secret is set, never the
    platform's internal marker (which it might echo back)."""
    if is_secret_placeholder(value):
        return SECRET_DISPLAY
    if isinstance(value, dict):
        return {k: mask_secrets(v) for k, v in value.items()}
    if isinstance(value, list):
        return [mask_secrets(v) for v in value]
    return value

# Read windows. One gateway's assets (max_assets caps creations at 200 by
# default) and one asset's catalog and settings rows; collector_core reads up
# to 10000 rows per table for the whole gateway.
ASSETS_LIMIT = 1000
DATAPOINTS_LIMIT = 3000
SETTINGS_LIMIT = 3000

# The columns a datapoint_settings row may carry besides the optional audit
# column: the identity, tsp and the user-owned settings. Mirrors
# collector.USER_DATAPOINT_COLUMNS (kept literal here: the store imports
# nothing from the handler layer), which mirrors collector_core's
# DATAPOINT_SETTINGS_COLUMNS. The table has no ``deleted`` column.
SETTINGS_IDENTITY = ("gateway_id", "asset_name", "datapoint_id")
SETTINGS_VALUE_COLUMNS = (
    "enabled", "change_detection", "demo_value", "demo_variance",
)


def now_iso():
    """UTC ISO-8601 timestamp, the family's row tsp convention."""
    return datetime.now(timezone.utc).isoformat()


def strip_platform(row):
    """Row minus platform-stamped columns and runtime keys -- the echo-merge
    base for the validate candidate and the no-change comparison."""
    return {
        k: v
        for k, v in row.items()
        if k not in PLATFORM_COLUMNS and k not in RUNTIME_KEYS
    }


def _norm(value):
    """Normalize a value for change comparison (DatapointStore._norm
    semantics): bools and None compare as themselves, numerics as float --
    the platform returns numeric columns as strings -- everything else as
    str."""
    if isinstance(value, bool) or value is None:
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return str(value)


def normalized_equal(a, b):
    """Value-wise equality under _norm -- 30, "30" and 30.0 are equal."""
    return _norm(a) == _norm(b)


def normalized_diff(candidate, existing, ignore=("tsp",)):
    """{column: {"from": old, "to": new}} for every column whose normalized
    value differs between the two rows (union of keys, ``ignore`` skipped).
    Empty dict == no effective change.

    Secret columns: a candidate still holding the placeholder, or no longer
    holding a column whose stored value is the placeholder, is not a change
    -- the placeholder is never written, so the stored secret stays. Only
    an explicit value (null included) replaces a stored secret."""
    diff = {}
    for key in set(candidate) | set(existing):
        if key in ignore or key in PLATFORM_COLUMNS or key in RUNTIME_KEYS:
            continue
        old, new = existing.get(key), candidate.get(key)
        if is_secret_placeholder(new):
            continue
        if is_secret_placeholder(old) and key not in candidate:
            continue
        if not normalized_equal(old, new):
            diff[key] = {"from": old, "to": new}
    return diff


def parse_tsp(value):
    """Parse a tsp into an aware datetime, or None when unparsable.

    Two forms, because a row's tsp comes back in a different one than it was
    written in: this library stamps an ISO string, and fleetdb's history read
    path turns every timestamptz into EPOCH MILLISECONDS (a number). Accepting
    only ISO made every read-back "not comparable", so every write reported
    "unverified" while it had landed. Tolerates the trailing-Z form (Python
    3.10 fromisoformat does not)."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _error_text(e):
    """One-line error text for a caught SDK exception."""
    text = str(e).strip()
    return text or type(e).__name__


class AssetStore:
    """Reads and writes one entity table (plus its optional datapoint and
    status side tables) for ONE gateway (``gateway_id == device_key``).
    Never raises: every method returns explicit values plus an error string.

    Table topology lives here, once. The defaults are the collector family's
    tables: ``datapoints_table`` is the gateway-written catalog (read only),
    ``settings_table`` the user/agent-written per-datapoint settings.
    ``register_config_tools`` injects the app's own ``table`` and passes
    ``datapoints_table=None`` / ``settings_table=None`` (no datapoint tools)
    and its ``status_table``. A ``None`` side table disables the
    corresponding reads and the handler features built on them."""

    def __init__(self, ironflock, device_key, verify_retry_delay=1.0,
                 table="assets", datapoints_table="datapoints",
                 status_table="assetstatus",
                 settings_table="datapoint_settings"):
        self.ironflock = ironflock
        self.device_key = int(device_key)
        self.verify_retry_delay = verify_retry_delay
        self.table = table
        self.datapoints_table = datapoints_table
        self.settings_table = settings_table if datapoints_table else None
        self.status_table = status_table

    # ------------------------------------------------------------- notify

    async def notify(self, message, level="info", user_message=None):
        """Best-effort operator toast via the SDK's report_error (which
        raises when the link is down on >= 1.6.0) -- a failed toast must
        never fail the operation it reports on."""
        try:
            await self.ironflock.report_error(
                message, level=level, user_message=user_message
            )
        except Exception as e:
            print(f"config_core: toast failed ({e}): {message}")

    # -------------------------------------------------------------- reads

    async def _read_latest(self, table, limit, extra_filters=()):
        """(rows, error) -- latest row per entity for this gateway."""
        params = {
            "limit": limit,
            "filterAnd": [
                {"latest": True},
                {"column": "gateway_id", "operator": "=", "value": self.device_key},
                *extra_filters,
            ],
        }
        try:
            rows = await self.ironflock.getHistory(table, params)
        except Exception as e:
            return [], _error_text(e)
        if rows is None:
            return [], f"reading {table} failed (no response from the platform)"
        return list(rows), None

    async def read_assets(self):
        """(rows, error) -- latest asset rows of this gateway, live AND
        deleted (partitioning is the handlers' job)."""
        return await self._read_latest(self.table, ASSETS_LIMIT)

    async def read_datapoints(self, asset_name):
        """(rows, error) -- latest catalog rows of one asset (live and
        deleted). Only called when ``datapoints_table`` is set."""
        return await self._read_latest(
            self.datapoints_table,
            DATAPOINTS_LIMIT,
            [{"column": "asset_name", "operator": "=", "value": asset_name}],
        )

    async def read_settings(self, asset_name):
        """(rows, error) -- latest datapoint_settings row per datapoint of
        one asset. Only called when ``settings_table`` is set."""
        return await self._read_latest(
            self.settings_table,
            SETTINGS_LIMIT,
            [{"column": "asset_name", "operator": "=", "value": asset_name}],
        )

    async def read_statuses(self):
        """(rows, error) -- latest status row per asset of this gateway.
        Only called when ``status_table`` is set."""
        return await self._read_latest(self.status_table, ASSETS_LIMIT)

    # ------------------------------------------------------------- writes

    def check_asset_payload(self, payload):
        """Poison-row invariants for an assets append; returns a problem
        string or None. The gateway skips a malformed asset row and reports
        it until a user fixes or deletes it -- never write one."""
        problems = self._check_common(payload)
        if problems:
            return problems
        return None

    def check_setting_payload(self, payload, audit_column=None):
        """Poison-row invariants for a datapoint_settings append. The
        gateway follows this table live and keys every row on
        (gateway_id, asset_name, datapoint_id): a row missing one of them
        is useless at best and a crashed subscription handler at worst. The
        table has a fixed shape -- identity, tsp, the four settings and the
        audit column -- so anything else (``deleted`` included: the table
        has no such column) is refused."""
        problem = self._check_identity(payload)
        if problem:
            return problem
        datapoint_id = payload.get("datapoint_id")
        if not isinstance(datapoint_id, str) or not datapoint_id.strip():
            return "settings payload must carry a non-empty datapoint_id"
        allowed = set(SETTINGS_IDENTITY) | set(SETTINGS_VALUE_COLUMNS) | {"tsp"}
        if audit_column:
            allowed.add(audit_column)
        extras = sorted(set(payload) - allowed)
        if extras:
            return (
                f"settings payload must not carry {', '.join(extras)} "
                "(not a datapoint_settings column)"
            )
        if not any(column in payload for column in SETTINGS_VALUE_COLUMNS):
            return "settings payload must carry at least one setting"
        return None

    def _check_common(self, payload):
        problem = self._check_identity(payload)
        if problem:
            return problem
        if not isinstance(payload.get("deleted"), bool):
            return "payload deleted flag must be a real boolean"
        return None

    def _check_identity(self, payload):
        name = payload.get("asset_name")
        if not isinstance(name, str) or not name.strip():
            return "payload must carry a non-empty asset_name"
        if payload.get("gateway_id") != self.device_key:
            return (
                f"payload gateway_id must be this gateway's device key "
                f"({self.device_key}), got {payload.get('gateway_id')!r}"
            )
        if not payload.get("tsp"):
            return "payload must carry a tsp"
        for column in PLATFORM_COLUMNS:
            if column != "tsp" and column in payload:
                return f"platform column {column} must not be written"
        for key in RUNTIME_KEYS:
            if key in payload:
                return f"runtime key {key} must not be written"
        return None

    async def append_asset(self, payload):
        """(acked, error) -- append one assets row after the invariant
        choke point."""
        return await self._append(self.table, payload, self.check_asset_payload)

    async def append_setting(self, payload, audit_column=None):
        """(acked, error) -- append one PARTIAL datapoint_settings row after
        the strict invariant choke point. Columns the payload omits keep
        their value (carry-over); an explicit null clears one."""
        return await self._append(
            self.settings_table, payload,
            lambda p: self.check_setting_payload(p, audit_column),
        )

    async def _append(self, table, payload, check):
        # Never write the secret placeholder back: dropping the column lets
        # the carry-over merge keep the stored secret.
        payload = drop_secret_placeholders(payload)
        problem = check(payload)
        if problem:
            # Internal bug, not agent input: the pipeline should never build
            # such a payload. Refuse to write it.
            return False, f"internal invariant violated: {problem}"
        try:
            result = await self.ironflock.append_to_table(table, payload)
        except Exception as e:
            return False, _error_text(e)
        if result is None:
            return False, f"append to {table} was not acknowledged"
        return True, None

    # ------------------------------------------------------------- verify

    async def verify_asset(self, asset_name, written_tsp):
        """Read back the latest row for the asset and judge the write.

        Returns ``(verdict, row, detail)`` where verdict is:
        - "verified"   -- the latest row is ours (tsp matches),
        - "superseded" -- someone wrote a NEWER row while we were writing
                          (row = the foreign row, detail names its authid),
        - "unverified" -- the read failed or still shows an older row after
                          one retry (append itself was acked; latest-state
                          just could not be confirmed).
        """
        for attempt in (0, 1):
            rows, err = await self.read_assets()
            if err is None:
                row = _find_by_name(rows, asset_name)
                verdict, detail = _judge(row, written_tsp)
                if verdict == "verified":
                    return "verified", row, None
                if verdict == "superseded":
                    authid = (row or {}).get("authid")
                    return (
                        "superseded",
                        row,
                        f"a newer write by {authid or 'another writer'} "
                        f"landed after ours",
                    )
                detail_text = detail
            else:
                detail_text = err
            if attempt == 0 and self.verify_retry_delay:
                await asyncio.sleep(self.verify_retry_delay)
        return "unverified", None, detail_text

    async def verify_settings(self, asset_name, written):
        """Read back the asset's datapoint_settings rows and judge each of
        our writes, ``written`` being ``{datapoint_id: tsp}``.

        Returns ``{datapoint_id: (verdict, row, detail)}`` with the verdicts
        of ``verify_asset``. One read covers the whole batch; when any write
        is still undecided the read is retried once."""
        results, detail_text = {}, None
        for attempt in (0, 1):
            rows, err = await self.read_settings(asset_name)
            if err is not None:
                detail_text = err
            else:
                by_id = {str(r.get("datapoint_id")): r for r in rows}
                for datapoint_id, written_tsp in written.items():
                    if datapoint_id in results:
                        continue
                    row = by_id.get(datapoint_id)
                    verdict, detail = _judge(row, written_tsp)
                    if verdict == "verified":
                        results[datapoint_id] = ("verified", row, None)
                    elif verdict == "superseded":
                        authid = (row or {}).get("authid")
                        results[datapoint_id] = (
                            "superseded", row,
                            f"a newer write by {authid or 'another writer'} "
                            "landed after ours",
                        )
                    else:
                        detail_text = detail
            if len(results) == len(written):
                return results
            if attempt == 0 and self.verify_retry_delay:
                await asyncio.sleep(self.verify_retry_delay)
        for datapoint_id in written:
            results.setdefault(
                datapoint_id, ("unverified", None, detail_text)
            )
        return results


def _find_by_name(rows, asset_name):
    for row in rows:
        if row.get("asset_name") == asset_name:
            return row
    return None


def _to_millisecond(moment):
    return moment.replace(microsecond=moment.microsecond // 1000 * 1000)


def _judge(row, written_tsp):
    """Compare the latest row's tsp against the tsp we stamped.

    At MILLISECOND precision: `now_iso` stamps microseconds and fleetdb reads
    back milliseconds, so an exact comparison sees our own row as slightly
    OLDER than our write, forever."""
    if row is None:
        return "pending", "no row visible for the asset yet"
    ours, theirs = parse_tsp(written_tsp), parse_tsp(row.get("tsp"))
    if ours is None or theirs is None:
        return "pending", "row tsp not comparable"
    ours, theirs = _to_millisecond(ours), _to_millisecond(theirs)
    if theirs == ours:
        return "verified", None
    if theirs > ours:
        return "superseded", None
    return "pending", "latest visible row is older than our write"
