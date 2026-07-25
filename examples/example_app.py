"""Minimal config_core wiring for a fictional single-protocol collector app.

The app contributes exactly one thing to the config tools: the validate
function. It owns every app-specific rule -- which columns exist, what is
required, how the datapoint spec is parsed -- and returns the valid
(normalized, defaults applied) config. config_core handles everything else.
"""


async def validate_example_asset(config, existing):
    """Return (valid_config, problems) for the example app.

    ``config`` is the full candidate row (echo-merged on update, core
    defaults applied on create; core columns arrive pre-normalized).
    ``existing`` is the current row or None on create.
    """
    problems = []
    known = {
        "asset_name", "gateway_id", "datapoint_spec", "collect_interval",
        "enabled", "demo_mode", "host", "port",
    }
    for column in sorted(set(config) - known):
        problems.append(f"unknown field {column!r} - this app has: host, port")
    host = str(config.get("host") or "").strip()
    if not host and not config.get("demo_mode"):
        problems.append("host is required (or enable demo_mode)")
    if problems:
        return None, problems
    valid = dict(config)
    valid["host"] = host
    if not valid.get("port"):
        valid["port"] = 502  # app default
    return valid, []


# In the app's ProtocolAdapter.start_background(collector) -- the only place
# with a live WAMP session (import inside the method so a broken package
# degrades the assistant, never collection):
#
#     try:
#         from config_core import register_asset_tools
#     except Exception as e:
#         await collector.report_error(
#             f"config tools unavailable: {e}", level="warn",
#             user_message="The AI assistant's configuration tools could not "
#                          "be loaded - it can advise but not apply changes.")
#     else:
#         await register_asset_tools(collector.ironflock, validate_example_asset)
