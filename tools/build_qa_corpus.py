#!/usr/bin/env python3
"""语料构建 CLI(spec §66 验收:连跑两次 content_hash 一致)。

用法:
    python tools/build_qa_corpus.py --data-root <dir>            # 在线抓取+下载图
    python tools/build_qa_corpus.py --offline                    # 用 fixture 快照,不联网
    python tools/build_qa_corpus.py --check-idempotent           # 构建两次比对 hash

退出码:0 成功;1 构建失败。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT.parent))  # workspace 根(astrbot_lark_kit)

from astrbot_plugin_feishu_qa.adapter.lark import DOC_FORMAT_XML, LarkAdapter  # noqa: E402
from astrbot_plugin_feishu_qa.corpus.builder import (  # noqa: E402
    build_manifest,
    manifest_content_hash,
)
from astrbot_plugin_feishu_qa.corpus.parser import parse_xml  # noqa: E402
from astrbot_plugin_feishu_qa.storage.snapshot import SnapshotStore  # noqa: E402

WIKI_URL = "https://my.feishu.cn/wiki/O9fcwP1PviPuOSkGBekc7B7xn4c"


def load_offline_xml() -> tuple[str, int, str]:
    meta = json.loads((PLUGIN_ROOT / "tests" / "fixtures" / "meta.json").read_text())
    xml = (PLUGIN_ROOT / "tests" / "fixtures" / "qa_r8268.xml").read_text()
    return xml, int(meta["revision_id"]), str(meta["document_id"])


async def fetch_online(adapter: LarkAdapter) -> tuple[str, int, str]:
    doc = await adapter.fetch_doc(fmt=DOC_FORMAT_XML)
    return doc.content, doc.revision_id, doc.document_id


async def download_images(
    adapter: LarkAdapter | None,
    manifest: dict,
    images_dir: Path,
) -> dict[Path, str]:
    """下载全部缺失图片;单张失败仅记录,不中断(spec §12)。"""
    staged: dict[Path, str] = {}
    failures = 0
    images_dir.mkdir(parents=True, exist_ok=True)
    for entry in manifest["entries"]:
        for img in entry["images"]:
            final_name = Path(img["local_path"]).name
            target = images_dir / final_name
            if target.is_file() and target.stat().st_size > 0:
                continue
            if adapter is None:
                continue
            path = await adapter.download_media(img["file_token"], target)
            if path is None:
                failures += 1
                print(f"[warn] 图片下载失败 token={img['file_token'][:16]}…")
            else:
                staged[path] = final_name
    if failures:
        print(f"[warn] 共 {failures} 张图片下载失败")
    return staged


async def main() -> int:
    ap = argparse.ArgumentParser(description="构建飞书 QA 语料快照")
    ap.add_argument("--data-root", type=Path, default=None)
    ap.add_argument("--offline", action="store_true", help="使用 fixture,不联网")
    ap.add_argument("--check-idempotent", action="store_true")
    ap.add_argument("--skip-images", action="store_true")
    args = ap.parse_args()

    data_root = args.data_root or PLUGIN_ROOT / "data" / "feishu_qa"

    if args.offline:
        xml, revision_id, document_id = load_offline_xml()
        adapter = None
        source = f"fixture(r{revision_id})"
    else:
        adapter = LarkAdapter(doc_ref=WIKI_URL)
        xml, revision_id, document_id = await fetch_online(adapter)
        source = f"online(r{revision_id})"

    parsed = parse_xml(xml, source_revision=revision_id)
    manifest = build_manifest(
        parsed, revision_id=revision_id, document_id=document_id
    )
    manifest["source"] = source

    store = SnapshotStore(data_root)

    if not args.skip_images and adapter is not None:
        with tempfile.TemporaryDirectory() as td:
            staged = await download_images(adapter, manifest, store.images_dir)
        # staged 已直接落在正式 images 目录,无需再搬移
    else:
        staged = {}

    old = store.load()
    from astrbot_plugin_feishu_qa.corpus.builder import diff_manifests

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
