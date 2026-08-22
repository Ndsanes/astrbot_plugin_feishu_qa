class Plain:
    def __init__(self, text="", *a, **k):
        self.text = text


class Image:
    def __init__(self, file=None, **k):
        self.file = file

    @classmethod
    def fromFileSystem(cls, path):
        inst = cls.__new__(cls)
        inst.file = str(path)
        return inst


class Node:
    def __init__(self, uin=0, name="", content=None):
        self.uin = uin
        self.name = name
        self.content = content or []


class Nodes(Node):
    pass
