"""最小桩:astrbot.api.message_components,仅供离线单测导入插件模块。

形状对齐真实 astrbot.core.message.components 中两个插件实际用到的子集;
astrbot_plugin_lark_cli_platform 与 astrbot_plugin_feishu_qa 的测试桩必须
保持同形(sys.modules 共享,任一侧缺属性都会污染另一侧的用例)。
"""


class Plain:
    type = "Plain"
    def __init__(self, text="", *a, **k):
        self.text = text


class Image:
    type = "Image"
    def __init__(self, file=None, **k):
        self.file = file

    @classmethod
    def fromFileSystem(cls, path):
        inst = cls.__new__(cls)
        inst.file = str(path)
        return inst

    @classmethod
    def fromURL(cls, url):
        inst = cls.__new__(cls)
        inst.file = ""
        inst.url = str(url)
        return inst

    async def convert_to_file_path(self):
        """真实组件返回本地缓存路径;桩直接回传 file。"""
        return self.file


class Node:
    type = "Node"
    def __init__(self, uin=0, name="", content=None):
        self.uin = uin
        self.name = name
        self.content = content or []


class Nodes(Node):
    pass


class Reply:
    """引用消息段。真实字段见 astrbot.core.message.components.Reply。"""

    type = "Reply"

    def __init__(self, id="", chain=None, message_str="", text="", **k):
        self.id = id
        self.chain = chain or []
        self.message_str = message_str
        self.text = text
