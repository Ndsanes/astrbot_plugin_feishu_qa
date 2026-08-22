"""astrbot_lark_kit 公共出口。"""

from __future__ import annotations

from .auth import AuthStatus, Health, auth_status_from_dict, health_of, parse_auth_status
from .cli import resolve_cli_bin, run_lark_cli, run_lark_cli_json
from .envelope import LarkCliErrorInfo, LarkEnvelope, parse_envelope
from .errors import (
    AuthRequiredError,
    CliExecutionError,
    CliInvalidOutputError,
    CliNotFoundError,
    CliTimeoutError,
    LarkKitError,
)
from .rate_limit import RateLimiter

__version__ = "0.1.0"

__all__ = [
    "AuthRequiredError",
    "AuthStatus",
    "CliExecutionError",
    "CliInvalidOutputError",
    "CliNotFoundError",
    "CliTimeoutError",
    "Health",
    "LarkCliErrorInfo",
    "LarkEnvelope",
    "LarkKitError",
    "RateLimiter",
    "__version__",
    "auth_status_from_dict",
    "health_of",
    "parse_auth_status",
    "parse_envelope",
    "resolve_cli_bin",
    "run_lark_cli",
    "run_lark_cli_json",
]
