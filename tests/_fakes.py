"""In-memory ironflock SDK fake for the config_core test suite.

Models the platform semantics the library depends on, honestly:

- append_to_table stores rows per table and stamps the platform columns
  (authid, device_key) the way fleetdb does; identity-key comparison is
  numeric-tolerant (the platform returns numeric columns as strings).
- getHistory implements the >= 1.6.0 read model: the ``{"latest": True}``
  filterAnd marker returns the newest row per identity (the table's
  maintainLatestFlagFor key), plus ``=`` predicates and limit. There is no
  physical latest_flag column.
- Failures RAISE (RuntimeError), like SDK 1.6.0; ``none_mode`` switches the
  injected failures to the pre-1.6 style of returning None instead.
- ``defer_visibility`` hides newly appended rows from getHistory until
  ``settle()`` -- exercises the store's verify retry honestly.
"""

import asyncio

IDENTITY_KEYS = {
    "assets": ("gateway_id", "asset_name"),
    "datapoints": ("gateway_id", "asset_name", "datapoint_id"),
    "assetstatus": ("gateway_id", "asset_name"),
    "gateways": ("gateway_name",),
}


def _norm_key(value):
    if isinstance(value, bool) or value is None:
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return str(value)


def _identity(table, row):
    return tuple(_norm_key(row.get(k)) for k in IDENTITY_KEYS.get(table, ("tsp",)))


class FakeIronflock:
    def __init__(self, device_key=471, none_mode=False, defer_visibility=False):
        self.device_key = device_key
        self.none_mode = none_mode
        self.defer_visibility = defer_visibility
        self.tables = {}          # table -> list of rows (insertion order)
        self.pending = []         # (table, row) appended but not yet visible
        self.calls = []           # (method, table_or_topic)
        self.reported = []        # report_error recordings
        self.registered = {}      # topic -> handler
        self._failures = {}       # method -> [error message, ...]
        self._fail_report = 0
        self._on_append = None

    # ------------------------------------------------------------ helpers

    def fail_next(self, method, n=1, error="injected platform failure"):
        self._failures.setdefault(method, []).extend([error] * n)

    def fail_report_error(self, n=1):
        self._fail_report += n

    def on_append(self, callback):
        """callback(table, row) invoked after each successful append --
        deterministic interleaving of an external writer."""
        self._on_append = callback

    def external_append(self, table, row, authid="user-9"):
        """A row written by someone else (board user, discovery). The
        platform stamps the ACTUAL writer's authid, so it overrides any
        echoed value."""
        stored = dict(row)
        stored["authid"] = authid
        stored.setdefault("device_key", self.device_key)
        self.tables.setdefault(table, []).append(stored)
        return stored

    def settle(self):
        """Make deferred appends visible to getHistory."""
        for table, row in self.pending:
            self.tables.setdefault(table, []).append(row)
        self.pending = []

    def rows(self, table):
        return list(self.tables.get(table, []))

    def latest(self, table, **key):
        """Newest visible row matching the key columns (test convenience)."""
        found = None
        for row in self.tables.get(table, []):
            if all(_norm_key(row.get(k)) == _norm_key(v) for k, v in key.items()):
                found = row
        return found

    def _maybe_fail(self, method):
        queue = self._failures.get(method)
        if queue:
            message = queue.pop(0)
            if self.none_mode:
                return True, None
            raise RuntimeError(message)
        return False, None

    # ---------------------------------------------------------- SDK surface

    async def append_to_table(self, table, payload):
        self.calls.append(("append_to_table", table))
        failed, value = self._maybe_fail("append_to_table")
        if failed:
            return value
        row = dict(payload)
        row["authid"] = f"device-{self.device_key}"
        row["device_key"] = self.device_key
        if self.defer_visibility:
            self.pending.append((table, row))
        else:
            self.tables.setdefault(table, []).append(row)
        if self._on_append is not None:
            self._on_append(table, row)
        return {"success": True}

    async def getHistory(self, table, params):
        self.calls.append(("getHistory", table))
        failed, value = self._maybe_fail("getHistory")
        if failed:
            return value
        rows = list(self.tables.get(table, []))
        latest_mode = False
        for cond in params.get("filterAnd") or []:
            if cond.get("latest") is True:
                latest_mode = True
                continue
            column, op, wanted = cond["column"], cond["operator"], cond["value"]
            assert op == "=", f"fake only implements '=' (got {op!r})"
            rows = [
                r for r in rows if _norm_key(r.get(column)) == _norm_key(wanted)
            ]
        if latest_mode:
            by_identity = {}
            for row in rows:  # insertion order == commit order
                by_identity[_identity(table, row)] = row
            rows = list(by_identity.values())
        return rows[: params.get("limit", 10)]

    async def register_device_function(self, topic, handler):
        self.calls.append(("register_device_function", topic))
        failed, value = self._maybe_fail("register_device_function")
        if failed:
            return value
        self.registered[topic] = handler
        return object()

    async def report_error(self, message, level="error", user_message=None):
        if self._fail_report:
            self._fail_report -= 1
            raise RuntimeError("toast channel down")
        self.reported.append(
            {"msg": str(message), "level": level, "user_message": user_message}
        )
        return {"success": True}


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)
