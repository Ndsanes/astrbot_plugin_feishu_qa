"""LarkAdapter — QA 插件的飞书访问层。

职责(spec §2/§65):文档抓取(markdown/XML)、revision、媒体下载、
auth 健康检查与 re-auth 引导。业务操作一律 user 身份。
"""

from __future__ import annotations

import asyncio
import os
import stat as stat_mod
from dataclasses import dataclass
from pathlib import Path

try:
    from astrbot_lark_kit import (
        AuthStatus,
        Health,
        RateLimiter,
        auth_status_from_dict,
        bundled_cli_platform,
        find_bundled_cli,
        health_of,
        resolve_cli_bin,
        run_lark_cli,
        run_lark_cli_json,
    )
except ImportError:  # 打包分发时 kit 以子包形式随插件提供
    from ..astrbot_lark_kit import (  # type: ignore
        AuthStatus,
        Health,
        RateLimiter,
        auth_status_from_dict,
        bundled_cli_platform,
        find_bundled_cli,
        health_of,
        resolve_cli_bin,
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

    def _resolve_bundled(self) -> Path | None:
        """解析携带二进制;存在但不可执行时尝试补权限位后重试。"""
        found = find_bundled_cli(self._vendor_dir)
        if found is not None:
            return found
        if self._bundled_platform is None:
            return None
        candidate = self._vendor_dir / self._bundled_platform / "lark-cli"
        if not candidate.is_file():
            return None
        try:  # 解压/挂载可能丢掉可执行位
            candidate.chmod(candidate.stat().st_mode | stat_mod.S_IEXEC)
        except OSError:
            return None
        if os.access(candidate, os.X_OK):
            return candidate
        return None

    def cli_diagnostics(self) -> str:
        """人读的 CLI 解析报告(供 /qa_status 展示)。"""
        binary = self._bin_path or find_bundled_cli(self._vendor_dir)
        if self._injected_bin is not None:
            source = "注入"
        else:
            source = "携带" if binary else "未找到"
        plat = self._bundled_platform or "未知平台"
        if binary:
            return f"CLI={source} platform={plat} path={binary}"
        candidate = (
            self._vendor_dir / plat / "lark-cli" if self._bundled_platform else None
        )
        detail = f"存在={candidate.is_file() if candidate else False}"
        return f"CLI=未找到 platform={plat} vendor_dir={self._vendor_dir} {detail}"

    def __init__(
        self,
        *,
        doc_ref: str,
        rate: float = 5.0,
        timeout_s: float = 30.0,
        env: dict[str, str] | None = None,
        bin_path: Path | None = None,
        state_home: Path | None = None,
    ) -> None:
        self.doc_ref = doc_ref
        self._timeout_s = timeout_s
        self._env = env
        self._injected_bin = bin_path
        # 二进制优先级:显式注入 > 插件携带(vendor/lark-cli/<平台>/) > PATH。
        self._vendor_dir = Path(__file__).resolve().parents[1] / "vendor" / "lark-cli"
        self._bundled_platform = bundled_cli_platform()
        self._bin_path = bin_path or self._resolve_bundled()
        # 登录态落盘目录:重定向 HOME,使 auth 凭据随插件数据持久化(容器友好)。
        self._extra_env = {"HOME": str(state_home)} if state_home else None
        # 供 re-auth 等旁路调用复用同一测试注入通道
        self.env = env
        self.bin_path = self._bin_path
        self.extra_env = self._extra_env
        self.state_home = state_home
        if state_home is not None:
            state_home.mkdir(parents=True, exist_ok=True)
        self._limiter = RateLimiter(rate=rate)
        self.timeout_s = timeout_s
        self.limiter = self._limiter

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
            extra_env=self._extra_env,
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
            try:
                from astrbot_lark_kit.errors import CliInvalidOutputError
            except ImportError:  # 打包分发时 kit 以子包形式随插件提供
                from ..astrbot_lark_kit.errors import (  # type: ignore
                    CliInvalidOutputError,
                )

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
        # drive +preview(source_file) 走预览通道,文档关闭"允许下载"时仍可取图;
        # 失败再退回 drive +download 直连。
        commands = (
            [
                [
                    "drive",
                    "+preview",
                    "--as",
                    "user",
                    "--file-token",
                    token,
                    "--type",
                    "source_file",
                    "--output",
                    f"./{output_path.name}",
                    *(["--overwrite"] if overwrite else []),
                ],
                [
                    "drive",
                    "+download",
                    "--as",
                    "user",
                    "--file-token",
                    token,
                    "--output",
                    f"./{output_path.name}",
                    *(["--overwrite"] if overwrite else []),
                ],
            ]
        )
        envelope = None
        for args in commands:
            try:
                envelope = await self._run(
                    args,
                    timeout_s=_MEDIA_TIMEOUT_S,
                    cwd=str(output_path.parent),
                )
                break
            except Exception:
                continue
        if envelope is None:
            return None

        saved = envelope.data.get("saved_path")
        path = Path(str(saved)) if saved else output_path
        return path if path.is_file() and path.stat().st_size > 0 else None

    # ── 登录态 ──

    async def run_raw(self, args: list[str], *, timeout_s: float = 30.0) -> tuple[int, bytes]:
        """旁路调用:返回 (退出码, stdout),不解析 envelope。

        用于 im +messages-send 等非 envelope 形态命令;与主调用共用
        携带二进制和 HOME 重定向。
        """
        binary = self._bin_path or resolve_cli_bin(env=self._env)
        child_env = {**os.environ, **self._extra_env} if self._extra_env else None
        proc = await asyncio.create_subprocess_exec(
            str(binary),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise
        return proc.returncode or 0, stdout

    async def is_configured(self) -> bool:
        """CLI 是否已完成应用配置(未配置时 auth status 以退出码 3 失败)。"""
        try:
            await self.auth_status()
            return True
        except Exception as exc:
            return "not_configured" not in str(exc) and "not configured" not in str(exc)

    async def configure_app(self, app_id: str, app_secret: str) -> None:
        """非交互完成 lark-cli config init(凭据走 stdin,不进进程列表)。

        Raises:
            CliExecutionError: init 非零退出。
        """
        if not app_id or not app_secret:
            raise ValueError("app_id/app_secret 不能为空")
        binary = self._bin_path or resolve_cli_bin(env=self._env)
        child_env = {**os.environ, **self._extra_env} if self._extra_env else None
        proc = await asyncio.create_subprocess_exec(
            str(binary),
            "config",
            "init",
            "--app-id",
            app_id,
            "--brand",
            "feishu",
            "--app-secret-stdin",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env,
        )
        _, stderr = await asyncio.wait_for(
            proc.communicate(app_secret.encode()), timeout=60
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"config init 失败(退出码 {proc.returncode}): "
                f"{stderr.decode(errors='replace')[:300]}"
            )

    async def auth_status(self) -> AuthStatus:
        obj = await run_lark_cli_json(
            ["auth", "status"],
            timeout_s=self._timeout_s,
            limiter=self._limiter,
            env=self._env,
            bin_path=self._bin_path,
            extra_env=self._extra_env,
        )
        return auth_status_from_dict(obj)

    async def auth_health(self, *, warning_hours: float = 48.0) -> Health:
        status = await self.auth_status()
        return health_of(status, warning_hours=warning_hours)


# re-auth 引导提示:插件已内置 lark-cli 且登录态落在插件数据目录,
# 管理员无需接触宿主机。
REAUTH_HINT = "管理员请在会话中发送 /qa_auth_login 发起扫码授权(或等待授权卡片推送)"


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
