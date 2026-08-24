"""登录态目录解析 —— kit 的 state 基础能力。

统一约定:各插件数据目录下的 ``lark_cli_home/`` 即 lark-cli 登录态 HOME
(经子进程 HOME 环境变量注入),随数据卷持久化、容器重建不丢。
登录态只归属本插件,不跨插件共享。

边界:只做路径解析/创建与 bot 凭据播种;不做 token 刷新、后台提醒、消息收发。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

__all__ = [
    "LARK_CLI_HOME_DIRNAME",
    "ensure_bot_credentials",
    "ensure_short_home",
    "resolve_state_home",
]

LARK_CLI_HOME_DIRNAME = "lark_cli_home"

_AF_UNIX_PATH_LIMIT = 108
# lark-cli 事件总线在 ``<HOME>/.lark-cli/events/<appId>/bus.sock`` 建 Unix socket;
# Linux 对 AF_UNIX 路径有 108 字节硬限制,超出即 bind 失败 → 守护进程无法就绪。
# 余量 = "/.lark-cli/events/"(18) + appId(≤32) + "/bus.sock"(9)
_SOCKET_PATH_RESERVE = len("/.lark-cli/events/") + 32 + len("/bus.sock")
_ALIAS_ROOT_DEFAULT = Path("/tmp")


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


def ensure_bot_credentials(
    state_home: str | Path,
    *,
    app_id: str,
    app_secret: str,
) -> bool:
    """把 bot 凭据(应用 app_id + app_secret)同步进登录态目录。

    平台配置是凭据的唯一权威来源:每次启动以配置为准,与目录中已有内容做
    比对,appId 或 secret 发生变化即覆盖重写;一致则不动(lark-cli 侧的
    TAT/用户令牌不受影响)。lark-cli 侧用 file 型 secret 源:``config.json``
    指向一个 0600 权限的明文 secret 文件。

    Args:
        state_home: 插件登录态 HOME 目录(即子进程 HOME)。
        app_id: 飞书应用 App ID(cli_xxx)。
        app_secret: 飞书应用 App Secret。

    Returns:
        True 表示配置就绪(本次写入或本已一致);False 表示缺少凭据输入。
    """
    home = Path(state_home)
    config_path = home / ".lark-cli" / "config.json"
    if not app_id.strip() or not app_secret.strip():
        return False
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
            apps = config.get("apps") or []
            if apps and apps[0].get("appId") == app_id.strip():
                secret_file = Path(str((apps[0].get("appSecret") or {}).get("id", "")))
                try:
                    if secret_file.read_text(encoding="utf-8").strip() == app_secret.strip():
                        return True  # 与配置完全一致,无需改动
                except OSError:
                    pass  # secret 文件缺失/不可读:按需重建
        except (OSError, ValueError):
            pass  # 损坏文件按缺失处理,重建
    cli_dir = home / ".lark-cli"
    cli_dir.mkdir(parents=True, exist_ok=True)
    secret_file = cli_dir / ".app_secret"
    secret_file.write_text(app_secret.strip(), encoding="utf-8")
    secret_file.chmod(0o600)
    config = {
        "apps": [
            {
                "appId": app_id.strip(),
                "appSecret": {"source": "file", "id": str(secret_file)},
                "brand": "feishu",
                "users": [],
            }
        ]
    }
    config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    return True

def ensure_short_home(
    state_home: str | Path,
    *,
    alias_root: Path = _ALIAS_ROOT_DEFAULT,
    limit: int = _AF_UNIX_PATH_LIMIT,
) -> Path:
    """登录态目录路径过深时返回短别名(symlink),否则原样返回。

    背景:数据卷挂载点普遍很深(如 ``/AstrBot/data/plugin_data/<插件>/lark_cli_home``),
    其下事件总线 socket 全长会超过 AF_UNIX 108 字节内核上限,lark-cli 事件消费
    进程因此稳定报 ``did not become ready within 3s``(exit code 5)。bind 的长度
    检查只针对传入字符串,不解析 symlink,故用 ``/tmp`` 下定名别名即可绕开,
    物理数据仍全部留在插件数据目录。别名幂等创建、按目标路径哈希命名。

    Args:
        state_home: 插件登录态 HOME 真实目录。
        alias_root: 别名所在目录(默认 /tmp;测试可注入 tmp_path)。
        limit: AF_UNIX 路径上限(默认 108,Linux 内核值)。

    Returns:
        可直接作为子进程 HOME 的路径(原目录或短别名)。
    """
    home = Path(state_home)
    if len(str(home)) + _SOCKET_PATH_RESERVE <= limit:
        return home
    digest = hashlib.sha256(str(home).encode()).hexdigest()[:12]
    alias = alias_root / f"lark_home_{digest}"
    try:
        alias_root.mkdir(parents=True, exist_ok=True)
        if alias.is_symlink():
            if os.readlink(alias) == str(home):
                return alias
            alias.unlink()
        elif alias.exists():  # 同名非链接:可能是历史残留的普通目录
            import shutil

            shutil.rmtree(alias)
        alias.symlink_to(home, target_is_directory=True)
    except OSError:
        # 别名建不出来(如只读 /tmp):只能退回原路径;短路径环境不会走到这里
        return home
    return alias
