"""统一错误分类。

lark-cli 调用的所有失败路径都必须收敛为这里的稳定异常类型,
下游不得自行解析 returncode / stderr / JSON。
"""

from __future__ import annotations


class LarkKitError(Exception):
    """所有 lark-kit 异常的根。"""

    code = "LARK_KIT_ERROR"


class CliNotFoundError(LarkKitError):
    """lark-cli 可执行文件不存在或不可执行。"""

    code = "CLI_NOT_FOUND"


class CliTimeoutError(LarkKitError):
    """子进程执行超时(已被强制终止)。"""

    code = "CLI_TIMEOUT"


class CliExecutionError(LarkKitError):
    """进程非零退出或 spawn 失败等执行层错误。"""

    code = "CLI_EXECUTION_FAILED"


class CliInvalidOutputError(LarkKitError):
    """stdout 不是合法 JSON 或不符合 envelope 结构。"""

    code = "CLI_INVALID_OUTPUT"


class AuthRequiredError(LarkKitError):
    """需要用户登录态(未登录/token 失效)。

    语义上属于业务可预期的失败:调用方应触发 re-auth 引导而非盲目重试。
    """

    code = "AUTH_REQUIRED"
