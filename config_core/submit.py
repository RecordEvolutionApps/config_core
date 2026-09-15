"""Tell the user how their board form save went.

A board's `widget-form` publishes the row and closes its dialog: it never
learns whether the app can use what was saved. The app is the only party that
can say - it checks the row when it arrives - so this closes the loop the other
way, with one `success` or `error` row in `error-logs`, which a board's
`widget-toast` pops up.

Three rules, each of which is a bug somebody would otherwise write:

* **Only a PERSON's save is answered.** The platform stamps `authid` on every
  row. A browser session's is a numeric account key; an app container presents
  its per-app credential (`app-<n>-<stage>-e<k>@<serial>`) or, on an older
  device agent, the device serial - never digits alone. So a digits-only authid
  is somebody at a form, while the assistant's writes (already answered by the
  tools' own toasts) and the app's own echoes of a save stay silent.
* **Once per row.** Keyed on the row's name and `tsp`: a row delivered twice is
  still one save.
* **Never raises.** Feedback about a save must not break applying it.

The severities are the TOAST widget's vocabulary - `success` and `error` - not
the log's: `widget-toast` renders any value it does not know as `info`.
"""

import asyncio
import re
from collections import OrderedDict

#: A browser session's authid: fleetdb resolves it as a bigint account key.
PERSON_AUTHID_RE = re.compile(r"^[0-9]+$")

#: How many (name, tsp) pairs are remembered for the once-per-row rule.
REMEMBERED_SAVES = 256


def submitted_by_person(row):
    """True when the platform stamped the row with a person's authid."""
    authid = (row or {}).get("authid")
    if authid is None or isinstance(authid, bool):
        return False
    return bool(PERSON_AUTHID_RE.match(str(authid).strip()))


class SubmitFeedback:
    """Answer a person's form save with one success or error toast.

    ``check(row)`` is the app's own question - "what is wrong with this row?" -
    and returns a list of problems (empty when usable); it may be async. It is
    NOT the tools' validate function: a form legitimately writes things the
    assistant may not, a password among them.

    ``report(message, user_message, level, name)`` replaces the default
    ``ironflock.report_error`` - an app with its own rate-limited reporter
    passes it here, and gets the entity name to key it on.
    """

    def __init__(self, ironflock, check, *, noun="asset", report=None,
                 remember=REMEMBERED_SAVES):
        self.ironflock = ironflock
        self.check = check
        self.noun = noun
        self.report = report
        self.remember = remember
        self._seen = OrderedDict()

    async def acknowledge(self, row, error=None):
        """Toast one saved row.

        ``error`` is an exception the app hit while applying the row; it is
        reported instead of running ``check``. Returns ``"success"``,
        ``"error"``, or ``None`` when nothing was sent: not a person's save, no
        name, or a row already answered.
        """
        try:
            return await self._acknowledge(row, error)
        except Exception:
            # Same promise as `_emit`: the save outranks the note about it.
            return None

    async def _acknowledge(self, row, error):
        if not isinstance(row, dict) or not submitted_by_person(row):
            return None
        name = str(row.get("asset_name") or "").strip()
        if not name:
            return None
        key = (name, str(row.get("tsp")))
        if key in self._seen:
            return None
        self._seen[key] = True
        while len(self._seen) > self.remember:
            self._seen.popitem(last=False)

        label = self.noun[:1].upper() + self.noun[1:]
        deleted = str(row.get("deleted")).strip().lower() == "true"
        if error is not None:
            problems = [str(error) or type(error).__name__]
        elif deleted:
            problems = []
        else:
            problems = await self._problems(row)

        if problems:
            joined = "; ".join(problems)
            await self._emit(
                name, "error",
                f"{self.noun} {name!r} was saved from a form but cannot run: {joined}",
                f"{label} '{name}' was saved, but it cannot run: {joined}",
            )
            return "error"
        verb = "deleted" if deleted else "saved"
        await self._emit(
            name, "success",
            f"{self.noun} {name!r} {verb} from a form",
            f"{label} '{name}' {verb}.",
        )
        return "success"

    async def _problems(self, row):
        try:
            result = self.check(dict(row))
            if asyncio.iscoroutine(result):
                result = await result
        except Exception as e:
            return [f"validation failed: {e}"]
        return [str(problem) for problem in (result or [])]

    async def _emit(self, name, level, message, user_message):
        try:
            if self.report is not None:
                result = self.report(message, user_message, level, name)
            else:
                result = self.ironflock.report_error(
                    message, level=level, user_message=user_message)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            # A toast channel that is down must not fail the save it describes.
            pass
