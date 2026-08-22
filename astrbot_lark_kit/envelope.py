"""lark-cli JSON envelope 解析。

lark-cli 统一输出结构:
    成功: {"ok": true, "identity": "user", "data": {...}}
    失败: {"ok": false, "error": {"type": "...", "subtype": "...",
                                  "code": 123, "message": "...", "log_id": "..."}}
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .errors import CliInvalidOutputError

# error type/subtype 中出现这些子串时判定为登录态问题(保守匹配,避免把普通
# permission 问题误判成 auth)。
_AUTH_MARKERS = ("auth", "login", "unauthorized", "credential")


@dataclass
class LarkCliErrorInfo:
    """envelope.error 的规范化视图。"""

    type: str = ""
    subtype: str = ""
    code: int | str = ""
    message: str = ""
    log_id: str = ""

    def is_auth_related(self) -> bool:
        haystack = f"{self.type} {self.subtype}".lower()
        if any(marker in haystack for marker in _AUTH_MARKERS):
            return True
        lowered_msg = self.message.lower()
        return any(
            phrase in lowered_msg
            for phrase in ("not logged in", "not logged on", "please login")
        )


@dataclass
class LarkEnvelope:
    """解析后的 lark-cli 输出。"""

    ok: bool
    identity: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    error: LarkCliErrorInfo = field(default_factory=LarkCliErrorInfo)

    @property
    def document(self) -> dict[str, Any]:
        """docs +fetch 等命令把业务载荷放在 data.document 下。"""
        payload = self.data.get("document")
        return payload if isinstance(payload, dict) else {}


def parse_envelope(raw: str) -> LarkEnvelope:
    """把 stdout 文本解析为 LarkEnvelope。

    Raises:
        CliInvalidOutputError: 不是合法 JSON,或顶层结构不是对象/缺少 ok 字段。
    """
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CliInvalidOutputError(f"stdout 不是合法 JSON: {exc}") from exc

    if not isinstance(obj, dict) or not isinstance(obj.get("ok"), bool):
        raise CliInvalidOutputError("envelope 缺少布尔 ok 字段")

    identity = obj.get("identity")
    data = obj.get("data")
    error_raw = obj.get("error")

    error = LarkCliErrorInfo()
    if isinstance(error_raw, dict):
        error = LarkCliErrorInfo(
            type=str(error_raw.get("type") or ""),
            subtype=str(error_raw.get("subtype") or ""),
            code=error_raw.get("code", ""),
            message=str(error_raw.get("message") or ""),
            log_id=str(error_raw.get("log_id") or ""),
        )

    return LarkEnvelope(
        ok=obj["ok"],
        identity=identity if isinstance(identity, str) else None,
        data=data if isinstance(data, dict) else {},
        error=error,
    )


def parse_envelope_bytes(raw: bytes) -> LarkEnvelope:
    return parse_envelope(raw.decode("utf-8", errors="replace"))

