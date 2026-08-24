"""把 QA 语料导出为 AstrBot 原生知识库预分块文档并可选上传。

用途(阶段一手工通道):让主 Agent 的 astr_kb_search 能命中精选 Q&A 语料。
chunk 内嵌 [截图标记] 与 qa_entry_images 工具配套(规格 §5/§6):
图片本体不进 KB,KB 只携带"这里有图"的语义指针。

用法(工作区根目录):
    python3 astrbot_plugin_feishu_qa/tools/export_kb_chunks.py \
        --xml astrbot_plugin_feishu_qa/tests/fixtures/qa_r8268.xml \
        --kb-id <uuid> [--upload] [--out payload.json]

零第三方依赖;上传经仓库根 astrbot_api.AstrBotClient(自动处理 JWT 提权)。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from astrbot_plugin_feishu_qa.corpus.builder import build_manifest  # noqa: E402
from astrbot_plugin_feishu_qa.corpus.parser import parse_xml  # noqa: E402

IMAGE_MARKER = "[本条目包含可按需发送的操作截图,entry_id={eid}]"


def build_chunk_content(entry: dict) -> str:
    """单条 QA → 单个 chunk 的正文(breadcrumb 标题 + 原文 + 截图标记)。"""
    section = " > ".join(entry.get("section_path") or [])
    title = entry.get("raw_title") or entry.get("title") or ""
    lines = [f"【全家桶FAQ > {section}】{title}", entry.get("body", "").strip()]
    if entry.get("images"):
        lines.append(IMAGE_MARKER.format(eid=entry["id"]))
    return "\n".join(part for part in lines if part)


def build_payload(manifest: dict) -> dict:
    """整库 → 单文档多 chunk 的导入请求体。

    文档名钉语料内容指纹,便于将来重导前识别版本与清理旧文档。
    """
    fingerprint = hashlib.sha256(
        json.dumps(
            [e["content_hash"] for e in manifest["entries"]],
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()[:8]
    document = {
        "file_name": f"有福同享全家桶FAQ-{manifest.get('revision_id', 'x')}-{fingerprint}.md",
        # 实例要求 chunks 为非空字符串列表(预分块,不再二次切片)
        "chunks": [build_chunk_content(e) for e in manifest["entries"]],
    }
    return {"documents": [document], "batch_size": 8}


def main() -> int:
    ap = argparse.ArgumentParser(description="QA 语料 → 知识库预分块导入")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--xml", help="飞书导出的文档 XML 路径")
    src.add_argument("--corpus", help="已构建的 corpus 快照 JSON 路径")
    ap.add_argument("--revision", type=int, default=-1, help="XML 源 revision")
    ap.add_argument("--kb-id", required=True, help="目标知识库 kb_id(uuid)")
    ap.add_argument("--upload", action="store_true", help="上传到实例(缺省只落盘)")
    ap.add_argument("--out", default="dist/kb_faq_chunks.json", help="payload 输出路径")
    args = ap.parse_args()

    if args.xml:
        parsed = parse_xml(
            Path(args.xml).read_text(encoding="utf-8"), source_revision=args.revision
        )
        manifest = build_manifest(parsed, revision_id=args.revision, document_id="doc")
    else:
        manifest = json.loads(Path(args.corpus).read_text(encoding="utf-8"))

    payload = build_payload(manifest)
    out = REPO_ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    marked = sum(
        1
        for c in payload["documents"][0]["chunks"]
        if "entry_id=qa_" in c
    )
    name = payload["documents"][0]["file_name"]
    total = len(payload["documents"][0]["chunks"])
    print(
        f"payload 就绪: {out}\n  文档={name} chunks={total} 含图标记={marked}"
    )

    if args.upload:
        from astrbot_api import AstrBotClient

        api = AstrBotClient()
        resp = api.post(
            f"/api/v1/knowledge-bases/{args.kb_id}/documents/import", json=payload
        )
        print("import:", json.dumps(resp, ensure_ascii=False)[:400])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
