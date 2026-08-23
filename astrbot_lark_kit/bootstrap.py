"""lark-cli 自举下载 —— vendored 二进制缺失或版本不符时从官方 release 补齐。

职责边界:
- 只做"获取并校验二进制":下载 → sha256 校验 → 解包到 vendor 目录;
- 幂等且版本感知:目标平台已存在、非空、且标记版本与请求一致 → 跳过;
  版本不一致 → 重新下载(不采用"只要非空就永久跳过");
- 不做后台刷新、不做业务逻辑;网络/校验失败抛 CliExecutionError 由调用方降级。

与 cli.py 的关系:cli.py 的 ``find_bundled_cli`` 负责查找,本模块负责补齐,
共同构成 "注入 bin_path > vendored > LARK_CLI_PATH > PATH > 自举下载"
解析链的最后一级。
"""

from __future__ import annotations

import hashlib
import io
import tarfile
import urllib.request
from collections.abc import Callable
from pathlib import Path

from .errors import CliExecutionError

__all__ = [
    "DEFAULT_CLI_VERSION",
    "SUPPORTED_PLATFORMS",
    "bundled_cli_download_url",
    "ensure_bundled_cli",
]

# 与 tools/package_astrbot_zip.sh 打包产物保持一致:只有这两个平台有真实构建物
DEFAULT_CLI_VERSION = "1.0.85"
SUPPORTED_PLATFORMS = ("linux-amd64", "linux-arm64")
_RELEASE_BASE = "https://github.com/larksuite/cli/releases/download/v{version}"
_DOWNLOAD_TIMEOUT_S = 300.0

# 每平台目录下的版本标记文件(幂等判断依据)
_VERSION_MARKER = ".cli_version"

Fetcher = Callable[[str], bytes]


def _default_fetcher(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=_DOWNLOAD_TIMEOUT_S) as resp:  # noqa: S310
        return resp.read()


def bundled_cli_download_url(version: str, plat: str) -> str:
    """指定版本与平台的 lark-cli tarball 官方下载地址。"""
    return f"{_RELEASE_BASE.format(version=version)}/lark-cli-{version}-{plat}.tar.gz"


def _extract_binary(tar_bytes: bytes, dest: Path) -> None:
    """从 tarball 中安全提取 lark-cli 单文件到 dest。

    链接成员、目录、basename 非 lark-cli 的成员一律忽略;
    写入目标是固定 dest,成员路径不参与落盘,
    因此天然免疫路径穿越、绝对路径与符号链接逃逸。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    chosen: tarfile.TarInfo | None = None
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tar:
        for member in tar.getmembers():
            if member.issym() or member.islnk() or not member.isfile():
                continue
            if Path(member.name).name == "lark-cli":
                chosen = member
                break
        if chosen is None:
            raise CliExecutionError("tarball 中未找到 lark-cli 可执行文件")
        src = tar.extractfile(chosen)
        if src is None:
            raise CliExecutionError(f"无法读取 tarball 成员: {chosen.name}")
        dest.write_bytes(src.read())
    dest.chmod(0o755)


def _installed_version(plat_dir: Path) -> str:
    """读取平台目录的版本标记;缺失返回空串。"""
    marker = plat_dir / _VERSION_MARKER
    if marker.is_file():
        return marker.read_text().strip()
    return ""


def ensure_bundled_cli(
    vendor_dir: Path,
    *,
    platforms: list[str] | tuple[str, ...] | None = None,
    version: str = DEFAULT_CLI_VERSION,
    fetch: Fetcher | None = None,
) -> dict[str, Path]:
    """确保 vendor 目录下各平台 lark-cli 就位;返回本次实际下载的 {平台: 路径}。

    - platforms 默认 :data:`SUPPORTED_PLATFORMS`;传入其它值立即报错,
      明确 unsupported,不假装支持;
    - 幂等且版本感知:二进制存在 + 非空 + 标记版本 == version → 跳过;
      否则下载 checksums.txt 校验 sha256 后解包并写版本标记;
    - 任一平台校验失败抛 CliExecutionError,不留半成品(临时文件 + 原子替换);
      已成功平台保留。
    """
    vendor_dir = Path(vendor_dir)
    plats = tuple(platforms) if platforms is not None else SUPPORTED_PLATFORMS
    unsupported = [p for p in plats if p not in SUPPORTED_PLATFORMS]
    if unsupported:
        raise CliExecutionError(
            f"不支持的平台: {', '.join(unsupported)};"
            f"可用平台: {', '.join(SUPPORTED_PLATFORMS)}"
        )

    do_fetch: Fetcher = fetch if fetch is not None else _default_fetcher
    installed: dict[str, Path] = {}

    needed: list[tuple[str, Path]] = []
    for plat in plats:
        plat_dir = vendor_dir / plat
        binary = plat_dir / "lark-cli"
        if (
            binary.is_file()
            and binary.stat().st_size > 0
            and _installed_version(plat_dir) == version
        ):
            # 幂等跳过前仍确保可执行位(某些解压器会丢失权限)
            try:
                binary.chmod(binary.stat().st_mode | 0o111)
            except OSError:
                pass
            continue
        needed.append((plat, binary))
    if not needed:
        return installed

    checksums_text = do_fetch(
        f"{_RELEASE_BASE.format(version=version)}/checksums.txt"
    ).decode("utf-8", errors="replace")
    expected_by_archive: dict[str, str] = {}
    for line in checksums_text.splitlines():
        parts = line.split()
        if len(parts) == 2:
            expected_by_archive[parts[1]] = parts[0]

    for plat, binary in needed:
        archive = f"lark-cli-{version}-{plat}.tar.gz"
        expected = expected_by_archive.get(archive)
        if not expected:
            raise CliExecutionError(f"checksums.txt 中没有 {archive} 的条目")
        tar_bytes = do_fetch(bundled_cli_download_url(version, plat))
        actual = hashlib.sha256(tar_bytes).hexdigest()
        if actual != expected:
            raise CliExecutionError(
                f"{archive} sha256 校验失败: 期望 {expected},实际 {actual}"
            )
        tmp = binary.with_name(binary.name + ".tmp")
        try:
            _extract_binary(tar_bytes, tmp)
            tmp.replace(binary)
        finally:
            tmp.unlink(missing_ok=True)
        binary.parent.joinpath(_VERSION_MARKER).write_text(version)
        installed[plat] = binary
    return installed
