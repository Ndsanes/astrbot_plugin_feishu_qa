"""astrbot_lark_kit 公共出口。"""

from __future__ import annotations

from .auth import AuthStatus, Health, auth_status_from_dict, health_of, parse_auth_status
from .bootstrap import (
    DEFAULT_CLI_VERSION,
    SUPPORTED_PLATFORMS,
    bundled_cli_download_url,
    ensure_bundled_cli,
)
from .cli import (
    bundled_cli_platform,
    find_bundled_cli,
    resolve_cli_bin,
    run_lark_cli,
    run_lark_cli_json,
)
from .envelope import LarkCliErrorInfo, LarkEnvelope, parse_envelope
from .errors import (
    AuthRequiredError,
    CliExecutionError,
    CliInvalidOutputError,
    CliNotFoundError,
    CliTimeoutError,
    LarkKitError,
    UmoParseError,
)
from .platforms import PlatformIdentity, resolve_platform_instance
from .rate_limit import RateLimiter
from .state import resolve_state_home

__version__ = "0.2.0"

__all__ = [
    "DEFAULT_CLI_VERSION",
    "SUPPORTED_PLATFORMS",
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
    "PlatformIdentity",
    "RateLimiter",
    "UmoParseError",
    "__version__",
    "auth_status_from_dict",
    "bundled_cli_download_url",
    "bundled_cli_platform",
    "ensure_bundled_cli",
    "find_bundled_cli",
    "health_of",
    "parse_auth_status",
    "parse_envelope",
    "resolve_cli_bin",
    "resolve_platform_instance",
    "resolve_state_home",
    "run_lark_cli",
    "run_lark_cli_json",
]
