"""官方手册 PDF 的每周更新检查(插件侧,零第三方依赖)。

只**检查**、不重建。重建需要 pymupdf 与 ``tools/knowledge`` 流水线,那部分
由 ``kb_sync.py`` 在装了依赖的机器/CI 上执行;本模块只负责在服务器上每周
HEAD 一次官方 PDF:

- ETag/大小与基线一致 → up_to_date
- 不一致 → update_available,WARNING 日志 + ``/qa_status`` 显示"待同步"
- 首次运行只记录基线,不报警

这样既不把 PDF 解析依赖塞进机器人,也不会因为开发机没开机而错过更新。
基线只在**重建成功**后由 ``kb_sync.py`` 推进;插件这里不擅自推进,避免
"看到了更新但没同步"却再也不再提醒。
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

OFFICIAL_PDF_URL = (
    "https://bandlab.github.io/cakewalk/docs/Cakewalk%20Sonar%20Reference%20Guide.pdf"
)
STATE_FILENAME = "kb_watch.json"
_USER_AGENT = "Mozilla/5.0 (compatible; astrbot-plugin-feishu-qa)"
DEFAULT_TIMEOUT = 20.0


@dataclass
class RemoteInfo:
    url: str
    bytes: int = 0
    etag: str = ""
    last_modified: str = ""


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def remote_info(url: str = OFFICIAL_PDF_URL, *, timeout: float = DEFAULT_TIMEOUT) -> RemoteInfo:
    """HEAD 官方 PDF,取大小与校验头。服务器不支持 HEAD 时抛异常。"""
    request = urllib.request.Request(
        url, method="HEAD", headers={"User-Agent": _USER_AGENT}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        headers = response.headers
        return RemoteInfo(
            url=url,
            bytes=int(headers.get("Content-Length") or 0),
            etag=(headers.get("ETag") or "").strip('"'),
            last_modified=headers.get("Last-Modified") or "",
        )


def load_state(path: Path) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(path: Path, state: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _same(left: dict, right: RemoteInfo) -> bool:
    """ETag 优先;没有 ETag 的服务器退回"大小 + 修改时间"比较。"""
    if left.get("etag") and right.etag:
        return str(left["etag"]) == right.etag and int(left.get("bytes") or 0) == right.bytes
    return (
        int(left.get("bytes") or 0) == right.bytes
        and str(left.get("last_modified") or "") == right.last_modified
    )


def evaluate(state: dict, remote: RemoteInfo) -> dict:
    """把远端信息与基线比较,返回新状态(纯函数,不落盘)。"""
    now = utc_now()
    result = dict(state or {})
    result["url"] = remote.url
    result["checked_at"] = now

    baseline = state.get("baseline") or {}
    if not (baseline.get("etag") or baseline.get("bytes")):
        result["baseline"] = {
            "etag": remote.etag,
            "bytes": remote.bytes,
            "last_modified": remote.last_modified,
            "at": now,
        }
        result["status"] = "baseline"
        result["pending"] = None
        return result

    if _same(baseline, remote):
        result["status"] = "up_to_date"
        result["pending"] = None
        return result

    result["status"] = "update_available"
    result["pending"] = {
        "etag": remote.etag,
        "bytes": remote.bytes,
        "last_modified": remote.last_modified,
        "detected_at": now,
    }
    return result


def check_once(
    state_path: Path, url: str = OFFICIAL_PDF_URL, *, timeout: float = DEFAULT_TIMEOUT
) -> dict:
    """HEAD + 比较 + 落盘。同步函数,调用方用 ``asyncio.to_thread`` 包一层。"""
    state = load_state(state_path)
    remote = remote_info(url, timeout=timeout)
    new_state = evaluate(state, remote)
    save_state(state_path, new_state)
    return {
        "status": new_state.get("status"),
        "remote": {
            "bytes": remote.bytes,
            "etag": remote.etag,
            "last_modified": remote.last_modified,
        },
        "baseline": new_state.get("baseline"),
        "pending": new_state.get("pending"),
    }


def status_line(state: dict | None) -> str:
    """给 ``/qa_status`` 的一行摘要。"""
    if not state:
        return "手册 PDF: 尚未检查"
    status = str(state.get("status") or "")
    if status == "update_available":
        pending = state.get("pending") or {}
        stamp = pending.get("last_modified") or pending.get("detected_at") or "未知"
        return f"手册 PDF: 有更新待同步({stamp})"
    baseline = state.get("baseline") or {}
    at = baseline.get("at") or state.get("checked_at") or "未知"
    return f"手册 PDF: 已是最新(基线 {at})"


def mark_notified(state_path: Path, *, when: str | None = None) -> dict:
    """标记"这次更新已经通知过",避免每周重复轰炸。

    远端再次变化时 ``evaluate`` 会生成新的 pending(没有 notified_at),
    因此下一轮仍会通知。
    """
    state = load_state(state_path)
    pending = state.get("pending")
    if isinstance(pending, dict):
        pending["notified_at"] = when or utc_now()
        save_state(state_path, state)
    return state
