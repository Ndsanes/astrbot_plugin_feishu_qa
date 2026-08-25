"""最小桩:仅供离线单测导入插件模块。"""



class AstrMessageEvent:
    def __init__(
        self,
        message_str="",
        group_id=None,
        sender_id="u1",
        self_id="10000",
        umo="",
    ):
        self.message_str = message_str
        self._group_id = group_id
        self._sender_id = sender_id
        self._self_id = self_id
        self.unified_msg_origin = umo or (
            f"stub_inst:GroupMessage:{group_id}" if group_id else ""
        )
        self.is_wake = False
        self.is_at_or_wake_command = False
        self.stopped = False
        self.sent = []

    def get_group_id(self):
        return self._group_id

    def get_sender_id(self):
        return self._sender_id

    def get_self_id(self):
        return self._self_id


    def set_extra(self, key, value):
        if not hasattr(self, "_extras"):
            self._extras = {}
        self._extras[key] = value

    def get_extra(self, key, default=None):
        return getattr(self, "_extras", {}).get(key, default)

    def get_platform_name(self):
        return getattr(self, "_platform_name", "")

    def stop_event(self):
        self.stopped = True

    def is_stopped(self):
        return self.stopped

    def is_admin(self):
        return getattr(self, "_is_admin_flag", False)

    async def send(self, chain):
        self.sent.append(chain)
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

    class EventMessageType:
        GROUP_MESSAGE = "group"
        PRIVATE_MESSAGE = "private"
