"""SubmitFeedback: a person's form save gets one success or error toast."""

import pytest

from config_core import SubmitFeedback, submitted_by_person
from _fakes import FakeIronflock

PERSON = "4711"
TSP = "2026-09-15T10:00:00.000Z"


def row(**columns):
    base = {"asset_name": "Press 1", "gateway_id": 471, "tsp": TSP,
            "authid": PERSON, "deleted": False}
    base.update(columns)
    return base


def feedback(check=lambda row: [], **kwargs):
    fake = FakeIronflock(device_key=471)
    return fake, SubmitFeedback(fake, check, noun="connection", **kwargs)


@pytest.mark.parametrize("authid, person", [
    ("4711", True),
    (4711, True),
    ("app-7-prod-e1@serial-abc", False),     # a per-app container credential
    ("fb9d535f-b522-4936-9e3c-693f7820dbec", False),   # legacy device serial
    ("system", False),
    (None, False),
    (True, False),
    ("", False),
])
def test_only_a_browser_account_key_is_a_person(authid, person):
    assert submitted_by_person({"authid": authid}) is person


async def test_a_usable_save_is_answered_with_success():
    fake, fb = feedback()
    assert await fb.acknowledge(row()) == "success"
    assert fake.reported == [{"msg": "connection 'Press 1' saved from a form",
                              "level": "success",
                              "user_message": "Connection 'Press 1' saved."}]


async def test_the_problems_the_app_finds_are_the_error_toast():
    fake, fb = feedback(check=lambda row: ["broker address is empty", "no mapping"])
    assert await fb.acknowledge(row()) == "error"
    [toast] = fake.reported
    assert toast["level"] == "error"
    assert toast["user_message"] == (
        "Connection 'Press 1' was saved, but it cannot run: "
        "broker address is empty; no mapping")


async def test_an_async_check_is_awaited():
    async def check(row):
        return ["not yet"]

    fake, fb = feedback(check=check)
    assert await fb.acknowledge(row()) == "error"


async def test_a_check_that_raises_is_an_error_not_a_crash():
    def check(row):
        raise ValueError("boom")

    fake, fb = feedback(check=check)
    assert await fb.acknowledge(row()) == "error"
    assert "validation failed: boom" in fake.reported[0]["user_message"]


async def test_an_error_applying_the_row_replaces_the_check():
    called = []
    fake, fb = feedback(check=lambda row: called.append(row) or [])
    assert await fb.acknowledge(row(), error=RuntimeError("disk full")) == "error"
    assert called == []
    assert "disk full" in fake.reported[0]["user_message"]


async def test_a_delete_is_acknowledged_without_a_check():
    fake, fb = feedback(check=lambda row: ["would fail"])
    assert await fb.acknowledge(row(deleted=True)) == "success"
    assert fake.reported[0]["user_message"] == "Connection 'Press 1' deleted."


@pytest.mark.parametrize("authid", ["app-7-prod-e1@serial-abc", "system", None])
async def test_the_apps_own_and_the_assistants_writes_stay_silent(authid):
    fake, fb = feedback()
    assert await fb.acknowledge(row(authid=authid)) is None
    assert fake.reported == []


async def test_one_row_is_answered_once_and_the_next_save_again():
    fake, fb = feedback()
    assert await fb.acknowledge(row()) == "success"
    assert await fb.acknowledge(row()) is None
    assert await fb.acknowledge(row(tsp="2026-09-15T10:00:05.000Z")) == "success"
    assert len(fake.reported) == 2


async def test_a_row_without_a_name_is_not_answered():
    fake, fb = feedback()
    assert await fb.acknowledge(row(asset_name="  ")) is None
    assert await fb.acknowledge("not a row") is None
    assert fake.reported == []


async def test_a_custom_reporter_gets_the_name_to_key_on():
    seen = []

    async def report(message, user_message, level, name):
        seen.append((level, name))

    fake, fb = feedback(report=report)
    await fb.acknowledge(row())
    assert seen == [("success", "Press 1")] and fake.reported == []


async def test_a_toast_channel_that_is_down_never_raises():
    def report(message, user_message, level, name):
        raise RuntimeError("link down")

    fake, fb = feedback(report=report)
    assert await fb.acknowledge(row()) == "success"


async def test_the_memory_is_bounded():
    fake, fb = feedback(remember=2)
    for second in range(3):
        await fb.acknowledge(row(tsp=f"2026-09-15T10:00:0{second}.000Z"))
    assert len(fb._seen) == 2


async def test_the_same_save_read_and_delivered_is_answered_once():
    # A history read gives the tsp as epoch ms, the table event as ISO text.
    fake, fb = feedback()
    assert await fb.acknowledge(row()) == "success"
    millis = 1789466400000  # 2026-09-15T10:00:00.000Z
    assert await fb.acknowledge(row(tsp=millis)) is None
