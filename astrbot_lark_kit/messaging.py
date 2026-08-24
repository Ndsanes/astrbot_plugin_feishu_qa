"""lark-cli 消息发送 —— bot 身份的最简 transport。

身份固定为 bot(平台消息平面);user 身份的业务 API(docs/bitable)不走本模块。
target 语义按前缀自动分派:``oc_`` 前缀 → chat-id;``ou_`` 前缀 → user-id。
图片发送沿用 lark-cli 的 cwd 相对路径约束:在图片所在目录内以文件名调用。
"""

from __future__ import annotations

from pathlib import Path

from .cli import resolve_cli_bin, run_lark_cli
from .envelope import LarkEnvelope
from .rate_limit import RateLimiter

__all__ = ["LarkMessenger"]


class LarkMessenger:
    """bot 身份的消息发送器(文本/图片),带限流。"""

    def __init__(
        self,
        *,
        binary: Path | None = None,
        env: dict[str, str] | None = None,  # 注入子进程环境(如 HOME),兼作二进制解析提示
        rate: float = 5.0,
        timeout_s: float = 30.0,
    ) -> None:
        self._binary = Path(binary) if binary else None
        self._env = env
        self._timeout_s = timeout_s
        self._limiter = RateLimiter(rate=rate)

    async def _send(self, args: list[str], *, cwd: str | None = None) -> LarkEnvelope:
        await self._limiter.acquire()
        binary = self._binary or resolve_cli_bin(env=self._env)
        return await run_lark_cli(
            args,
            bin_path=binary,
            # env 既用于二进制解析,也必须注入子进程环境(如 HOME 登录态),
            # _spawn 会以 {**os.environ, **extra_env} 合成完整子进程环境
            env=self._env,
            extra_env=self._env,
            timeout_s=self._timeout_s,
            cwd=cwd,
        )

    @staticmethod
    def _target_args(target: str) -> list[str]:
        target = target.strip()
        if target.startswith("oc_"):
            return ["--chat-id", target]
        if target.startswith("ou_"):
            return ["--user-id", target]
        raise ValueError(f"无法识别的 target(应为 oc_/ou_ 前缀): {target!r}")

    async def send_text(self, target: str, text: str) -> LarkEnvelope:
        """以 bot 身份向 chat/user 发送纯文本。"""
        return await self._send(
            [
                "im",
                "+messages-send",
                "--as",
                "bot",
                *self._target_args(target),
                "--text",
                text,
            ]
        )

    async def send_card(self, target: str, card: dict) -> LarkEnvelope:
        """以 bot 身份发送飞书卡片消息(msg_type=interactive)。"""
        import json as _json

        return await self._send(
            [
                "im",
                "+messages-send",
                "--as",
                "bot",
                *self._target_args(target),
                "--msg-type",
                "interactive",
                "--content",
                _json.dumps(card, ensure_ascii=False),
            ]
        )

    async def send_image(self, target: str, image_path: str | Path) -> LarkEnvelope:
        """以 bot 身份发送本地图片(lark-cli 要求 cwd 相对路径)。"""
        image_path = Path(image_path).resolve()
        if not image_path.is_file():
            raise FileNotFoundError(f"图片不存在: {image_path}")
        return await self._send(
            [
                "im",
                "+messages-send",
                "--as",
                "bot",
                *self._target_args(target),
                "--image",
                f"./{image_path.name}",
            ],
            cwd=str(image_path.parent),
        )
