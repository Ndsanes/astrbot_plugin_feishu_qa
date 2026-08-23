"""AstrBot OpenAPI 轻量封装。

从项目根目录 .env 读取配置：
  ASTRBOT_BASE_URL           实例地址
  ASTRBOT_API_KEY            abk_ 开头的 API Key
  ASTRBOT_API_KEY_ISSUED_AT  签发日期 YYYY-MM-DD（有效期 30 天，过期时告警）
  ASTRBOT_DASHBOARD_USERNAME 管理员面板用户名（可选，用于 JWT 登录拿全权限）
  ASTRBOT_DASHBOARD_PASSWORD 管理员面板密码（可选）

API Key 无 logs 等 scope 时（403 Insufficient API key scope），
若配置了管理员账号会自动改走 JWT 登录重试。

用法：
    from astrbot_api import AstrBotClient
    api = AstrBotClient()
    data = api.get("/api/v1/plugins")["data"]
    api.post("/api/v1/plugins/reload", json={...})

命令行快速探测：
    python3 astrbot_api.py plugins
"""

from __future__ import annotations

import base64
import datetime
import json
import os
import re
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

# 日志行内嵌 ANSI 颜色码，CLI 输出前清除
_RE_ANSI = re.compile(r"\x1b\[[0-9;]*m")

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
        self._username = os.environ.get("ASTRBOT_DASHBOARD_USERNAME", "").strip()
        self._password = os.environ.get("ASTRBOT_DASHBOARD_PASSWORD", "")
        self._jwt_token: str = ""
        self._jwt_cache = Path(__file__).parent / ".astrbot_jwt_cache"
        if not self.base_url or not self.api_key:
            raise RuntimeError("缺少 ASTRBOT_BASE_URL / ASTRBOT_API_KEY，请检查 .env")
        self._warn_expiry()

    def _jwt_cache_load(self) -> str:
        """从磁盘缓存读 JWT，过期（留 60s 余量）则丢弃。"""
        try:
            raw = json.loads(self._jwt_cache.read_text(encoding="utf-8"))
            token, exp = str(raw.get("token", "")), float(raw.get("exp", 0))
        except Exception:
            return ""
        if not token or exp <= datetime.datetime.now(datetime.UTC).timestamp() + 60:
            return ""
        return token

    def _jwt_cache_save(self, token: str) -> None:
        """解析 JWT payload 的 exp 后落盘（不校验签名，仅本地缓存）。"""
        exp = 0.0
        try:
            payload_b64 = token.split(".")[1]
            payload_b64 += "=" * (-len(payload_b64) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64))
            exp = float(payload.get("exp", 0))
        except Exception:
            pass
        self._jwt_cache.write_text(
            json.dumps({"token": token, "exp": exp}), encoding="utf-8"
        )

    def login_jwt(self, *, force: bool = False) -> str:
        """管理员账号登录拿 JWT（scopes=["*"]）。

        进程内缓存 + 磁盘缓存（.astrbot_jwt_cache，gitignored）双重复用，
        仅在无可用缓存或 401 强刷时才真正走登录。
        """
        if not force:
            if self._jwt_token:
                return self._jwt_token
            cached = self._jwt_cache_load()
            if cached:
                self._jwt_token = cached
                return cached
        if not self._username or not self._password:
            raise RuntimeError(
                "需要管理员凭据：请在 .env 配置 "
                "ASTRBOT_DASHBOARD_USERNAME / ASTRBOT_DASHBOARD_PASSWORD"
            )
        result = self._raw_request(
            "POST",
            "/api/v1/auth/login",
            json={"username": self._username, "password": self._password},
            use_jwt=False,
        )
        data = result.get("data") or {}
        token = str(data.get("jwt_token") or data.get("token") or "")
        if not token:
            raise RuntimeError(f"登录未返回 jwt_token: {result}")
        self._jwt_token = token
        self._jwt_cache_save(token)
        print("[astrbot_api] 管理员 JWT 登录成功（已缓存）", file=sys.stderr)
        return token

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

    def _raw_request(self, method: str, path: str, *, use_jwt: bool = False, **kwargs) -> dict:
        url = f"{self.base_url}{path}"
        token = self._jwt_token if use_jwt else self.api_key
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
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
            if e.code == 401 and use_jwt:
                # JWT 过期：强制重登一次
                self.login_jwt(force=True)
                return self._raw_request(method, path, use_jwt=True, **kwargs)
            if e.code == 401:
                raise ApiKeyExpired(
                    "401 Token 无效：API Key 可能已过期或被撤销，请更新 .env 中的 ASTRBOT_API_KEY"
                ) from e
            raise RuntimeError(f"{method} {url} -> HTTP {e.code}: {detail}") from e
        return json.loads(payload) if payload else {}

    def request(self, method: str, path: str, **kwargs) -> dict:
        try:
            return self._raw_request(method, path, use_jwt=bool(self._jwt_token), **kwargs)
        except RuntimeError as e:
            # API key 权限不足且有管理员凭据 → 登录后用 JWT 重试一次
            if "HTTP 403" in str(e) and "Insufficient API key scope" in str(e):
                self.login_jwt()
                return self._raw_request(method, path, use_jwt=True, **kwargs)
            raise

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
    if endpoint == "logs":
        # python3 astrbot_api.py logs [过滤关键词] [条数]
        pattern = sys.argv[2] if len(sys.argv) > 2 else ""
        limit = int(sys.argv[3]) if len(sys.argv) > 3 else 100
        result = client.get("/api/v1/logs/history", params={"limit": limit})
        data = result.get("data", result)
        lines = data if isinstance(data, list) else data.get("logs", data.get("list", []))
        for line in lines:
            text = str(line.get("data", line)) if isinstance(line, dict) else str(line)
            text = _RE_ANSI.sub("", text)
            if not pattern or pattern in text:
                print(text)
    else:
        result = client.get(f"/api/v1/{endpoint}")
        names = (
            [p["name"] for p in result.get("data", [])]
            if isinstance(result.get("data"), list)
            else result
        )
        print(json.dumps(names, ensure_ascii=False, indent=2))
