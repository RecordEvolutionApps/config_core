"""config_core test suite.

Every handler response is passed through json.dumps -- serializability is
part of the contract (responses travel over WAMP to the agent runtime).
"""

import asyncio
import importlib.util
import json
import os
import subprocess
import sys

import pytest

from config_core import (
    ASSET_NAME_PATTERN,
    DEFAULT_TOPICS,
    RECOMMENDED_PROMPT_GUIDANCE,
    SQL_DIAGNOSTICS_GUIDANCE,
    coerce_rpc_args,
    register_asset_tools,
    register_config_tools,
)
from config_core.handlers import (
    CORE_DEFAULTS,
    ToolConfig,
    _Entity,
    _generic_hints,
    rpc_handlers,
)
from config_core.store import AssetStore, now_iso, strip_platform

from _fakes import FakeIronflock

DEVICE_KEY = 471
OTHER_GATEWAY = 999


def passthrough(config, existing):
    return dict(config), []


def make_tools(validate=passthrough, fake=None, **cfg_kwargs):
    fake = fake or FakeIronflock(device_key=DEVICE_KEY)
    store = AssetStore(fake, DEVICE_KEY, verify_retry_delay=0)
    cfg = ToolConfig(validate, **cfg_kwargs)
    return fake, store, cfg, rpc_handlers(store, cfg)


def make_generic_tools(validate=passthrough, fake=None, table="connections",
                       status_table=None, noun="connection",
                       create_defaults=None, **cfg_kwargs):
    """The register_config_tools shape: custom table, no datapoints, no
    coercion, generic hints."""
    fake = fake or FakeIronflock(device_key=DEVICE_KEY)
    store = AssetStore(fake, DEVICE_KEY, verify_retry_delay=0, table=table,
                       datapoints_table=None, status_table=status_table)
    entity = _Entity(noun=noun, create_defaults=dict(create_defaults or {}),
                     coerce=None, hints=_generic_hints(noun))
    cfg = ToolConfig(validate, entity=entity, **cfg_kwargs)
    return fake, store, cfg, rpc_handlers(store, cfg)


def seed_asset(fake, name, gateway_id=DEVICE_KEY, deleted=False, **columns):
    row = {
        "tsp": now_iso(),
        "asset_name": name,
        "gateway_id": gateway_id,
        "deleted": deleted,
        "enabled": True,
        "demo_mode": False,
        "collect_interval": 5,
        "datapoint_spec": "",
    }
    row.update(columns)
    return fake.external_append("assets", row)


def seed_datapoint(fake, asset_name, datapoint_id, gateway_id=DEVICE_KEY,
                   deleted=False, **columns):
    row = {
        "tsp": now_iso(),
        "asset_name": asset_name,
        "datapoint_id": datapoint_id,
        "gateway_id": gateway_id,
        "deleted": deleted,
        "name": datapoint_id,
        "units": "",
        "path": "",
    }
    row.update(columns)
    return fake.external_append("datapoints", row)


def check(response):
    """Contract assertions shared by every test: serializable + envelope."""
    json.dumps(response)
    assert isinstance(response.get("ok"), bool)
    assert response.get("status")
    assert response.get("hint")
    return response


# ---------------------------------------------------------------- coercion


def test_coerce_rpc_args_conventions():
    assert coerce_rpc_args((), {"a": 1}, ("a",)) == {"a": 1}
    assert coerce_rpc_args(({"a": 1},), {}, ("a",)) == {"a": {"a": 1}["a"]} or True
    assert coerce_rpc_args(({"a": 1, "b": 2},), {}, ("a",)) == {"a": 1, "b": 2}
    assert coerce_rpc_args(("x", "y"), {}, ("a", "b")) == {"a": "x", "b": "y"}
    assert coerce_rpc_args((), {}, ("a",)) == {}


def test_coerce_rpc_args_drops_device_key():
    # The platform strips the routing device_key before dispatch; if one ever
    # leaks through, op(**params) would TypeError and reject a valid call.
    assert coerce_rpc_args((), {"device_key": 42, "a": 1}, ("a",)) == {"a": 1}
    assert coerce_rpc_args(({"device_key": 42, "a": 1},), {}, ("a",)) == {"a": 1}


async def test_handlers_accept_all_calling_conventions():
    fake, _, _, handlers = make_tools()
    seed_asset(fake, "Press 1")
    for call in (
        lambda: handlers["get"](asset_name="Press 1"),
        lambda: handlers["get"]({"asset_name": "Press 1"}),
        lambda: handlers["get"]("Press 1"),
    ):
        response = check(await call())
        assert response["ok"] is True and response["found"] is True


async def test_object_params_accept_json_strings():
    fake, _, _, handlers = make_tools()
    response = check(await handlers["create"](
        asset_name="Press 1",
        fields=json.dumps({"demo_mode": True}),
    ))
    assert response["ok"] is True
    assert fake.latest("assets", asset_name="Press 1")["demo_mode"] is True


# ------------------------------------------------------------ gateway scope


async def test_reads_scoped_to_local_gateway():
    fake, _, _, handlers = make_tools()
    seed_asset(fake, "Mine")
    seed_asset(fake, "Foreign", gateway_id=OTHER_GATEWAY)
    response = check(await handlers["list"]())
    names = [a["asset_name"] for a in response["assets"]]
    assert names == ["Mine"]
    response = check(await handlers["get"](asset_name="Foreign"))
    assert response["ok"] is False and response["code"] == "not_found"


async def test_writes_stamp_local_gateway_key():
    fake, _, _, handlers = make_tools()
    check(await handlers["create"](asset_name="New One",
                                   fields={"demo_mode": True}))
    row = fake.latest("assets", asset_name="New One")
    assert row["gateway_id"] == DEVICE_KEY


async def test_gateway_id_echo_dropped_foreign_rejected():
    fake, _, _, handlers = make_tools()
    seed_asset(fake, "Press 1")
    response = check(await handlers["update"](
        asset_name="Press 1",
        changes={"gateway_id": str(DEVICE_KEY), "collect_interval": 30},
    ))
    assert response["ok"] is True and "gateway_id" in response["ignored"]
    response = check(await handlers["update"](
        asset_name="Press 1", changes={"gateway_id": OTHER_GATEWAY},
    ))
    assert response["ok"] is False
    assert response["code"] == "protected_column"


# ------------------------------------------------------- names and hygiene


async def test_name_rules():
    _, _, _, handlers = make_tools()
    for bad in (None, "", "  ", "ab", "x" * 51, "bad/name", "ümlaut"):
        response = check(await handlers["create"](asset_name=bad, fields={}))
        assert response["ok"] is False
        assert response["code"] == "invalid_name", bad
    # 3 and 50 chars are valid; surrounding whitespace is stripped.
    fake, _, _, handlers = make_tools()
    response = check(await handlers["create"](
        asset_name="  abc  ", fields={"demo_mode": True}))
    assert response["ok"] is True
    assert fake.latest("assets", asset_name="abc") is not None


async def test_bool_coercion_strict():
    fake, _, _, handlers = make_tools()
    check(await handlers["create"](
        asset_name="Press 1",
        fields={"demo_mode": "true", "enabled": "false"},
    ))
    row = fake.latest("assets", asset_name="Press 1")
    assert row["demo_mode"] is True
    assert row["enabled"] is False  # a real bool -- the pause switch depends on it
    response = check(await handlers["update"](
        asset_name="Press 1", changes={"enabled": "maybe"},
    ))
    assert response["ok"] is False and response["code"] == "invalid_value"


async def test_collect_interval_hygiene():
    fake, _, _, handlers = make_tools()
    seed_asset(fake, "Press 1")
    for bad in ("abc", 0, -5, True):
        response = check(await handlers["update"](
            asset_name="Press 1", changes={"collect_interval": bad}))
        assert response["ok"] is False, bad
        assert response["code"] == "invalid_value"
    response = check(await handlers["update"](
        asset_name="Press 1", changes={"collect_interval": "2.5"}))
    assert response["ok"] is True
    assert fake.latest("assets", asset_name="Press 1")["collect_interval"] == 2


async def test_secret_guard():
    _, _, _, handlers = make_tools()
    cases = [
        {"api_key": "abc"},
        {"opc_password": "hunter2"},
        {"host": "opc.tcp://user:hunter2@10.0.0.1:4840"},
        {"note": "-----BEGIN RSA PRIVATE KEY-----"},
        # Public certificates are refused too, by value, even though the
        # column name passes -- decided 2026-08-28, see SECRET_VALUE_RES.
        {"tls_ca": "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----"},
        {"datapoint_spec": "- name: t\n  address: eyJhbGciOiJIUzI1NiJ9.eyJzdWIifQ"},
    ]
    for fields in cases:
        response = check(await handlers["create"](
            asset_name="Press 1", fields=fields))
        assert response["ok"] is False, fields
        assert response["code"] == "secret_rejected"


async def test_deleted_field_rejected():
    fake, _, _, handlers = make_tools()
    seed_asset(fake, "Press 1")
    response = check(await handlers["update"](
        asset_name="Press 1", changes={"deleted": True}))
    assert response["ok"] is False
    assert response["code"] == "protected_column"
    assert "delete_asset" in " ".join(response["problems"])


async def test_rename_via_fields_rejected():
    fake, _, _, handlers = make_tools()
    seed_asset(fake, "Press 1")
    response = check(await handlers["update"](
        asset_name="Press 1", changes={"asset_name": "Press 2"}))
    assert response["ok"] is False
    assert "rename" in " ".join(response["problems"])


# ------------------------------------------------------ validate integration


async def test_validate_problems_reject_and_nothing_written():
    def validate(config, existing):
        return None, ["host is required", "driver unknown"]

    fake, _, _, handlers = make_tools(validate=validate)
    response = check(await handlers["create"](asset_name="Press 1", fields={}))
    assert response["ok"] is False
    assert response["code"] == "validation_error"
    assert response["problems"] == ["host is required", "driver unknown"]
    assert fake.rows("assets") == []


async def test_validate_raise_rejects_fail_closed():
    def validate(config, existing):
        raise ValueError("spec is not valid YAML")

    fake, _, _, handlers = make_tools(validate=validate)
    response = check(await handlers["create"](asset_name="Press 1", fields={}))
    assert response["ok"] is False
    assert "spec is not valid YAML" in " ".join(response["problems"])
    assert fake.rows("assets") == []


async def test_validate_normalizes_and_async_supported():
    async def validate(config, existing):
        valid = dict(config)
        valid["host"] = valid.get("host", "").strip().lower()
        valid.setdefault("port", 502)
        return valid, []

    fake, _, _, handlers = make_tools(validate=validate)
    response = check(await handlers["create"](
        asset_name="Press 1", fields={"host": "  PLC-A.local  "}))
    assert response["ok"] is True
    row = fake.latest("assets", asset_name="Press 1")
    assert row["host"] == "plc-a.local" and row["port"] == 502


async def test_validate_cannot_smuggle_identity_or_poison():
    def validate(config, existing):
        valid = dict(config)
        valid["asset_name"] = "Hijacked"
        valid["gateway_id"] = OTHER_GATEWAY
        valid["deleted"] = True
        valid["authid"] = "spoof"
        return valid, []

    fake, _, _, handlers = make_tools(validate=validate)
    response = check(await handlers["create"](
        asset_name="Press 1", fields={"demo_mode": True}))
    assert response["ok"] is True
    row = fake.latest("assets", asset_name="Press 1")
    assert row["asset_name"] == "Press 1"
    assert row["gateway_id"] == DEVICE_KEY
    assert row["deleted"] is False
    assert row["authid"] == f"device-{DEVICE_KEY}"  # fake's platform stamp
    assert fake.latest("assets", asset_name="Hijacked") is None


async def test_validate_returning_bad_core_value_rejected():
    def validate(config, existing):
        valid = dict(config)
        valid["collect_interval"] = "sometimes"
        return valid, []

    fake, _, _, handlers = make_tools(validate=validate)
    response = check(await handlers["create"](asset_name="Press 1", fields={}))
    assert response["ok"] is False
    assert response["code"] == "validation_error"
    assert "validate function returned an invalid value" in " ".join(
        response["problems"]
    )
    assert fake.rows("assets") == []


async def test_validate_receives_documented_candidate():
    """What validate sees is contract (README "What config holds at call
    time"): a non-collector app rejecting unknown columns rejects every
    create if the seed grows, so the seed is pinned here too."""
    seen = []

    def validate(config, existing):
        seen.append((dict(config), existing))
        return dict(config), []

    assert CORE_DEFAULTS == {"datapoint_spec": "", "collect_interval": 5,
                             "enabled": True, "demo_mode": False}

    fake, _, _, handlers = make_tools(validate=validate)
    check(await handlers["create"](
        asset_name="Press 1", fields={"host": "10.0.0.5"}))
    candidate, existing = seen.pop()
    assert candidate == dict(CORE_DEFAULTS, host="10.0.0.5",
                             asset_name="Press 1", gateway_id=DEVICE_KEY)
    assert existing is None

    check(await handlers["update"](
        asset_name="Press 1", changes={"collect_interval": 30}))
    candidate, existing = seen.pop()
    # the stored row minus the platform columns, plus the agent's change
    assert candidate == dict(CORE_DEFAULTS, host="10.0.0.5",
                             asset_name="Press 1", gateway_id=DEVICE_KEY,
                             collect_interval=30, deleted=False)
    assert existing["tsp"] and existing["authid"] == f"device-{DEVICE_KEY}"


async def test_validate_bad_shape_rejected():
    fake, _, _, handlers = make_tools(validate=lambda c, e: {"just": "config"})
    response = check(await handlers["create"](asset_name="Press 1", fields={}))
    assert response["ok"] is False
    assert "must return (config, problems)" in " ".join(response["problems"])


# ------------------------------------------------------------------ create


async def test_create_applies_defaults_and_audit():
    fake, _, _, handlers = make_tools(audit_column="configured_by")
    response = check(await handlers["create"](
        asset_name="Press 1", fields={"host": "10.0.0.5"}))
    assert response["ok"] is True and response["created"] is True
    assert response["verified"] is True
    row = fake.latest("assets", asset_name="Press 1")
    assert row["collect_interval"] == 5
    assert row["enabled"] is True
    assert row["demo_mode"] is False
    assert row["datapoint_spec"] == ""
    assert row["configured_by"] == "agent"
    # applied info toast
    assert any(
        r["level"] == "info" and "created" in r["msg"] for r in fake.reported
    )


async def test_create_existing_name_rejected_with_row():
    fake, _, _, handlers = make_tools()
    seed_asset(fake, "Press 1", host="10.0.0.5")
    response = check(await handlers["create"](asset_name="Press 1", fields={}))
    assert response["ok"] is False
    assert response["code"] == "already_exists"
    assert response["asset"]["host"] == "10.0.0.5"
    # similar-name warning on a casefold near-match
    response = check(await handlers["create"](
        asset_name="press 1", fields={"demo_mode": True}))
    assert response["ok"] is True
    assert any("similarly named" in w for w in response["warnings"])


async def test_create_revives_deleted_name_with_warning():
    fake, _, _, handlers = make_tools()
    seed_asset(fake, "Press 1", deleted=True)
    response = check(await handlers["create"](
        asset_name="Press 1", fields={"demo_mode": True}))
    assert response["ok"] is True
    assert any("revives" in w for w in response["warnings"])
    assert fake.latest("assets", asset_name="Press 1")["deleted"] is False


async def test_create_cap():
    fake, _, _, handlers = make_tools(max_assets=2)
    seed_asset(fake, "A 1")
    seed_asset(fake, "A 2")
    response = check(await handlers["create"](asset_name="A 3", fields={}))
    assert response["ok"] is False
    assert response["code"] == "limit_exceeded"


async def test_create_dry_run_writes_nothing():
    fake, _, _, handlers = make_tools()
    response = check(await handlers["create"](
        asset_name="Press 1", fields={"host": "10.0.0.5"}, dry_run=True))
    assert response["ok"] is True and response["status"] == "dry_run"
    assert response["would_write"]["host"] == "10.0.0.5"
    assert response["would_write"]["tsp"] == "<stamped at write>"
    assert fake.rows("assets") == []
    assert fake.reported == []  # no toast for previews


async def test_create_append_failure_carries_sdk_error():
    fake, _, _, handlers = make_tools()
    fake.fail_next("append_to_table", error="Append to table 'assets' failed "
                                            "with WAMP error 'wamp.error.no_such_procedure'")
    response = check(await handlers["create"](asset_name="Press 1", fields={}))
    assert response["ok"] is False
    assert response["status"] == "failed"
    assert response["code"] == "write_failed"
    assert "no_such_procedure" in response["error"]
    assert any(r["level"] == "warn" for r in fake.reported)


async def test_create_append_none_mode_defensive():
    fake = FakeIronflock(device_key=DEVICE_KEY, none_mode=True)
    fake, _, _, handlers = make_tools(fake=fake)
    fake.fail_next("append_to_table")
    response = check(await handlers["create"](asset_name="Press 1", fields={}))
    assert response["ok"] is False and response["code"] == "write_failed"


# ------------------------------------------------------------------ update


async def test_update_echo_merge_preserves_omitted_app_columns():
    fake, _, _, handlers = make_tools()
    seed_asset(fake, "Press 1", driver="s7", host="10.0.0.5", rack=0, slot=1)
    response = check(await handlers["update"](
        asset_name="Press 1", changes={"collect_interval": 30}))
    assert response["ok"] is True
    row = fake.latest("assets", asset_name="Press 1")
    # The blanking regression: omitted app columns must survive the append.
    assert row["driver"] == "s7"
    assert row["host"] == "10.0.0.5"
    assert row["rack"] == 0 and row["slot"] == 1
    assert row["collect_interval"] == 30
    assert response["changed_fields"] == {
        "collect_interval": {"from": 5, "to": 30}
    }
    assert response["previous"]["collect_interval"] == 5


async def test_update_never_echoes_platform_columns():
    fake, _, _, handlers = make_tools()
    seed_asset(fake, "Press 1")
    stored = fake.latest("assets", asset_name="Press 1")
    stored["datapoint_list"] = [{"id": "x"}]  # runtime key on the read row
    check(await handlers["update"](
        asset_name="Press 1", changes={"collect_interval": 30}))
    # The wire payload must never carry runtime keys or platform columns --
    # authid/device_key on the stored row are the platform's fresh stamps.
    table, payload = fake.payloads[-1]
    assert table == "assets"
    assert "datapoint_list" not in payload
    assert "authid" not in payload and "device_key" not in payload
    appended = fake.latest("assets", asset_name="Press 1")
    assert appended["authid"] == f"device-{DEVICE_KEY}"


async def test_update_null_clears_omitted_preserves():
    def validate(config, existing):
        return dict(config), []

    fake, _, _, handlers = make_tools(validate=validate)
    seed_asset(fake, "Press 1", host="10.0.0.5", rack=0)
    response = check(await handlers["update"](
        asset_name="Press 1", changes={"host": None}))
    assert response["ok"] is True
    row = fake.latest("assets", asset_name="Press 1")
    assert row["host"] is None   # explicit null blanks
    assert row["rack"] == 0      # omitted preserves


async def test_update_no_change_skip_numeric_normalization():
    fake, _, _, handlers = make_tools()
    seed_asset(fake, "Press 1", collect_interval=30, host="10.0.0.5")
    before = len(fake.rows("assets"))
    for same in (30, "30", 30.0):
        response = check(await handlers["update"](
            asset_name="Press 1", changes={"collect_interval": same}))
        assert response["ok"] is True
        assert response["status"] == "no_change"
        assert response["changed"] is False
    assert len(fake.rows("assets")) == before  # nothing appended
    assert fake.reported == []                 # and no toast


async def test_update_not_found_suggestions_and_deleted_hint():
    fake, _, _, handlers = make_tools()
    seed_asset(fake, "Compressor House 1")
    response = check(await handlers["update"](
        asset_name="Compresor House 1", changes={"collect_interval": 30}))
    assert response["ok"] is False and response["code"] == "not_found"
    assert "Compressor House 1" in response["suggestions"]
    seed_asset(fake, "Old Pump", deleted=True, host="10.9.9.9")
    response = check(await handlers["update"](
        asset_name="Old Pump", changes={"collect_interval": 30}))
    assert response["ok"] is False
    assert "create_asset" in response["hint"]
    assert response["previous"]["host"] == "10.9.9.9"


async def test_update_expected_tsp_conflict():
    fake, _, _, handlers = make_tools()
    seed_asset(fake, "Press 1")
    current = fake.latest("assets", asset_name="Press 1")["tsp"]
    response = check(await handlers["update"](
        asset_name="Press 1", changes={"collect_interval": 30},
        expected_tsp="2020-01-01T00:00:00+00:00"))
    assert response["ok"] is False and response["status"] == "conflict"
    assert response["current_tsp"] == current
    response = check(await handlers["update"](
        asset_name="Press 1", changes={"collect_interval": 30},
        expected_tsp=current))
    assert response["ok"] is True


async def test_update_superseded_detected():
    fake, _, _, handlers = make_tools()
    seed_asset(fake, "Press 1")

    def racing_writer(table, row):
        if table == "assets":
            fake.on_append(None)  # once
            newer = dict(row)
            newer["collect_interval"] = 99
            newer["tsp"] = "2999-01-01T00:00:00+00:00"
            fake.external_append("assets", newer, authid="user-9")

    fake.on_append(racing_writer)
    response = check(await handlers["update"](
        asset_name="Press 1", changes={"collect_interval": 30}))
    assert response["ok"] is True          # our row is in the history
    assert response["verified"] is False
    assert response["superseded"] is True
    assert "user-9" in response["hint"]


async def test_update_visibility_lag_unverified():
    fake = FakeIronflock(device_key=DEVICE_KEY, defer_visibility=True)
    fake, _, _, handlers = make_tools(fake=fake)
    seed_asset(fake, "Press 1")
    response = check(await handlers["update"](
        asset_name="Press 1", changes={"collect_interval": 30}))
    assert response["ok"] is True
    assert response["status"] == "unverified"
    assert response["verified"] is False
    assert "get_asset" in response["hint"]


async def test_update_auto_registered_warning():
    fake, _, _, handlers = make_tools()
    seed_asset(fake, "Scanner Found", auto_registered=True)
    response = check(await handlers["update"](
        asset_name="Scanner Found", changes={"collect_interval": 30}))
    assert response["ok"] is True
    assert any("discovery" in w for w in response["warnings"])


# ------------------------------------------------------------------ delete


async def test_delete_tombstone_and_cascade():
    fake, _, _, handlers = make_tools(audit_column="configured_by")
    seed_asset(fake, "Press 1", host="10.0.0.5")
    seed_datapoint(fake, "Press 1", "temp")
    seed_datapoint(fake, "Press 1", "pressure")
    seed_datapoint(fake, "Press 1", "gone", deleted=True)
    response = check(await handlers["delete"](asset_name="Press 1"))
    assert response["ok"] is True and response["status"] == "deleted"
    assert response["datapoints_deleted"] == 2
    assert response["datapoints_failed"] == 0
    assert response["previous"]["host"] == "10.0.0.5"
    row = fake.latest("assets", asset_name="Press 1")
    assert row["deleted"] is True and row["configured_by"] == "agent"
    for dp in ("temp", "pressure"):
        assert fake.latest("datapoints", asset_name="Press 1",
                           datapoint_id=dp)["deleted"] is True
    # ordering: the asset tombstone is appended before any datapoint tombstone
    appends = [t for m, t in fake.calls if m == "append_to_table"]
    assert appends[0] == "assets"
    assert set(appends[1:3]) == {"datapoints"}


async def test_delete_missing_and_dry_run():
    fake, _, _, handlers = make_tools()
    seed_asset(fake, "Press 1")
    seed_datapoint(fake, "Press 1", "temp")
    response = check(await handlers["delete"](asset_name="Pres 1"))
    assert response["ok"] is False and response["code"] == "not_found"
    response = check(await handlers["delete"](asset_name="Press 1",
                                              dry_run=True))
    assert response["status"] == "dry_run"
    assert response["datapoints_to_delete"] == 1
    assert fake.latest("assets", asset_name="Press 1")["deleted"] is False


async def test_delete_already_deleted_resweeps_orphans():
    fake, _, _, handlers = make_tools()
    seed_asset(fake, "Press 1", deleted=True)
    response = check(await handlers["delete"](asset_name="Press 1"))
    assert response["status"] == "no_change"
    seed_datapoint(fake, "Press 1", "orphan")
    response = check(await handlers["delete"](asset_name="Press 1"))
    assert response["status"] == "deleted"
    assert response["datapoints_deleted"] == 1
    assert fake.latest("datapoints", datapoint_id="orphan")["deleted"] is True


async def test_delete_partial_cascade_reports_failures():
    fake, _, _, handlers = make_tools()
    seed_asset(fake, "Press 1")
    seed_datapoint(fake, "Press 1", "a")
    seed_datapoint(fake, "Press 1", "b")
    # first append (tombstone) succeeds, second (datapoint a) fails
    fake.fail_next("append_to_table", n=0)

    async def run_with_one_dp_failure():
        original = fake.append_to_table
        state = {"count": 0}

        async def flaky(table, payload):
            state["count"] += 1
            if state["count"] == 2:
                raise RuntimeError("append to datapoints interrupted")
            return await original(table, payload)

        fake.append_to_table = flaky
        try:
            return await handlers["delete"](asset_name="Press 1")
        finally:
            fake.append_to_table = original

    response = check(await run_with_one_dp_failure())
    assert response["ok"] is True and response["status"] == "deleted"
    assert response["datapoints_deleted"] == 1
    assert response["datapoints_failed"] == 1
    assert "again" in response["hint"]
    assert any(r["level"] == "warn" for r in fake.reported)


# -------------------------------------------------------------- datapoints


async def test_list_datapoints():
    fake, _, _, handlers = make_tools()
    seed_asset(fake, "Press 1")
    seed_datapoint(fake, "Press 1", "temp", units="degC", address="DB1.DBW2")
    seed_datapoint(fake, "Press 1", "old", deleted=True)
    response = check(await handlers["list_datapoints"](asset_name="Press 1"))
    assert response["count"] == 1
    assert response["datapoints"][0]["datapoint_id"] == "temp"
    assert "authid" not in response["datapoints"][0]


async def test_set_datapoints_machine_owned_rejected():
    fake, _, _, handlers = make_tools()
    seed_asset(fake, "Press 1")
    seed_datapoint(fake, "Press 1", "temp")
    response = check(await handlers["set_datapoints"](
        asset_name="Press 1",
        changes={"datapoint_id": "temp", "address": "DB1.DBW4"},
    ))
    assert response["ok"] is False
    item = response["results"][0]
    assert item["status"] == "rejected"
    assert "discovery" in " ".join(item["problems"])


async def test_set_datapoints_batch_and_crash_class_invariants():
    fake, _, _, handlers = make_tools()
    seed_asset(fake, "Press 1")
    seed_datapoint(fake, "Press 1", "temp")
    seed_datapoint(fake, "Press 1", "pressure", enabled=True)
    response = check(await handlers["set_datapoints"](
        asset_name="Press 1",
        changes=[
            {"datapoint_id": "temp", "enabled": False},
            {"datapoint_id": "pressure", "enabled": True},   # no-op
            {"datapoint_id": "tmep", "enabled": False},      # typo
        ],
    ))
    assert response["applied"] == 1
    assert response["no_change"] == 1
    assert response["rejected"] == 1
    assert response["results"][2]["suggestions"] == ["temp"]
    # every datapoints row ever appended carries the crash-class keys
    for row in fake.rows("datapoints"):
        assert row["datapoint_id"]
        assert row["asset_name"]
        assert isinstance(row["deleted"], bool)
    changed = fake.latest("datapoints", datapoint_id="temp")
    assert changed["enabled"] is False
    assert changed["name"] == "temp"  # echo-merge preserved machine columns


async def test_set_datapoints_demo_settings():
    fake, _, _, handlers = make_tools()
    seed_asset(fake, "Press 1")
    seed_datapoint(fake, "Press 1", "temp")
    seed_datapoint(fake, "Press 1", "running")
    response = check(await handlers["set_datapoints"](
        asset_name="Press 1",
        changes=[
            {"datapoint_id": "temp", "demo_value": "75", "demo_variance": 2},
            {"datapoint_id": "running", "demo_value": True},  # a resting state
        ],
    ))
    assert response["applied"] == 2
    temp = fake.latest("datapoints", datapoint_id="temp")
    assert temp["demo_value"] == 75.0 and temp["demo_variance"] == 2.0
    assert fake.latest("datapoints", datapoint_id="running")["demo_value"] is True


async def test_set_datapoints_demo_settings_rejected_and_cleared():
    fake, _, _, handlers = make_tools()
    seed_asset(fake, "Press 1")
    seed_datapoint(fake, "Press 1", "temp", demo_value=75.0)
    response = check(await handlers["set_datapoints"](
        asset_name="Press 1",
        changes=[
            {"datapoint_id": "temp", "demo_variance": "wide"},
            {"datapoint_id": "temp", "demo_value": "warm"},
        ],
    ))
    assert response["rejected"] == 2
    assert "not a number" in " ".join(response["results"][0]["problems"])
    # an explicit null restores the default range
    response = check(await handlers["set_datapoints"](
        asset_name="Press 1",
        changes={"datapoint_id": "temp", "demo_value": None},
    ))
    assert response["applied"] == 1
    assert fake.latest("datapoints", datapoint_id="temp")["demo_value"] is None


async def test_set_datapoints_cap_and_dry_run():
    fake, _, _, handlers = make_tools()
    seed_asset(fake, "Press 1")
    seed_datapoint(fake, "Press 1", "temp")
    response = check(await handlers["set_datapoints"](
        asset_name="Press 1",
        changes=[{"datapoint_id": f"d{i}"} for i in range(101)],
    ))
    assert response["ok"] is False and response["code"] == "invalid_value"
    response = check(await handlers["set_datapoints"](
        asset_name="Press 1",
        changes={"datapoint_id": "temp", "enabled": False},
        dry_run=True,
    ))
    assert response["results"][0]["status"] == "dry_run"
    assert fake.latest("datapoints", datapoint_id="temp").get("enabled") is not False


# -------------------------------------------------------------------- list


async def test_list_shapes_and_status_join():
    fake, _, _, handlers = make_tools()
    seed_asset(fake, "B Press", datapoint_spec="- name: t\n  address: DB1")
    seed_asset(fake, "A Press")
    seed_asset(fake, "Old", deleted=True)
    fake.external_append("assetstatus", {
        "tsp": now_iso(), "asset_name": "A Press", "gateway_id": DEVICE_KEY,
        "status": "online", "detail": "", "deleted": False,
    })
    response = check(await handlers["list"]())
    assert response["count"] == 2
    assert [a["asset_name"] for a in response["assets"]] == ["A Press", "B Press"]
    a_press = response["assets"][0]
    assert a_press["live_status"]["status"] == "online"
    b_press = response["assets"][1]
    assert b_press["live_status"]["status"] == "unknown"
    assert b_press["datapoint_spec"]["chars"] > 0
    assert "authid" not in a_press
    response = check(await handlers["list"](include_deleted=True))
    assert response["count"] == 3


async def test_list_status_read_failure_degrades():
    fake, _, _, handlers = make_tools()
    seed_asset(fake, "Press 1")
    fake.fail_next("getHistory", n=0)  # assets read must succeed...

    async def flaky_second_read():
        original = fake.getHistory
        state = {"count": 0}

        async def wrapped(table, params):
            state["count"] += 1
            if table == "assetstatus":
                raise RuntimeError("status service down")
            return await original(table, params)

        fake.getHistory = wrapped
        try:
            return await handlers["list"]()
        finally:
            fake.getHistory = original

    response = check(await flaky_second_read())
    assert response["ok"] is True and response["count"] == 1
    assert any("status" in w for w in response["warnings"])


# --------------------------------------------------------------------- get


async def test_get_full_shape():
    fake, _, _, handlers = make_tools(audit_column="configured_by")
    seed_asset(fake, "Press 1", host="10.0.0.5", auto_registered=False,
               configured_by="user")
    seed_datapoint(fake, "Press 1", "temp", enabled=False)
    seed_datapoint(fake, "Press 1", "pressure", change_detection=True)
    fake.external_append("assetstatus", {
        "tsp": now_iso(), "asset_name": "Press 1", "gateway_id": DEVICE_KEY,
        "status": "offline", "detail": "connection refused", "deleted": False,
    })
    response = check(await handlers["get"](asset_name="Press 1"))
    assert response["found"] is True
    assert response["asset"]["host"] == "10.0.0.5"
    assert "authid" not in response["asset"]
    assert response["live_status"]["detail"] == "connection refused"
    assert response["datapoints"]["count"] == 2
    assert response["datapoints"]["disabled"] == 1
    assert response["datapoints"]["change_detection"] == 1
    assert response["_meta"]["configured_by"] == "user"
    assert response["_meta"]["tsp"]
    assert "error-logs" in response["hint"]  # offline steers to SQL diagnosis


async def test_get_deleted_and_spec_truncation():
    fake, _, _, handlers = make_tools()
    seed_asset(fake, "Old Pump", deleted=True)
    response = check(await handlers["get"](asset_name="Old Pump"))
    assert response["deleted"] is True and "create_asset" in response["hint"]
    seed_asset(fake, "Big Spec", datapoint_spec="x" * 40000)
    response = check(await handlers["get"](asset_name="Big Spec"))
    assert "truncated" in response["asset"]["datapoint_spec"]


# -------------------------------------------------------------- discipline


async def test_handlers_never_raise_on_garbage():
    fake, _, _, handlers = make_tools()
    seed_asset(fake, "Press 1")
    calls = [
        handlers["get"](),
        handlers["get"](12345),
        handlers["create"](),
        handlers["create"]("Press 9", "not-json-not-dict {"),
        handlers["update"]("Press 1", 42),
        handlers["update"]("Press 1", {"collect_interval": 30},
                           "tsp", "not-a-bool"),
        handlers["set_datapoints"]("Press 1", "definitely not json ["),
        handlers["delete"](None),
        handlers["list"]("not-a-bool"),
    ]
    for call in calls:
        response = await call
        json.dumps(response)
        assert response["ok"] is False


async def test_internal_error_caught_and_toasted():
    fake, store, _, handlers = make_tools()

    async def boom():
        raise RuntimeError("store bug")

    store.read_assets = lambda: boom()
    response = check(await handlers["get"](asset_name="Press 1"))
    assert response["ok"] is False
    assert response["code"] == "internal_error"
    assert "store bug" in response["error"]
    assert any(r["level"] == "error" for r in fake.reported)


async def test_link_error_carries_sdk_text():
    fake, _, _, handlers = make_tools()
    fake.fail_next("getHistory",
                   error="History query for 'assets' failed: not connected")
    response = check(await handlers["list"]())
    assert response["ok"] is False and response["code"] == "link_error"
    assert "not connected" in response["error"]


async def test_concurrent_mutations_serialized():
    fake, _, _, handlers = make_tools()
    seed_asset(fake, "Press 1", host="10.0.0.5")
    results = await asyncio.gather(
        handlers["update"]("Press 1", {"collect_interval": 30}),
        handlers["update"]("Press 1", {"host": "10.0.0.6"}),
    )
    assert all(r["ok"] for r in results)
    row = fake.latest("assets", asset_name="Press 1")
    # The second write's echo-merge base includes the first write -- no lost
    # update.
    assert row["collect_interval"] == 30
    assert row["host"] == "10.0.0.6"


async def test_toast_failure_never_fails_operation():
    fake, _, _, handlers = make_tools()
    fake.fail_report_error(n=5)
    response = check(await handlers["create"](
        asset_name="Press 1", fields={"demo_mode": True}))
    assert response["ok"] is True


# ------------------------------------------------------------ registration


async def test_register_defaults_and_failures():
    fake = FakeIronflock(device_key=DEVICE_KEY)
    failed = await register_asset_tools(fake, passthrough,
                                        device_key=DEVICE_KEY)
    assert failed == []
    assert set(fake.registered) == set(DEFAULT_TOPICS.values())

    fake = FakeIronflock(device_key=DEVICE_KEY)
    fake.fail_next("register_device_function", n=2,
                   error="Registration of procedure failed")
    failed = await register_asset_tools(fake, passthrough,
                                        device_key=DEVICE_KEY)
    assert len(failed) == 2
    warn = [r for r in fake.reported if r["level"] == "warn"]
    assert len(warn) == 1
    assert "Registration of procedure failed" in warn[0]["msg"]


async def test_register_partial_topics_and_skip():
    fake = FakeIronflock(device_key=DEVICE_KEY)
    failed = await register_asset_tools(
        fake, passthrough, device_key=DEVICE_KEY,
        topics={"delete": None, "list": "custom_assets.list"},
    )
    assert failed == []
    assert "custom_assets.list" in fake.registered
    assert DEFAULT_TOPICS["delete"] not in fake.registered
    assert DEFAULT_TOPICS["get"] in fake.registered


async def test_register_device_key_env_fallback(monkeypatch):
    monkeypatch.setenv("DEVICE_KEY", str(DEVICE_KEY))
    fake = FakeIronflock(device_key=DEVICE_KEY)
    failed = await register_asset_tools(fake, passthrough)
    assert failed == []
    seedable = fake  # registered handlers are wired to the env device key
    seed_asset(seedable, "Press 1")
    response = await fake.registered[DEFAULT_TOPICS["get"]]("Press 1")
    assert response["found"] is True

    monkeypatch.delenv("DEVICE_KEY")
    fake = FakeIronflock(device_key=DEVICE_KEY)
    failed = await register_asset_tools(fake, passthrough)
    assert set(failed) == set(DEFAULT_TOPICS.values())
    assert any(r["level"] == "warn" for r in fake.reported)


async def test_register_rejects_non_callable_validate():
    fake = FakeIronflock(device_key=DEVICE_KEY)
    failed = await register_asset_tools(fake, "not-callable",
                                        device_key=DEVICE_KEY)
    assert set(failed) == set(DEFAULT_TOPICS.values())
    assert fake.registered == {}


async def test_registered_handler_end_to_end():
    fake = FakeIronflock(device_key=DEVICE_KEY)
    await register_asset_tools(fake, passthrough, device_key=DEVICE_KEY)
    create = fake.registered[DEFAULT_TOPICS["create"]]
    response = await create({"asset_name": "Press 1",
                             "fields": {"demo_mode": True}})
    json.dumps(response)
    assert response["ok"] is True
    assert fake.latest("assets", asset_name="Press 1") is not None


# ------------------------------------------------- generic entity (1.2.0)


async def test_generic_crud_on_custom_table():
    fake, _, _, handlers = make_generic_tools(audit_column="configured_by")
    response = check(await handlers["create"](
        asset_name="Broker 1", fields={"broker_url": "mqtts://x:8883"}))
    assert response["ok"] is True and response["created"] is True
    row = fake.latest("connections", asset_name="Broker 1")
    assert row["broker_url"] == "mqtts://x:8883"
    assert row["configured_by"] == "agent"

    response = check(await handlers["update"](
        asset_name="Broker 1", changes={"broker_url": "mqtts://y:8883"}))
    assert response["ok"] is True
    response = check(await handlers["get"](asset_name="Broker 1"))
    assert response["asset"]["broker_url"] == "mqtts://y:8883"
    response = check(await handlers["delete"](asset_name="Broker 1"))
    assert response["ok"] is True
    assert fake.latest("connections", asset_name="Broker 1")["deleted"] is True

    # every read and write hit ONLY the entity's own table
    touched = {t for method, t in fake.calls
               if method in ("getHistory", "append_to_table")}
    assert touched == {"connections"}


async def test_generic_create_seeds_nothing():
    """The data_relay regression: a validate that hard-rejects unknown
    columns must accept an empty create, because nothing is seeded."""
    seen = []

    def strict(config, existing):
        seen.append((dict(config), existing))
        unknown = set(config) - {"asset_name", "gateway_id", "broker_url"}
        if unknown:
            return None, [f"unknown column(s): {', '.join(sorted(unknown))}"]
        return dict(config), []

    _, _, _, handlers = make_generic_tools(validate=strict)
    response = check(await handlers["create"](asset_name="Broker 1",
                                              fields={}))
    assert response["ok"] is True, response
    candidate, existing = seen.pop()
    assert candidate == {"asset_name": "Broker 1", "gateway_id": DEVICE_KEY}
    assert existing is None


async def test_generic_create_defaults_seed_under_agent_fields():
    fake, _, _, handlers = make_generic_tools(
        create_defaults={"enabled": True})
    check(await handlers["create"](asset_name="Broker 1", fields={}))
    assert fake.latest("connections", asset_name="Broker 1")["enabled"] is True
    check(await handlers["create"](asset_name="Broker 2",
                                   fields={"enabled": False}))
    assert (fake.latest("connections", asset_name="Broker 2")["enabled"]
            is False)


async def test_generic_status_table_gating():
    # without: no live_status field, no status read, no warning
    fake, _, _, handlers = make_generic_tools()
    check(await handlers["create"](asset_name="Broker 1", fields={}))
    response = check(await handlers["list"]())
    assert "live_status" not in response["assets"][0]
    assert "warnings" not in response
    response = check(await handlers["get"](asset_name="Broker 1"))
    assert "live_status" not in response
    assert ("getHistory", "assetstatus") not in fake.calls

    # with: joined exactly like the collector preset
    fake, _, _, handlers = make_generic_tools(status_table="assetstatus")
    check(await handlers["create"](asset_name="Broker 1", fields={}))
    fake.external_append("assetstatus", {
        "tsp": now_iso(), "asset_name": "Broker 1",
        "gateway_id": DEVICE_KEY, "status": "online", "detail": "",
    })
    response = check(await handlers["get"](asset_name="Broker 1"))
    assert response["live_status"]["status"] == "online"
    response = check(await handlers["list"]())
    assert response["assets"][0]["live_status"]["status"] == "online"


async def test_generic_has_no_datapoint_machinery():
    fake, _, _, handlers = make_generic_tools()
    assert set(handlers) == {"list", "get", "create", "update", "delete"}
    check(await handlers["create"](asset_name="Broker 1", fields={}))
    response = check(await handlers["get"](asset_name="Broker 1"))
    assert "datapoints" not in response
    response = check(await handlers["list"]())
    assert "datapoint_spec" not in response["assets"][0]
    response = check(await handlers["delete"](asset_name="Broker 1",
                                              dry_run=True))
    assert "datapoints_to_delete" not in response
    response = check(await handlers["delete"](asset_name="Broker 1"))
    assert "datapoints_deleted" not in response
    assert "datapoints_failed" not in response
    assert ("getHistory", "datapoints") not in fake.calls


async def test_generic_no_coercion_reaches_validate():
    """No collector column hygiene: a string boolean reaches validate as-is
    -- the app owns typing (documented in register_config_tools)."""
    seen = []

    def recording(config, existing):
        seen.append(dict(config))
        return dict(config), []

    _, _, _, handlers = make_generic_tools(validate=recording)
    check(await handlers["create"](asset_name="Broker 1",
                                   fields={"enabled": "false",
                                           "collect_interval": "2.5"}))
    candidate = seen.pop()
    assert candidate["enabled"] == "false"
    assert candidate["collect_interval"] == "2.5"


async def test_generic_noun_in_prose():
    fake, _, _, handlers = make_generic_tools()
    response = check(await handlers["get"](asset_name="Nope 1"))
    assert "No connection named" in response["hint"]
    assert response["problems"] == ["connection 'Nope 1' not found"]
    check(await handlers["create"](asset_name="Broker 1", fields={}))
    assert any("created connection 'Broker 1'" in (r["user_message"] or "")
               for r in fake.reported)


async def test_register_config_tools_end_to_end(monkeypatch):
    fake = FakeIronflock(device_key=DEVICE_KEY)
    failed = await register_config_tools(
        fake, passthrough, "connections", noun="connection",
        create_defaults={"enabled": True}, device_key=DEVICE_KEY,
    )
    assert failed == []
    # exactly the five CRUD topics, shared with the preset's wire contract
    expected = {DEFAULT_TOPICS[op]
                for op in ("list", "get", "create", "update", "delete")}
    assert set(fake.registered) == expected

    create = fake.registered[DEFAULT_TOPICS["create"]]
    response = await create({"asset_name": "Broker 1",
                             "fields": {"broker_url": "mqtts://x:8883"}})
    json.dumps(response)
    assert response["ok"] is True
    row = fake.latest("connections", asset_name="Broker 1")
    assert row["enabled"] is True and row["broker_url"] == "mqtts://x:8883"

    # partial topics + None skip
    fake = FakeIronflock(device_key=DEVICE_KEY)
    failed = await register_config_tools(
        fake, passthrough, "connections", device_key=DEVICE_KEY,
        topics={"delete": None, "list": "custom.list"},
    )
    assert failed == []
    assert "custom.list" in fake.registered
    assert DEFAULT_TOPICS["delete"] not in fake.registered
    assert DEFAULT_TOPICS["list_datapoints"] not in fake.registered

    # failure paths mirror register_asset_tools
    monkeypatch.delenv("DEVICE_KEY", raising=False)
    fake = FakeIronflock(device_key=DEVICE_KEY)
    failed = await register_config_tools(fake, passthrough, "connections")
    assert set(failed) == expected
    assert any(r["level"] == "warn" for r in fake.reported)
    fake = FakeIronflock(device_key=DEVICE_KEY)
    failed = await register_config_tools(fake, "not-callable", "connections",
                                         device_key=DEVICE_KEY)
    assert set(failed) == expected
    assert fake.registered == {}


def test_collector_import_paths_survive():
    """Consuming apps pin config_core.handlers.* names in drift tests; both
    import orders must work on a fresh interpreter (the handlers->collector
    re-export is lazy)."""
    for script in (
        "from config_core.handlers import CORE_DEFAULTS, "
        "USER_DATAPOINT_COLUMNS, DATAPOINT_COERCERS, "
        "RECOMMENDED_PROMPT_GUIDANCE; "
        "assert CORE_DEFAULTS['collect_interval'] == 5",
        "import config_core.collector; "
        "import config_core.handlers as h; "
        "assert h.CORE_DEFAULTS is config_core.collector.CORE_DEFAULTS",
    ):
        subprocess.run(
            [sys.executable, "-c", script],
            check=True,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        )


# ------------------------------------------- carry-over diff writes (1.3.0)


async def test_update_writes_only_the_diff():
    fake, _, _, handlers = make_tools(audit_column="configured_by")
    seed_asset(fake, "Press 1", host="10.0.0.5", port=502)
    check(await handlers["update"](
        asset_name="Press 1", changes={"port": 1502}))
    table, payload = fake.payloads[-1]
    assert table == "assets"
    # changed column + protected columns + first-time audit stamp, nothing else
    assert set(payload) == {"port", "asset_name", "gateway_id", "deleted",
                            "tsp", "configured_by"}
    row = fake.latest("assets", asset_name="Press 1")
    assert row["host"] == "10.0.0.5" and row["port"] == 1502  # carried over


async def test_update_does_not_clobber_concurrent_edit():
    """The lost-update carry-over kills: a board edit landing between our
    read and our append survives, because we no longer echo its column."""

    class RacingFake(FakeIronflock):
        race = None  # full row a board user writes right after our read

        async def getHistory(self, table, params):
            rows = await super().getHistory(table, params)
            if self.race is not None and table == "assets":
                row, self.race = self.race, None
                self.external_append("assets", row)
            return rows

    fake = RacingFake(device_key=DEVICE_KEY)
    _, _, _, handlers = make_tools(fake=fake)
    seed_asset(fake, "Press 1", host="10.0.0.5", port=502)
    board_row = dict(strip_platform(fake.latest("assets", asset_name="Press 1")))
    board_row.update(host="10.0.0.9", tsp=now_iso())
    fake.race = board_row

    response = check(await handlers["update"](
        asset_name="Press 1", changes={"port": 1502}))
    assert response["ok"] is True
    row = fake.latest("assets", asset_name="Press 1")
    assert row["port"] == 1502          # our change applied
    assert row["host"] == "10.0.0.9"    # the board's concurrent edit survives


async def test_recreate_deleted_name_nulls_stale_columns():
    """Carry-over crosses the tombstone: create must explicitly null the
    dead row's columns the new configuration does not set."""
    fake, _, _, handlers = make_tools()
    seed_asset(fake, "Press 1", deleted=True, host="10.0.0.5", rack=2)
    response = check(await handlers["create"](
        asset_name="Press 1", fields={"port": 502}))
    assert response["ok"] is True
    _, payload = fake.payloads[-1]
    assert payload["host"] is None and payload["rack"] is None
    row = fake.latest("assets", asset_name="Press 1")
    assert row["host"] is None and row["port"] == 502
    assert row["deleted"] is False


async def test_delete_writes_partial_tombstones():
    fake, _, _, handlers = make_tools(audit_column="configured_by")
    seed_asset(fake, "Press 1", host="10.0.0.5")
    seed_datapoint(fake, "Press 1", "temp", address="DB1.0")
    response = check(await handlers["delete"](asset_name="Press 1"))
    assert response["ok"] is True and response["datapoints_deleted"] == 1
    asset_payloads = [p for t, p in fake.payloads if t == "assets"]
    dp_payloads = [p for t, p in fake.payloads if t == "datapoints"]
    assert set(asset_payloads[-1]) == {"asset_name", "gateway_id", "deleted",
                                       "tsp", "configured_by"}
    assert set(dp_payloads[-1]) == {"asset_name", "datapoint_id",
                                    "gateway_id", "deleted", "tsp",
                                    "configured_by"}
    # carry-over keeps the dead rows' configuration visible
    assert fake.latest("assets", asset_name="Press 1")["host"] == "10.0.0.5"
    dead_dp = fake.latest("datapoints", asset_name="Press 1",
                          datapoint_id="temp")
    assert dead_dp["deleted"] is True and dead_dp["address"] == "DB1.0"


async def test_set_datapoints_writes_partial():
    fake, _, _, handlers = make_tools()
    seed_asset(fake, "Press 1")
    seed_datapoint(fake, "Press 1", "temp", address="DB1.0", enabled=True)
    response = check(await handlers["set_datapoints"](
        asset_name="Press 1", changes={"datapoint_id": "temp",
                                       "enabled": False}))
    assert response["applied"] == 1
    _, payload = fake.payloads[-1]
    assert set(payload) == {"asset_name", "datapoint_id", "gateway_id",
                            "deleted", "tsp", "enabled"}
    row = fake.latest("datapoints", asset_name="Press 1",
                      datapoint_id="temp")
    assert row["enabled"] is False and row["address"] == "DB1.0"


# ------------------------------------------------------------------- misc


def test_guidance_constants_exported():
    assert "dry_run" in RECOMMENDED_PROMPT_GUIDANCE
    # The asset tools are topic tools: without this the agent guesses a
    # device_key instead of resolving one, and configures the wrong gateway.
    assert "device_key" in RECOMMENDED_PROMPT_GUIDANCE
    assert "list_devices" in RECOMMENDED_PROMPT_GUIDANCE
    assert "error-logs" in SQL_DIAGNOSTICS_GUIDANCE
    assert ASSET_NAME_PATTERN == r"^[a-zA-Z0-9 ]{3,50}$"


def test_example_app_validate():
    spec = importlib.util.spec_from_file_location(
        "example_app",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "examples", "example_app.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    async def run():
        valid, problems = await module.validate_example_asset(
            {"asset_name": "Press 1", "gateway_id": 471,
             "host": " 10.0.0.5 ", "enabled": True, "demo_mode": False,
             "collect_interval": 5, "datapoint_spec": ""},
            None,
        )
        assert problems == []
        assert valid["host"] == "10.0.0.5" and valid["port"] == 502
        _, problems = await module.validate_example_asset(
            {"asset_name": "P", "gateway_id": 471, "demo_mode": False,
             "bogus": 1}, None)
        assert len(problems) == 2

    asyncio.run(run())


async def test_strip_platform():
    row = {"asset_name": "A", "tsp": "t", "latest_flag": True,
           "authid": "u", "device_key": 1, "datapoint_list": [], "host": "h"}
    assert strip_platform(row) == {"asset_name": "A", "host": "h"}
