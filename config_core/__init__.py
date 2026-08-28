"""config_core -- agent-facing configuration tools for IronFlock apps.

The app owns its config-row shape and validates it through ONE injected
function; this library owns the repetitive CRUD plumbing (reads, echo-merge
writes, soft delete, verification, never-raise agent responses).
``register_asset_tools`` is the collector preset (assets + datapoints +
assetstatus); ``register_config_tools`` manages any named-row config table.
See README.md for the full contract.
"""

__version__ = "1.2.0"

from .collector import (
    RECOMMENDED_PROMPT_GUIDANCE,
    SQL_DIAGNOSTICS_GUIDANCE,
)
from .handlers import (
    ASSET_NAME_PATTERN,
    coerce_rpc_args,
)
from .register import (
    DEFAULT_TOPICS,
    register_asset_tools,
    register_config_tools,
)

__all__ = [
    "__version__",
    "ASSET_NAME_PATTERN",
    "DEFAULT_TOPICS",
    "RECOMMENDED_PROMPT_GUIDANCE",
    "SQL_DIAGNOSTICS_GUIDANCE",
    "coerce_rpc_args",
    "register_asset_tools",
    "register_config_tools",
]
