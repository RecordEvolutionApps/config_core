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
``deleted=true``, and the handlers need to see it (re-create hints, cascade
re-sweeps), so live/deleted partitioning happens in the handlers.

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

ASSETS_LIMIT = 1000      # matches collector_core's own assets query window
DATAPOINTS_LIMIT = 3000  # matches collector_core's datapoints query window


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
    Empty dict == no effective change."""
    diff = {}
    for key in set(candidate) | set(existing):
        if key in ignore or key in PLATFORM_COLUMNS or key in RUNTIME_KEYS:
            continue
        old, new = existing.get(key), candidate.get(key)
        if not normalized_equal(old, new):
            diff[key] = {"from": old, "to": new}
    return diff


def parse_tsp(value):
    """Parse an ISO tsp into an aware datetime, or None when unparsable.
    Tolerates the trailing-Z form (Python 3.10 fromisoformat does not)."""
    if not value:
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
    """Reads and writes one entity table (plus its optional datapoints and
    status side tables) for ONE gateway (``gateway_id == device_key``).
    Never raises: every method returns explicit values plus an error string.

    Table topology lives here, once. The defaults are the collector family's
    tables; ``register_config_tools`` injects the app's own ``table`` and
    passes ``datapoints_table=None`` (no datapoint tools) and its
    ``status_table``. A ``None`` side table disables the corresponding reads
    and the handler features built on them."""

    def __init__(self, ironflock, device_key, verify_retry_delay=1.0,
                 table="assets", datapoints_table="datapoints",
                 status_table="assetstatus"):
        self.ironflock = ironflock
        self.device_key = int(device_key)
        self.verify_retry_delay = verify_retry_delay
        self.table = table
        self.datapoints_table = datapoints_table
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
        """(rows, error) -- latest datapoint rows of one asset (live and
        deleted). Only called when ``datapoints_table`` is set."""
        return await self._read_latest(
            self.datapoints_table,
            DATAPOINTS_LIMIT,
            [{"column": "asset_name", "operator": "=", "value": asset_name}],
        )

    async def read_statuses(self):
        """(rows, error) -- latest status row per asset of this gateway.
        Only called when ``status_table`` is set."""
        return await self._read_latest(self.status_table, ASSETS_LIMIT)

    # ------------------------------------------------------------- writes

    def check_asset_payload(self, payload):
        """Poison-row invariants for an assets append; returns a problem
        string or None. A malformed asset row is soft-deleted-and-warned at
        the gateway's next startup -- never write one."""
        problems = self._check_common(payload)
        if problems:
            return problems
        return None

    def check_datapoint_payload(self, payload):
        """Poison-row invariants for a datapoints append. This is the
        fleet-crash class: the collector's datapoints subscription handler
        []-indexes datapoint_id/asset_name/deleted, so a row missing any of
        them crashes the handler on every subscribed gateway."""
        problems = self._check_common(payload)
        if problems:
            return problems
        datapoint_id = payload.get("datapoint_id")
        if not isinstance(datapoint_id, str) or not datapoint_id.strip():
            return "datapoint payload must carry a non-empty datapoint_id"
        return None

    def _check_common(self, payload):
        name = payload.get("asset_name")
        if not isinstance(name, str) or not name.strip():
            return "payload must carry a non-empty asset_name"
        if payload.get("gateway_id") != self.device_key:
            return (
                f"payload gateway_id must be this gateway's device key "
                f"({self.device_key}), got {payload.get('gateway_id')!r}"
            )
        if not isinstance(payload.get("deleted"), bool):
            return "payload deleted flag must be a real boolean"
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

    async def append_datapoint(self, payload):
        """(acked, error) -- append one datapoints row after the strict
        invariant choke point."""
        return await self._append(
            self.datapoints_table, payload, self.check_datapoint_payload
        )

    async def _append(self, table, payload, check):
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

    # ------------------------------------------------------------ cascade

    async def cascade_delete_datapoints(self, asset_name, audit=None):
        """Soft-delete every live datapoints row of the asset with partial
        tombstones (the platform's carry-over merge preserves the rest of
        each row). Best-effort per row; returns ``(deleted_count,
        failures)`` where failures is a list of "datapoint_id: error"
        strings. Convergent: re-running sweeps whatever a partial run left
        behind."""
        rows, err = await self.read_datapoints(asset_name)
        if err is not None:
            return 0, [f"could not read datapoints: {err}"]
        deleted, failures = 0, []
        for row in rows:
            if row.get("deleted"):
                continue
            payload = {
                "asset_name": asset_name,
                "datapoint_id": row.get("datapoint_id"),
                "gateway_id": self.device_key,
                "deleted": True,
                "tsp": now_iso(),
            }
            if audit:
                column, value = audit
                payload[column] = value
            acked, append_err = await self.append_datapoint(payload)
            if acked:
                deleted += 1
            else:
                failures.append(f"{row.get('datapoint_id')}: {append_err}")
        return deleted, failures


def _find_by_name(rows, asset_name):
    for row in rows:
        if row.get("asset_name") == asset_name:
            return row
    return None


def _judge(row, written_tsp):
    """Compare the latest row's tsp against the tsp we stamped."""
    if row is None:
        return "pending", "no row visible for the asset yet"
    ours, theirs = parse_tsp(written_tsp), parse_tsp(row.get("tsp"))
    if ours is None or theirs is None:
        return "pending", "row tsp not comparable"
    if theirs == ours:
        return "verified", None
    if theirs > ours:
        return "superseded", None
    return "pending", "latest visible row is older than our write"
