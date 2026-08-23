"""登录态目录解析 —— kit 的 state 基础能力。

统一约定:各插件数据目录下的 ``lark_cli_home/`` 即 lark-cli 登录态 HOME
(经子进程 HOME 环境变量注入),随数据卷持久化、容器重建不丢。

边界:只做路径解析与创建;不做 token 刷新、不做后台提醒、不含业务逻辑。
同一 app 的登录态可被多个插件共享(指向同一 state_home 即可)。
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["LARK_CLI_HOME_DIRNAME", "resolve_state_home"]

LARK_CLI_HOME_DIRNAME = "lark_cli_home"


def resolve_state_home(plugin_data_dir: str | Path, *, create: bool = True) -> Path:
    """把插件数据目录解析为 lark-cli 登录态 HOME 路径。

    Args:
        plugin_data_dir: 插件数据根目录。
        create: 是否确保目录存在(默认创建)。

    Returns:
        ``<plugin_data_dir>/lark_cli_home`` 的绝对路径。
    """
    home = (Path(plugin_data_dir) / LARK_CLI_HOME_DIRNAME).resolve()
    if create:
        home.mkdir(parents=True, exist_ok=True)
    return home
