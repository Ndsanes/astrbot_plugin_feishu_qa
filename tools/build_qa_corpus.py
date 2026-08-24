#!/usr/bin/env python3
"""语料构建 CLI(spec §66 验收:连跑两次 content_hash 一致)。

用法:
    python tools/build_qa_corpus.py --data-root <dir>            # 用 fixture 构建快照
    python tools/build_qa_corpus.py --check-idempotent           # 构建两次比对 hash

退出码:0 成功;1 构建失败。

注:本工具不再直连飞书(在线抓取已收口到 lark_cli 平台网关);
线上语料更新走插件内 /qa_sync。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT.parent))  # workspace 根(astrbot_lark_kit)

from astrbot_plugin_feishu_qa.corpus.builder import (  # noqa: E402
    build_manifest,
    diff_manifests,
    manifest_content_hash,
)
from astrbot_plugin_feishu_qa.corpus.parser import parse_xml  # noqa: E402
from astrbot_plugin_feishu_qa.storage.snapshot import SnapshotStore  # noqa: E402


def load_offline_xml() -> tuple[str, int, str]:
    meta = json.loads((PLUGIN_ROOT / "tests" / "fixtures" / "meta.json").read_text())
    xml = (PLUGIN_ROOT / "tests" / "fixtures" / "qa_r8268.xml").read_text()
    return xml, int(meta["revision_id"]), str(meta["document_id"])


async def main() -> int:
    ap = argparse.ArgumentParser(description="构建飞书 QA 语料快照(fixture 离线)")
    ap.add_argument("--data-root", type=Path, default=None)
    ap.add_argument("--check-idempotent", action="store_true")
    args = ap.parse_args()

    data_root = args.data_root or PLUGIN_ROOT / "data" / "feishu_qa"

    xml, revision_id, document_id = load_offline_xml()
    source = f"fixture(r{revision_id})"

    parsed = parse_xml(xml, source_revision=revision_id)
    manifest = build_manifest(
        parsed, revision_id=revision_id, document_id=document_id
    )
    manifest["source"] = source

    store = SnapshotStore(data_root)

    old = store.load()
    diff = diff_manifests(old, manifest)

    if old is not None and old.get("content_hash") == manifest["content_hash"]:
        print(f"[unchanged] revision={revision_id} hash={manifest['content_hash'][:12]}…")
        if not args.check_idempotent:
            return 0
    else:
        store.commit(manifest)
        print(
            f"[built] revision={revision_id} entries={manifest['entry_count']} "
            f"images={manifest['image_count']} "
            f"hash={manifest['content_hash'][:12]}… "
            f"(added={diff['added']} updated={diff['updated']} removed={diff['removed']})"
        )

    if args.check_idempotent:
        parsed2 = parse_xml(xml, source_revision=revision_id)
        manifest2 = build_manifest(parsed2, revision_id=revision_id, document_id=document_id)
        h1, h2 = manifest_content_hash(manifest), manifest_content_hash(manifest2)
        if h1 != h2:
            print("[FAIL] 幂等校验失败:两次构建 hash 不一致")
            return 1
        print(f"[ok] 幂等校验通过 hash={h1[:12]}…")

    if parsed.diagnostics:
        print(f"[diag] {len(parsed.diagnostics)} 条诊断信息:")
        for d in parsed.diagnostics:
            print(f"  - {d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
