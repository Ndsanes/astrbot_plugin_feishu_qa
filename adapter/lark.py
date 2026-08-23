"""LarkAdapter — QA 插件的飞书访问层。

职责(spec §2/§65):文档抓取(markdown/XML)、revision、媒体下载、
auth 健康检查与 re-auth 引导。业务操作一律 user 身份。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from astrbot_lark_kit import (
    AuthStatus,
    Health,
    RateLimiter,
    auth_status_from_dict,
    health_of,
    run_lark_cli,
    run_lark_cli_json,
)

DOC_FORMAT_XML = "xml"
DOC_FORMAT_MARKDOWN = "markdown"

_MEDIA_TIMEOUT_S = 120.0  # 图片较大,放宽单张下载超时


@dataclass
class DocContent:
    """docs +fetch 的规范化结果。"""

    content: str
    document_id: str
    revision_id: int


class LarkAdapter:
    """对 lark-cli docs/media/auth 能力的薄封装。

    不含任何 AstrBot API 依赖,便于独立测试;
    通知通过注入的回调完成(插件层接到 bot 身份 IM 或 AstrBot 消息)。
    """

    def __init__(
        self,
        *,
        doc_ref: str,
        rate: float = 5.0,
        timeout_s: float = 30.0,
        env: dict[str, str] | None = None,
        bin_path: Path | None = None,
    ) -> None:
        self.doc_ref = doc_ref
        self._timeout_s = timeout_s
        self._env = env
        self._bin_path = bin_path
        self._limiter = RateLimiter(rate=rate)

    async def _run(
        self, args: list[str], *, timeout_s: float | None = None, cwd: str | None = None
    ):
        return await run_lark_cli(
            args,
            timeout_s=timeout_s or self._timeout_s,
            limiter=self._limiter,
            env=self._env,
            bin_path=self._bin_path,
            cwd=cwd,
        )

    # ── 文档 ──

    async def fetch_doc(self, *, fmt: str = DOC_FORMAT_MARKDOWN) -> DocContent:
        """抓取整份文档,返回内容 + document_id + revision_id。"""
        envelope = await self._run(
            [
                "docs",
                "+fetch",
                "--as",
                "user",
                "--doc",
                self.doc_ref,
                "--doc-format",
                fmt,
                "--detail",
                "simple",
            ]
        )
        doc = envelope.document
        content = doc.get("content")
        if not isinstance(content, str):
            from astrbot_lark_kit.errors import CliInvalidOutputError

            raise CliInvalidOutputError("envelope.data.document.content 缺失")
        revision_raw = doc.get("revision_id", -1)
        try:
            revision_id = int(revision_raw)
        except (TypeError, ValueError):
            revision_id = -1
        return DocContent(
            content=content,
            document_id=str(doc.get("document_id") or ""),
            revision_id=revision_id,
        )

    # ── 媒体 ──

    async def download_media(
        self,
        token: str,
        output_path: Path,
        *,
        overwrite: bool = False,
    ) -> Path | None:
        """下载文档媒体到本地。

        已存在且非空时默认跳过(幂等)。失败返回 None,不抛异常——
        单张图片失败不得阻断 corpus 构建(spec §12)。
        """
        output_path = Path(output_path)
        if not overwrite and output_path.is_file() and output_path.stat().st_size > 0:
            return output_path
        # lark-cli 要求 --output 为 cwd 内相对路径:在目标目录内以文件名调用
        output_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            envelope = await self._run(
                [
                    "docs",
                    "+media-download",
                    "--as",
                    "user",
                    "--type",
                    "media",
                    "--token",
                    token,
                    "--output",
                    f"./{output_path.name}",
                    *(["--overwrite"] if overwrite else []),
                ],
                timeout_s=_MEDIA_TIMEOUT_S,
                cwd=str(output_path.parent),
            )
        except Exception:
            return None

        saved = envelope.data.get("saved_path")
        path = Path(str(saved)) if saved else output_path
        return path if path.is_file() and path.stat().st_size > 0 else None

    # ── 登录态 ──

    async def auth_status(self) -> AuthStatus:
        obj = await run_lark_cli_json(
            ["auth", "status"],
            timeout_s=self._timeout_s,
            limiter=self._limiter,
            env=self._env,
            bin_path=self._bin_path,
        )
        return auth_status_from_dict(obj)

    async def auth_health(self, *, warning_hours: float = 48.0) -> Health:
        status = await self.auth_status()
        return health_of(status, warning_hours=warning_hours)


# re-auth 引导命令模板:管理员在宿主机执行即可走 CLI 自带 Device Flow。
REAUTH_HINT = "请在部署机执行: lark-cli auth login  (或 lark-cli auth qrcode 扫码)"


async def wait_for_auth_recovery(
    adapter: LarkAdapter,
    *,
    poll_seconds: float = 60.0,
    max_polls: int = 60,
    warning_hours: float = 48.0,
) -> Health:
    """后台轮询直到登录态恢复或轮询次数耗尽(spec §7 的授权闭环)。"""
    for _ in range(max_polls):
        await asyncio.sleep(poll_seconds)
        try:
            health = await adapter.auth_health(warning_hours=warning_hours)
        except Exception:
            continue
        if health in (Health.HEALTHY,):
            return health
    return Health.UNAVAILABLE
