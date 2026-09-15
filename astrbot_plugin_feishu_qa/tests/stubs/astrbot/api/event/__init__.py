"""最小桩:astrbot.api.event,供离线单测导入插件模块。

形状对齐真实 astrbot.core.platform.astr_message_event 中三个插件实际用到的子集:
- 真实签名是 AstrMessageEvent(message_str, message_obj, platform_meta, session_id),
  平台的 __init__ 会 super().__init__ 这四参;测试也按四参构造
  (message_obj=None / platform_meta=None / session_id=...) 以断言 session_id
  与 message_obj.type。
- send_streaming 必须存在于基类:lark_cli 适配器覆写时会先调 super() 记账;
  基类默认实现**不消费**生成器(与真实实现一致),聚合逻辑由子类负责。

注意:本文件被 .gitignore 的 `AstrBot/` 规则(大小写不敏感)一并忽略,
因此**不在版本控制内**,改动后无法用 git 恢复,编辑请谨慎。
"""

from __future__ import annotations


class AstrMessageEvent:
    def __init__(
        self,
        message_str="",
        message_obj=None,
        platform_meta=None,
        session_id="",
        group_id=None,
        sender_id="u1",
        self_id="10000",
        umo="",
        messages=None,
    ):
        self.message_str = message_str
        self.message_obj = message_obj
        self.platform_meta = platform_meta
        self.session_id = session_id
        self._group_id = group_id
        self._sender_id = sender_id
        self._self_id = self_id
        self._messages = messages or []
        self.unified_msg_origin = umo or (
            f"stub_inst:GroupMessage:{group_id}" if group_id else ""
        )
        self.is_wake = False
        self.is_at_or_wake_command = False
        self.stopped = False
        self.role = "member"
        self.sent = []

    def get_messages(self):
        return self._messages

    def get_group_id(self):
        return self._group_id

    def get_sender_id(self):
        return self._sender_id

    def get_self_id(self):
        return self._self_id

    def get_platform_name(self):
        return getattr(self, "_platform_name", "")

    def set_extra(self, key, value):
        if not hasattr(self, "_extras"):
            self._extras = {}
        self._extras[key] = value

    def get_extra(self, key, default=None):
        return getattr(self, "_extras", {}).get(key, default)

    def stop_event(self):
        self.stopped = True

    def is_stopped(self):
        return self.stopped

    def is_admin(self):
        return getattr(self, "_is_admin_flag", False)

    async def send(self, chain):
        self.sent.append(chain)
        return None

    async def send_streaming(self, generator, use_fallback: bool = False) -> None:
        """基类默认实现:不消费生成器(与真实实现一致,见 astr_message_event.py)。

        子类覆写后自行消费;真实基类只做记账,此处保留空实现以免子类 super()
        调用失败。
        """
        return None

    def plain_result(self, text):
        return ("plain", text)


class MessageChain:
    def __init__(self, chain=None):
        self.chain = chain or []

    def message(self, text):
        self.chain.append(("plain", text))
        return self

    def file_image(self, path):
        self.chain.append(("image", path))
        return self


class MessageEventResult(MessageChain):
    """真实 API 中继承 MessageChain 的结果载体(工具直发契约用)。"""


class filter:
    @staticmethod
    def command(*a, **k):
        def deco(fn):
            fn._filter = ("command", a, k)
            return fn

        return deco

    @staticmethod
    def event_message_type(*a, **k):
        def deco(fn):
            fn._filter = ("emt", a, k)
            return fn

        return deco

    @staticmethod
    def llm_tool(name=None, **k):
        def deco(fn):
            fn._llm_tool_name = name or fn.__name__
            fn._filter = ("llm_tool", name, k)
            return fn

        return deco

    @staticmethod
    def on_llm_request(*a, **k):
        def deco(fn):
            fn._filter = ("on_llm_request", a, k)
            return fn

        return deco

    @staticmethod
    def on_llm_tool_respond(*a, **k):
        def deco(fn):
            fn._filter = ("on_llm_tool_respond", a, k)
            return fn

        return deco

    class EventMessageType:
        GROUP_MESSAGE = "group"
        PRIVATE_MESSAGE = "private"
