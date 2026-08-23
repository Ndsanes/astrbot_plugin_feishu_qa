"""AstrBot OpenAPI 轻量封装。

从项目根目录 .env 读取配置：
  ASTRBOT_BASE_URL          实例地址
  ASTRBOT_API_KEY           abk_ 开头的 API Key
  ASTRBOT_API_KEY_ISSUED_AT 签发日期 YYYY-MM-DD（有效期 30 天，过期时告警）

用法：
    from astrbot_api import AstrBotClient
    api = AstrBotClient()
    data = api.get("/api/v1/plugins")["data"]
    api.post("/api/v1/plugins/reload", json={...})

命令行快速探测：
    python3 astrbot_api.py plugins
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

KEY_TTL_DAYS = 30


class ApiKeyExpired(PermissionError):
    pass


def _load_env(path: Path | None = None) -> None:
    """极简 .env 加载（不覆盖已有环境变量）。"""
    env_path = path or Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def _days_until_expiry() -> int | None:
    issued = os.environ.get("ASTRBOT_API_KEY_ISSUED_AT", "").strip()
    if not issued:
        return None
    try:
        issued_at = datetime.date.fromisoformat(issued)
    except ValueError:
        return None
    return KEY_TTL_DAYS - (datetime.date.today() - issued_at).days


class AstrBotClient:
    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        _load_env()
        self.base_url = (base_url or os.environ.get("ASTRBOT_BASE_URL", "")).rstrip("/")
        self.api_key = api_key or os.environ.get("ASTRBOT_API_KEY", "")
        if not self.base_url or not self.api_key:
            raise RuntimeError("缺少 ASTRBOT_BASE_URL / ASTRBOT_API_KEY，请检查 .env")
        self._warn_expiry()

    def _warn_expiry(self) -> None:
        remaining = _days_until_expiry()
        if remaining is None:
            print("[astrbot_api] 未设置 ASTRBOT_API_KEY_ISSUED_AT，无法跟踪有效期", file=sys.stderr)
        elif remaining <= 0:
            print(
                f"[astrbot_api] ⚠️ API Key 已过期 {abs(remaining)} 天，"
                "请更新 .env 中的 ASTRBOT_API_KEY",
                file=sys.stderr,
            )
        elif remaining <= 7:
            print(
                f"[astrbot_api] 提醒：API Key 将在 {remaining} 天内过期，请更新 .env",
                file=sys.stderr,
            )

    def request(self, method: str, path: str, **kwargs) -> dict:
        url = f"{self.base_url}{path}"
        headers = {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}
        headers.update(kwargs.pop("headers", {}))
        data = kwargs.pop("json", None)
        body = json.dumps(data).encode() if data is not None else None
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=kwargs.pop("timeout", 30)) as resp:
                payload = resp.read().decode()
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            if e.code == 401:
                raise ApiKeyExpired(
                    "401 Token 无效：API Key 可能已过期或被撤销，请更新 .env 中的 ASTRBOT_API_KEY"
                ) from e
            raise RuntimeError(f"{method} {url} -> HTTP {e.code}: {detail}") from e
        return json.loads(payload) if payload else {}

    def get(self, path: str, **kwargs) -> dict:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs) -> dict:
        return self.request("POST", path, **kwargs)

    def upload_file(self, path: str, file_path: str, fields: dict | None = None) -> dict:
        """multipart/form-data 上传(如 POST /api/v1/plugins/install/upload)。"""
        boundary = f"----astrbotapi{uuid.uuid4().hex}"
        parts: list[bytes] = []
        for name, value in (fields or {}).items():
            parts.append(
                (
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                    f"{value}\r\n"
                ).encode(),
            )
        file_name = os.path.basename(file_path)
        payload = Path(file_path).read_bytes()
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="file"; filename="{file_name}"\r\n'
                "Content-Type: application/zip\r\n\r\n"
            ).encode()
            + payload
            + b"\r\n",
        )
        parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(parts)
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                result = json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            raise RuntimeError(f"POST {url} -> HTTP {e.code}: {detail}") from e
        if isinstance(result, dict) and result.get("status") not in (None, "success", "ok"):
            raise RuntimeError(f"POST {url} -> {result}")
        return result


if __name__ == "__main__":
    client = AstrBotClient()
    endpoint = sys.argv[1] if len(sys.argv) > 1 else "plugins"
    result = client.get(f"/api/v1/{endpoint}")
    names = (
        [p["name"] for p in result.get("data", [])]
        if isinstance(result.get("data"), list)
        else result
    )
    print(json.dumps(names, ensure_ascii=False, indent=2))
