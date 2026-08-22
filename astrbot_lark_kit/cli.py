"""lark-cli 子进程调用层。

统一处理:可执行文件缺失、超时、非零退出、非法 JSON、非法 envelope、
auth 失败分类。下游只面对 LarkEnvelope 或稳定的 LarkKitError。

两种输出形态:
- envelope 形态(docs/drive/base 等):{"ok": bool, ...} → run_lark_cli()
- 裸 JSON 形态(auth status/whoami):顶层即数据对象 → run_lark_cli_json()
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Any

from .envelope import LarkEnvelope, parse_envelope
from .errors import (
    AuthRequiredError,
    CliExecutionError,
    CliInvalidOutputError,
    CliNotFoundError,
    CliTimeoutError,
)
from .rate_limit import RateLimiter

DEFAULT_TIMEOUT_S = 30.0


def resolve_cli_bin(env: dict[str, str] | None = None) -> Path:
    """解析 lark-cli 二进制路径。

    优先级:环境变量 LARK_CLI_PATH > env 参数(便于测试注入)> PATH 查找。
    """
    source = env if env is not None else os.environ
    override = source.get("LARK_CLI_PATH")
    if override:
        path = Path(override)
        if path.is_file() and os.access(path, os.X_OK):
            return path
        raise CliNotFoundError(f"LARK_CLI_PATH 指向不可执行文件: {override}")

    found = shutil.which("lark-cli")
    if found:
        return Path(found)
    raise CliNotFoundError("PATH 中未找到 lark-cli 可执行文件")


def _classify_failure(envelope: LarkEnvelope) -> Exception:
    """把 ok=false 的 envelope 映射为稳定异常。"""
    if envelope.error.is_auth_related():
        return AuthRequiredError(envelope.error.message or "需要登录态")
    return CliExecutionError(
        f"lark-cli 调用失败: type={envelope.error.type} "
        f"subtype={envelope.error.subtype} code={envelope.error.code} "
        f"message={envelope.error.message}"
    )


async def _spawn(
    args: list[str],
    binary: Path,
    timeout_s: float,
) -> tuple[bytes, bytes]:
    """spawn 并回收输出;非零退出抛 CliExecutionError。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            str(binary),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, PermissionError, NotADirectoryError) as exc:
        raise CliNotFoundError(f"无法启动 lark-cli: {exc}") from exc
    except OSError as exc:  # 其他 OS 层 spawn 失败归入执行错误
        raise CliExecutionError(f"lark-cli spawn 失败: {exc}") from exc

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise CliTimeoutError(f"lark-cli 超时({timeout_s}s): {args[:3]}") from exc

    if proc.returncode != 0:
        stderr_text = stderr.decode("utf-8", errors="replace").strip()
        raise CliExecutionError(
            f"lark-cli 退出码 {proc.returncode}: {stderr_text[:500]}"
        )
    return stdout, stderr


async def run_lark_cli(
    args: list[str],
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    limiter: RateLimiter | None = None,
    env: dict[str, str] | None = None,
    bin_path: Path | None = None,
) -> LarkEnvelope:
    """spawn lark-cli 并返回解析后的 envelope(envelope 形态命令)。

    Raises:
        CliNotFoundError: 二进制不存在。
        CliTimeoutError: 超时(进程已被终止)。
        CliExecutionError: 非零退出或 spawn 失败;或 envelope ok=false 且非 auth 类。
        CliInvalidOutputError: stdout 无法解析为合法 envelope。
        AuthRequiredError: envelope ok=false 且错误与登录态相关。
    """
    binary = bin_path or resolve_cli_bin(env=env)
    if limiter is not None:
        await limiter.acquire()

    stdout, _ = await _spawn(args, binary, timeout_s)
    envelope = parse_envelope(stdout.decode("utf-8", errors="replace"))
    if not envelope.ok:
        raise _classify_failure(envelope)
    return envelope


async def run_lark_cli_json(
    args: list[str],
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    limiter: RateLimiter | None = None,
    env: dict[str, str] | None = None,
    bin_path: Path | None = None,
) -> dict[str, Any]:
    """spawn lark-cli 并解析裸 JSON 输出(auth status 等非 envelope 命令)。"""
    binary = bin_path or resolve_cli_bin(env=env)
    if limiter is not None:
        await limiter.acquire()

    stdout, _ = await _spawn(args, binary, timeout_s)
    try:
        obj = json.loads(stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise CliInvalidOutputError(f"stdout 不是合法 JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise CliInvalidOutputError("期望顶层 JSON 对象")
    return obj
