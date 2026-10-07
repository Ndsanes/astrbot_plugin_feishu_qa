#!/usr/bin/env python3
"""部署插件到远端 AstrBot 服务器。

用法:
    python tools/deploy.py [--zip <path>] [--skip-tests]

默认使用 dist/astrbot_plugin_feishu_qa.zip，部署前自动跑测试套件。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT.parent))

from astrbot_api import AstrBotClient  # noqa: E402


def run_tests() -> bool:
    """运行测试套件，返回是否通过。"""
    print("[deploy] 运行测试套件...")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "astrbot_plugin_feishu_qa/tests/", "-q"],
        cwd=PLUGIN_ROOT.parent,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print("[deploy] 测试失败:")
        print(result.stdout)
        print(result.stderr)
        return False
    # 提取测试结果行
    for line in result.stdout.splitlines():
        if "passed" in line:
            print(f"[deploy] {line.strip()}")
    return True


def build_zip() -> Path:
    """打包插件为 zip。"""
    print("[deploy] 打包插件...")
    result = subprocess.run(
        ["bash", str(PLUGIN_ROOT / "tools" / "package_astrbot_zip.sh")],
        cwd=PLUGIN_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"[deploy] 打包失败: {result.stderr}")
        sys.exit(1)
    zip_path = PLUGIN_ROOT / "dist" / "astrbot_plugin_feishu_qa.zip"
    if not zip_path.exists():
        print(f"[deploy] 打包失败: {zip_path} 不存在")
        sys.exit(1)
    print(f"[deploy] 打包完成: {zip_path}")
    return zip_path


def upload_plugin(zip_path: Path) -> dict:
    """上传插件 zip 到远端服务器。"""
    print(f"[deploy] 上传 {zip_path.name} 到远端服务器...")

    client = AstrBotClient()
    # 用管理员 JWT 登录（API key 可能没有 plugin scope）
    client.login_jwt()

    # 构造 multipart/form-data 请求
    boundary = uuid.uuid4().hex
    filename = zip_path.name
    content_type = "application/zip"

    with open(zip_path, "rb") as f:
        file_data = f.read()

    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode() + file_data + f"\r\n--{boundary}--\r\n".encode()

    headers = {
        "Authorization": f"Bearer {client._jwt_token}",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Accept": "application/json",
    }

    import urllib.request

    req = urllib.request.Request(
        f"{client.base_url}/api/v1/plugins/install/upload",
        data=body,
        headers=headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            result = json.loads(resp.read().decode())
            print(f"[deploy] 上传成功: {result}")
            return result
    except Exception as exc:
        print(f"[deploy] 上传失败: {exc}")
        raise


def main() -> int:
    ap = argparse.ArgumentParser(description="部署插件到远端 AstrBot 服务器")
    ap.add_argument("--zip", type=Path, default=None, help="指定 zip 文件路径")
    ap.add_argument("--skip-tests", action="store_true", help="跳过测试")
    ap.add_argument("--skip-build", action="store_true", help="跳过打包")
    args = ap.parse_args()

    if not args.skip_tests and not run_tests():
        print("[deploy] 测试未通过，中止部署")
        return 1

    if args.skip_build and args.zip:
        zip_path = args.zip
    else:
        zip_path = build_zip()

    try:
        upload_plugin(zip_path)
        print("[deploy] 部署完成")
        return 0
    except Exception as exc:
        print(f"[deploy] 部署失败: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
