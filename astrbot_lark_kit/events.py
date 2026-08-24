"""lark-cli 事件流 —— im.message.receive_v1 的归一化接收层。

处理链(平台插件不得直接解析原始 NDJSON):

    lark-cli NDJSON
        ↓ normalize_event()
    NormalizedLarkMessage
        ↓ (Platform Adapter)
    AstrBotMessage

身份固定为 bot(``--as bot``,飞书仅允许 bot 接收消息)。
生命周期:EOF/非零退出自动重启,bounded backoff(1s→2s→…→30s 封顶);
cancel 时子进程终止并等待,不留孤儿进程。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import CliExecutionError
from .state import ensure_short_home

__all__ = ["EventStream", "NormalizedLarkMessage", "normalize_event"]

_EVENT_KEY = "im.message.receive_v1"
_DEDUP_TTL_S = 300.0
_BACKOFF_BASE_S = 1.0
_BACKOFF_MAX_S = 30.0


@dataclass(frozen=True, slots=True)
class NormalizedLarkMessage:
    """归一化后的飞书消息事件。

    Attributes:
        message_id: 消息 ID(om_ 前缀),去重键。
        sender_id: 发送者 open_id(ou_ 前缀)。
        sender_name: 发送者显示名;缺失时由调用方以 sender_id 兜底。
        sender_type: ``user`` 或 ``bot``(bot 为本应用自身发出的消息)。
        chat_id: 会话 ID(oc_ 前缀)。
        chat_type: ``group`` 或 ``p2p``。
        message_type: 平台消息类型(text/image/post/...)。
        text: 预渲染的人类可读正文(非文本消息也有摘要)。
        timestamp: 事件时间戳(ms 字符串)。
        raw: 原始 payload(只读引用,调用方不得修改)。
    """

    message_id: str
    sender_id: str
    sender_name: str
    sender_type: str
    chat_id: str
    chat_type: str
    message_type: str
    text: str
    timestamp: str
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_from_bot(self) -> bool:
        """是否为本 bot 自身产生的消息(回环防护依据)。"""
        return self.sender_type == "bot"


def normalize_event(payload: dict[str, Any]) -> NormalizedLarkMessage | None:
    """把 lark-cli 事件 payload 归一化;非目标事件返回 None。

    缺失的可选字段一律以空串兜底,不因字段缺失丢弃事件。
    """
    if not isinstance(payload, dict):
        return None
    if payload.get("type") and payload["type"] != _EVENT_KEY:
        return None
    return NormalizedLarkMessage(
        message_id=str(payload.get("message_id") or payload.get("id") or ""),
        sender_id=str(payload.get("sender_id") or ""),
        sender_name=str(payload.get("sender_name") or ""),
        sender_type=str(payload.get("sender_type") or "user"),
        chat_id=str(payload.get("chat_id") or ""),
        chat_type=str(payload.get("chat_type") or ""),
        message_type=str(payload.get("message_type") or ""),
        text=str(payload.get("content") or ""),
        timestamp=str(payload.get("create_time") or payload.get("timestamp") or ""),
        raw=payload,
    )


class _Deduper:
    """短 TTL 去重:message_id → 最近见到的时间。"""

    def __init__(self, ttl_s: float = _DEDUP_TTL_S) -> None:
        self._ttl = ttl_s
        self._seen: dict[str, float] = {}

    def first_time(self, key: str) -> bool:
        """首次见到返回 True 并记录;TTL 内重复返回 False。"""
        now = time.monotonic()
        cutoff = now - self._ttl
        stale = [k for k, t in self._seen.items() if t < cutoff]
        for k in stale:
            self._seen.pop(k, None)
        if not key or key in self._seen:
            return False
        self._seen[key] = now
        return True


class EventStream:
    """lark-cli 事件消费进程的托管包装。

    用法::

        stream = EventStream(binary=..., state_home=...)
        async for msg in stream.stream():
            ...

    - 子进程异常退出自动重启(bounded backoff 1s→30s);
    - :meth:`stream` 被取消时终止子进程并等待,无孤儿;
    - 归一化 + TTL 去重 + bot 自消息过滤都在此层完成。
    """

    def __init__(
        self,
        *,
        binary: Path,
        state_home: Path | None = None,
        alias_root: Path | None = None,
        extra_env: dict[str, str] | None = None,
        event_key: str = _EVENT_KEY,
        max_backoff_s: float = _BACKOFF_MAX_S,
        max_restarts: int | None = None,
        log_cb: Callable[[str], None] | None = None,
    ) -> None:
        import os

        self._binary = Path(binary)
        child_env = {**os.environ}
        alias_note: str = ""
        if state_home is not None:
            runtime_home = ensure_short_home(
                state_home,
                alias_root=alias_root if alias_root is not None else Path("/tmp"),
            )
            if runtime_home != state_home:
                alias_note = f"登录态路径过深,子进程 HOME 使用别名 {runtime_home} -> {state_home}"
            child_env["HOME"] = str(runtime_home)
        if extra_env:
            child_env.update(extra_env)
        self._env = child_env
        self._event_key = event_key
        self._max_backoff = max_backoff_s
        self._max_restarts = max_restarts
        self._log = log_cb or (lambda _msg: None)
        if alias_note:
            self._log(alias_note)
        self._dedup = _Deduper()
        self._proc: asyncio.subprocess.Process | None = None

    def _spawn_args(self) -> list[str]:
        return [
            str(self._binary),
            "event",
            "consume",
            self._event_key,
            "--as",
            "bot",
        ]

    async def _terminate(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        # lark-cli consume 以 stdin EOF 为退出信号:先关 stdin 走优雅清理路径,
        # 避免跳过 cleanup 泄漏服务端订阅;terminate 仅作超时兜底。
        if proc.stdin is not None and not proc.stdin.is_closing():
            proc.stdin.close()
        if proc.stdin is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.stdin.wait_closed(), timeout=3)
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except TimeoutError:
            proc.kill()
            await proc.wait()
        # 关闭 stdout 管道传输,避免事件循环残留读句柄导致挂起
        if proc.stdout is not None:
            proc.stdout._transport.close()
    async def stream(self) -> AsyncIterator[NormalizedLarkMessage]:
        """持续产出归一化消息;取消/关闭即优雅停止并回收子进程。

        - 子进程异常退出或 EOF 自动重启(bounded backoff);
        - ``max_restarts`` 限制重启次数(None = 不限),供测试与受控场景使用;
        - GeneratorExit(aclose)/CancelledError 两条停止路径都回收子进程。
        """
        backoff = _BACKOFF_BASE_S
        restarts = 0
        try:
            while True:
                try:
                    self._proc = await asyncio.create_subprocess_exec(
                        *self._spawn_args(),
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        env=self._env,
                    )
                except (FileNotFoundError, PermissionError, OSError) as exc:
                    raise CliExecutionError(
                        f"无法启动 lark-cli event consume: {exc}"
                    ) from exc
                assert self._proc is not None and self._proc.stdout is not None
                self._log(f"consumer pid={self._proc.pid}")
                ran_ok = False
                try:
                    async for raw_line in self._proc.stdout:
                        line = raw_line.decode("utf-8", errors="replace").strip()
                        if not line:
                            continue
                        try:
                            payload = json.loads(line)
                        except json.JSONDecodeError:
                            continue  # malformed 行直接忽略
                        msg = normalize_event(payload)
                        if msg is None or msg.is_from_bot:
                            continue
                        if not self._dedup.first_time(msg.message_id):
                            continue
                        ran_ok = True
                        yield msg
                except asyncio.CancelledError:
                    raise
                finally:
                    # 无论 EOF/异常/生成器关闭,都先回收当前子进程
                    code = self._proc.returncode
                    err_tail = b""
                    if self._proc is not None and self._proc.stderr is not None:
                        with contextlib.suppress(Exception):
                            err_tail = await asyncio.wait_for(
                                self._proc.stderr.read(65536), timeout=5
                            )
                    await self._terminate()
                    if self._log:
                        self._log(
                            f"consumer exit code={code} "
                            f"stderr={err_tail.decode('utf-8', errors='replace')!r}"
                        )
                    if code not in (None, 0):
                        self._log(f"consumer 非零退出 code={code}")
                # 停止路径:GeneratorExit 已向上传播,不会到达这里;
                # 到达此处说明是 EOF/非零退出 → 判断是否重启
                if self._max_restarts is not None and restarts >= self._max_restarts:
                    return
                restarts += 1
                if ran_ok and code == 0:
                    backoff = _BACKOFF_BASE_S  # 干净退出后重置退避
                    await asyncio.sleep(0)
                else:
                    await asyncio.sleep(min(backoff, self._max_backoff))
                    backoff = min(backoff * 2, self._max_backoff)
        finally:
            await self._terminate()

