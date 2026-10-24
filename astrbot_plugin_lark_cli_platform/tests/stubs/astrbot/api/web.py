"""最小桩:astrbot.api.web(插件 Pages 的后端接口),仅供离线单测。

真实实现在 astrbot/api/web.py:handler 里 ``from astrbot.api.web import request``
拿到的是当前请求上下文代理,``json_response`` 返回 JSONResponse。
"""

from __future__ import annotations


class _Request:
    def __init__(self) -> None:
        self.payload = {}
        self.username = "tester"

    async def json(self, default=None):
        return self.payload if self.payload is not None else default


request = _Request()


def json_response(data=None, **kwargs):
    return data


def error_response(message, **kwargs):
    return {"status": "error", "message": message, "data": kwargs.get("data")}
