"""部署布局回归测试:模拟 AstrBot 运行时的插件加载方式。

AstrBot 把插件作为 `data.plugins.<目录名>.main` 导入,且 `data/plugins`
不在 sys.path 上。本测试在临时目录重建该布局(插件 + vendored kit +
astrbot stub),验证两个场景:
1. 顶层无 astrbot_lark_kit 可用时(裸实例,未 pip 安装),
   插件走 vendored 副本兜底且正常导入;
2. 顶层存在 astrbot_lark_kit 时(pip 安装版,合法解析层级),
   插件同样正常导入——kit 来源允许安装版或 vendored 二选一。

这是"能否导入 AstrBot 实例"的验收门槛,改动导入结构时必须保持通过。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest

PLUGIN_SRC = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = PLUGIN_SRC.parent.parent

_COPY_IGNORE = shutil.ignore_patterns(
    "__pycache__",
    "*.pyc",
    ".DS_Store",
    ".pytest_cache",
    "tests",
    "dist",
)


class _BlockTopLevelKit:
    """meta_path 钩子:让顶层 `import astrbot_lark_kit` 失败,
    模拟"裸实例上未安装 kit"的真实部署场景。"""

    def find_spec(self, fullname: str, path=None, target=None):  # noqa: ANN001
        if fullname == "astrbot_lark_kit" or fullname.startswith("astrbot_lark_kit."):
            raise ImportError("顶层 astrbot_lark_kit 已被测试钩子屏蔽")
        return None


def _block_installed_kit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "meta_path", [_BlockTopLevelKit(), *sys.meta_path])

WORKSPACE_ROOT = PLUGIN_SRC.parent

def _purge_modules(*prefixes: str) -> dict[str, ModuleType]:
    """移除匹配前缀的模块并返回备份,供测试后恢复。"""
    saved: dict[str, ModuleType] = {}
    for name in list(sys.modules):
        if any(name == p or name.startswith(p + ".") for p in prefixes):
            saved[name] = sys.modules.pop(name)
    return saved


@pytest.fixture()
def deployed_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """构建 AstrBot 运行时布局并隔离工作区路径。"""
    root = tmp_path / "astrbot_root"
    plugin_dest = root / "data" / "plugins" / "astrbot_plugin_feishu_qa"

    # 插件本体(排除 tests/,避免把 stubs 当成插件内容)
    shutil.copytree(PLUGIN_SRC, plugin_dest, ignore=_COPY_IGNORE)
    # kit vendored 子包
    kit_src = WORKSPACE_ROOT / "astrbot_lark_kit"
    shutil.copytree(kit_src, plugin_dest / "astrbot_lark_kit", ignore=_COPY_IGNORE)
    # astrbot stub(真实实例上是 venv 里的 astrbot 包)
    stub_src = PLUGIN_SRC / "tests" / "stubs" / "astrbot"
    shutil.copytree(stub_src, root / "astrbot")

    # 隔离:去掉工作区根路径与已加载的包模块,确保走的是部署解析路径
    monkeypatch.setattr(
        sys,
        "path",
        [p for p in sys.path if p and Path(p).resolve() != WORKSPACE_ROOT],
    )
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.chdir(tmp_path)
    saved = _purge_modules("astrbot_plugin_feishu_qa", "astrbot_lark_kit", "data")
    yield root
    _purge_modules("astrbot_plugin_feishu_qa", "astrbot_lark_kit", "data")
    for name, mod in saved.items():
        sys.modules[name] = mod


async def test_vendored_fallback_without_top_level_kit(
    deployed_layout: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """裸实例(顶层无 kit):插件必须走 vendored 副本并正常导入。"""
    import importlib

    _block_installed_kit(monkeypatch)

    module = importlib.import_module("data.plugins.astrbot_plugin_feishu_qa.main")
    assert hasattr(module, "FeishuQaPlugin")

    kit = sys.modules["data.plugins.astrbot_plugin_feishu_qa.astrbot_lark_kit"]
    assert str(deployed_layout) in kit.__file__
    assert "data/plugins/astrbot_plugin_feishu_qa/astrbot_lark_kit" in (
        kit.__file__.replace("\\", "/")
    )


async def test_imports_with_top_level_kit_available(deployed_layout: Path) -> None:
    """顶层存在 kit(pip 安装版):合法解析层级,插件正常导入即可。

    kit 实际来源允许安装版(site-packages)或 vendored 副本——取决于运行
    环境是否 pip 安装过;唯一不允许的是导入失败。
    """
    import importlib

    module = importlib.import_module("data.plugins.astrbot_plugin_feishu_qa.main")
    assert hasattr(module, "FeishuQaPlugin")

    vendored = sys.modules.get(
        "data.plugins.astrbot_plugin_feishu_qa.astrbot_lark_kit"
    )
    top_level = sys.modules.get("astrbot_lark_kit")
    assert vendored is not None or top_level is not None


