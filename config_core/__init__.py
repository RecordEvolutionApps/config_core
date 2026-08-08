"""config_core -- agent-facing asset configuration tools for IronFlock
collector apps.

The app owns its asset shape and validates it through ONE injected function;
this library owns the repetitive CRUD plumbing (reads, echo-merge writes,
soft delete with cascade, verification, never-raise agent responses). See
README.md for the full contract.
"""

__version__ = "1.1.0"

from .handlers import (
    ASSET_NAME_PATTERN,
    RECOMMENDED_PROMPT_GUIDANCE,
    SQL_DIAGNOSTICS_GUIDANCE,
    coerce_rpc_args,
)
from .register import DEFAULT_TOPICS, register_asset_tools

__all__ = [
    "__version__",
    "ASSET_NAME_PATTERN",
    "DEFAULT_TOPICS",
    "RECOMMENDED_PROMPT_GUIDANCE",
    "SQL_DIAGNOSTICS_GUIDANCE",
    "coerce_rpc_args",
    "register_asset_tools",
]
