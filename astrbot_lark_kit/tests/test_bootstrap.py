"""bootstrap.py 自举下载测试:全部零网络,fetcher 注入内存字节。"""

from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path

import pytest

from astrbot_lark_kit.bootstrap import (
    DEFAULT_CLI_VERSION,
    SUPPORTED_PLATFORMS,
    ensure_bundled_cli,
)
from astrbot_lark_kit.errors import CliExecutionError

VERSION = DEFAULT_CLI_VERSION


def make_tar_bytes(
    member_name: str = "pkg/lark-cli", content: bytes = b"#!/bin/sh\necho hi\n"
) -> bytes:
    """构造内存 tarball(单个成员)。"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name=member_name)
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def checksums_for(version: str, tar_by_plat: dict[str, bytes]) -> str:
    lines = [
        f"{hashlib.sha256(tb).hexdigest()}  lark-cli-{version}-{plat}.tar.gz"
        for plat, tb in tar_by_plat.items()
    ]
    return "\n".join(lines) + "\n"


def make_fetcher(tar_by_plat: dict[str, bytes], version: str = VERSION):
    """构造注入 fetcher:checksums.txt 按 tar 内容实时计算,其余按平台返回 tarball。"""

    def fetch(url: str) -> bytes:
        if url.endswith("checksums.txt"):
            return checksums_for(version, tar_by_plat).encode()
        archive = url.rsplit("/", 1)[-1]
        if archive == f"lark-cli-{version}-linux-amd64.tar.gz":
            return tar_by_plat["linux-amd64"]
        if archive == f"lark-cli-{version}-linux-arm64.tar.gz":
            return tar_by_plat["linux-arm64"]
        raise AssertionError(f"意外请求: {url}")

    return fetch


def wrap_counting(fetch, calls: list):
    def inner(url: str) -> bytes:
        calls.append(url)
        return fetch(url)

    return inner


def test_正常下载并落盘(tmp_path: Path):
    installed = ensure_bundled_cli(
        tmp_path,
        platforms=("linux-amd64",),
        version=VERSION,
        fetch=make_fetcher({"linux-amd64": make_tar_bytes()}),
    )
    binary = tmp_path / "lark-cli" / "linux-amd64" / "lark-cli"
    assert installed == {"linux-amd64": binary}
    assert binary.is_file() and binary.stat().st_size > 0
    assert (tmp_path / "lark-cli" / "linux-amd64" / ".cli_version").read_text() == VERSION


def test_sha256校验失败抛错且不留半成品(tmp_path: Path):
    bad = make_tar_bytes()

    def fetch(url: str) -> bytes:
        if url.endswith("checksums.txt"):
            wrong = "0" * 64
            return f"{wrong}  lark-cli-{VERSION}-linux-amd64.tar.gz\n".encode()
        return bad

    with pytest.raises(CliExecutionError, match="sha256"):
        ensure_bundled_cli(
            tmp_path, platforms=("linux-amd64",), version=VERSION, fetch=fetch
        )
    assert not (tmp_path / "lark-cli" / "linux-amd64" / "lark-cli").exists()


def test_archive缺目标文件报错(tmp_path: Path):
    empty = make_tar_bytes(member_name="README.md")
    with pytest.raises(CliExecutionError, match="未找到"):
        ensure_bundled_cli(
            tmp_path,
            platforms=("linux-amd64",),
            version=VERSION,
            fetch=make_fetcher({"linux-amd64": empty}),
        )


def test路径穿越与绝对路径成员被免疫(tmp_path: Path):
    # 恶意成员(../、/abs、symlink)全部忽略;只有嵌套的真身 lark-cli 被提取;
    # 写入目标固定为 vendor 目录内,任何成员路径都不参与落盘。
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in ("../evil.sh", "/abs/evil.sh"):
            info = tarfile.TarInfo(name=name)
            info.size = 3
            tar.addfile(info, io.BytesIO(b"bad"))
        link = tarfile.TarInfo(name="link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tar.addfile(link)
        info = tarfile.TarInfo(name="pkg/lark-cli")
        info.size = len(b"ok")
        tar.addfile(info, io.BytesIO(b"ok"))

    installed = ensure_bundled_cli(
        tmp_path,
        platforms=("linux-amd64",),
        version=VERSION,
        fetch=make_fetcher({"linux-amd64": buf.getvalue()}),
    )
    binary = installed["linux-amd64"]
    assert binary.read_bytes() == b"ok"
    plat_dir = tmp_path / "lark-cli" / "linux-amd64"
    assert sorted(p.name for p in plat_dir.iterdir()) == [".cli_version", "lark-cli"]
    assert not (tmp_path / "evil.sh").exists()
    assert not (tmp_path.parent / "evil.sh").exists()


def test符号链接成员被忽略仍能取到真身(tmp_path: Path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        link = tarfile.TarInfo(name="lark-cli-link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tar.addfile(link)
        info = tarfile.TarInfo(name="real/lark-cli")
        info.size = len(b"real-bin")
        tar.addfile(info, io.BytesIO(b"real-bin"))
    installed = ensure_bundled_cli(
        tmp_path,
        platforms=("linux-arm64",),
        version=VERSION,
        fetch=make_fetcher({"linux-arm64": buf.getvalue()}),
    )
    assert installed["linux-arm64"].read_bytes() == b"real-bin"


def test幂等跳过_已存在且版本一致(tmp_path: Path):
    calls: list[str] = []
    counted = wrap_counting(make_fetcher({"linux-amd64": make_tar_bytes()}), calls)

    ensure_bundled_cli(
        tmp_path, platforms=("linux-amd64",), version=VERSION, fetch=counted
    )
    installed = ensure_bundled_cli(
        tmp_path, platforms=("linux-amd64",), version=VERSION, fetch=counted
    )
    assert installed == {}  # 第二次零下载
    assert len(calls) == 2  # 仅第一次: checksums + tarball


def test二进制存在但版本标记缺失时重新下载(tmp_path: Path):
    binary = tmp_path / "lark-cli" / "linux-amd64" / "lark-cli"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"stale-but-nonempty")

    calls: list[str] = []
    counted = wrap_counting(
        make_fetcher({"linux-amd64": make_tar_bytes(content=b"fresh")}), calls
    )

    installed = ensure_bundled_cli(
        tmp_path, platforms=("linux-amd64",), version=VERSION, fetch=counted
    )
    assert installed and installed["linux-amd64"].read_bytes() == b"fresh"
    assert any("checksums.txt" in c for c in calls)


def test版本变化触发重新下载(tmp_path: Path):
    other = "1.0.87"
    ensure_bundled_cli(
        tmp_path,
        platforms=("linux-amd64",),
        version=VERSION,
        fetch=make_fetcher({"linux-amd64": make_tar_bytes(content=b"v85")}),
    )

    tar87 = make_tar_bytes(content=b"v87")

    def fetch(url: str) -> bytes:
        if url.endswith("checksums.txt"):
            return checksums_for(other, {"linux-amd64": tar87}).encode()
        return tar87

    installed = ensure_bundled_cli(
        tmp_path, platforms=("linux-amd64",), version=other, fetch=fetch
    )
    assert installed["linux-amd64"].read_bytes() == b"v87"
    assert (tmp_path / "lark-cli" / "linux-amd64" / ".cli_version").read_text() == other


def test不支持的平台明确拒绝():
    with pytest.raises(CliExecutionError, match="不支持的平台"):
        ensure_bundled_cli(
            Path("/tmp/never-written"), platforms=("darwin-arm64",), fetch=lambda u: b""
        )
    with pytest.raises(CliExecutionError):
        ensure_bundled_cli(
            Path("/tmp/never-written"), platforms=("win-x64",), fetch=lambda u: b""
        )


def test默认平台集合与版本常量():
    assert SUPPORTED_PLATFORMS == ("linux-amd64", "linux-arm64")
    assert DEFAULT_CLI_VERSION == "1.0.85"
