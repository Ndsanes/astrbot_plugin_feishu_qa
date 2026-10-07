"""配置 schema 验收测试。

AstrBot 的 ``_parse_schema`` 只认一组固定类型,遇到不支持的类型会**直接抛
TypeError 让整个插件载入失败**:

    TypeError: 不受支持的配置类型 dict。
    支持的类型有：['int','float','bool','string','text','list','file','object','template_list']

2026-10-07 事故:把 ``FUUUMUSIC_SECTIONS`` 从 list 改成 dict(想做成"键值对勾选")
后插件在线上再也载不起来,而且**只有当 AstrBot 重新构造插件配置时才暴露**——
部署当时看起来一切正常,直到重建容器才炸。本测试就是在本地把这道门禁补上。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "_conf_schema.json"

# 抄自 AstrBot core/config/astrbot_config.py::_parse_schema
SUPPORTED_TYPES = {
    "int",
    "float",
    "bool",
    "string",
    "text",
    "list",
    "file",
    "object",
    "template_list",
}


@pytest.fixture(scope="module")
def schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


def test_schema_is_valid_json_object(schema: dict) -> None:
    assert isinstance(schema, dict) and schema, "配置 schema 必须是非空对象"


def test_every_type_is_supported_by_astrbot(schema: dict) -> None:
    bad = {
        key: node.get("type")
        for key, node in schema.items()
        if isinstance(node, dict) and node.get("type") not in SUPPORTED_TYPES
    }
    assert not bad, f"这些配置项用了 AstrBot 不支持的类型(会导致插件载入失败): {bad}"


def test_every_entry_has_description_and_default(schema: dict) -> None:
    missing = [
        key
        for key, node in schema.items()
        if not isinstance(node, dict) or "description" not in node or "default" not in node
    ]
    assert not missing, f"配置项缺少 description/default: {missing}"


_EXPECTED_PYTHON_TYPE: dict[str, object] = {
    "list": list,
    "bool": bool,
    "int": (int, float),
    "float": (int, float),
    "string": str,
    "text": str,
    "object": dict,
}


def test_default_values_match_declared_type(schema: dict) -> None:
    mismatched = []
    for key, node in schema.items():
        if not isinstance(node, dict):
            continue
        typ = node.get("type")
        expected = _EXPECTED_PYTHON_TYPE.get(typ)
        if expected is None:
            continue
        default = node.get("default")
        # bool 是 int 的子类,必须单独判,否则 True 会被当成合法的 int 默认值。
        ok = isinstance(default, bool) if typ == "bool" else isinstance(default, expected)
        if not ok:
            mismatched.append((key, typ, type(default).__name__))
    assert not mismatched, f"默认值与声明类型不符: {mismatched}"
