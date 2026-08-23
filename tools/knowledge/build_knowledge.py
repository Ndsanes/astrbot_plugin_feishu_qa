"""build_knowledge.py — PDF → AstrBot 知识库预处理 CLI。

用法:
    python build_knowledge.py INPUT.pdf
    python build_knowledge.py --input INPUT.pdf --output ./output/x \\
        --mode enhanced --context-mode breadcrumb --min-tokens 250 --max-tokens 500
    python build_knowledge.py --input INPUT.pdf --benchmark
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from kb_pipeline.baseline import baseline_chunks  # noqa: E402
from kb_pipeline.chunker import chunk_sections  # noqa: E402
from kb_pipeline.cleaner import clean_pages  # noqa: E402
from kb_pipeline.extract import extract_pages  # noqa: E402
from kb_pipeline.structure import (  # noqa: E402
    build_sections,
    detect_toc_pages,
    extract_toc,
)

DEFAULT_MIN_TOKENS = 250
DEFAULT_MAX_TOKENS = 500


def content_hash(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def run_pipeline(
    pdf_path: Path,
    output_dir: Path,
    *,
    mode: str = "enhanced",  # baseline | enhanced | both
    context_mode: str = "breadcrumb",  # none | breadcrumb | llm
    min_tokens: int = DEFAULT_MIN_TOKENS,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> dict:
    """执行完整流水线,返回 manifest 字典(同时落盘)。"""
    doc_title = pdf_path.stem.strip() or "Document"
    output_dir.mkdir(parents=True, exist_ok=True)

    # ── 提取与清理 ──
    pages = extract_pages(str(pdf_path))
    cleaned, clean_stats = clean_pages(pages)
    toc_pages = detect_toc_pages(cleaned)

    # ── 结构 ──
    root, headings, body_size = build_sections(
        cleaned,
        doc_title=doc_title,
        toc_pages=toc_pages,
    )
    toc_entries = extract_toc(cleaned, toc_pages)

    manifest: dict = {
        "document": pdf_path.name,
        "doc_title": doc_title,
        "page_count": len(pages),
        "toc_page_count": len(toc_pages),
        "heading_counts": {
            "l1": sum(1 for h in headings if h.level == 1),
            "l2": sum(1 for h in headings if h.level == 2),
            "l3": sum(1 for h in headings if h.level == 3),
        },
        "extraction_method": "pymupdf(font-size structure)",
        "noise_cleanup": clean_stats,
        "token_count_method": "approximate_chars_4",
    }

    chunks: list = []
    if mode in ("enhanced", "both"):
        chunks = chunk_sections(
            root, doc_title, min_tokens=min_tokens, max_tokens=max_tokens
        )
        chunks_dir = output_dir / "chunks"
        chunks_dir.mkdir(exist_ok=True)
        for idx, chunk in enumerate(chunks, 1):
            (chunks_dir / f"{idx:04d}_{chunk.chunk_id}.txt").write_text(
                chunk.content, encoding="utf-8"
            )
        manifest["enhanced"] = _chunk_stats(chunks, method="structure+breadcrumb")
        _write_toc(output_dir / "toc.txt", toc_entries)

    if mode in ("baseline", "both"):
        base_chunks = baseline_chunks(str(pdf_path))
        base_dir = output_dir / "baseline"
        base_dir.mkdir(exist_ok=True)
        for idx, bc in enumerate(base_chunks, 1):
            (base_dir / f"{idx:04d}_{bc.chunk_id}.txt").write_text(
                bc.body, encoding="utf-8"
            )
        manifest["baseline"] = _chunk_stats(base_chunks, method="flat+fixed-window")

    # processed.md:结构化全文(标题树 + 正文),供人工检查与其他 KB 复用
    _write_processed_md(output_dir / "processed.md", root, headings)

    # context-mode 记录(llm 模式在 enrich 步骤另行处理)
    manifest["context_mode"] = context_mode

    digest = content_hash(manifest)
    manifest["manifest_hash_input"] = digest
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def _chunk_stats(chunks, *, method: str) -> dict:
    tokens = [c.tokens for c in chunks]
    ids = [c.chunk_id for c in chunks]
    return {
        "method": method,
        "chunk_count": len(chunks),
        "avg_chunk_tokens": round(statistics.mean(tokens), 1) if tokens else 0,
        "median_chunk_tokens": statistics.median(tokens) if tokens else 0,
        "max_chunk_tokens": max(tokens) if tokens else 0,
        "min_chunk_tokens": min(tokens) if tokens else 0,
        "unique_ids": len(set(ids)) == len(ids),
        "over_limit_ratio": round(
            sum(1 for t in tokens if t > DEFAULT_MAX_TOKENS * 1.15) / len(tokens), 4
        )
        if tokens
        else 0.0,
        "_total_chars": sum(len(c.body) for c in chunks),
    }


def _write_toc(path: Path, entries: list[tuple[int, str]]) -> None:
    lines = [f"{title} .... p{page}" if page > 0 else title for page, title in entries]
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_processed_md(path: Path, root, headings) -> None:
    out: list[str] = []
    for section in root.walk():
        if section.level == 0:
            continue
        prefix = "#" * min(section.level + 1, 6)
        out.append(f"{prefix} {section.title}\n")
        for block in section.blocks:
            out.append(block.text + "\n")
    path.write_text("\n".join(out), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="build_knowledge.py",
        description="PDF → AstrBot 知识库预处理流水线(结构化提取+breadcrumb分块)",
    )
    parser.add_argument("input_pos", nargs="?", help="输入 PDF 路径")
    parser.add_argument("--input", dest="input_opt", help="输入 PDF 路径")
    parser.add_argument("--output", default=None, help="输出目录")
    parser.add_argument(
        "--mode",
        choices=["baseline", "enhanced", "both"],
        default="enhanced",
        help="产出模式",
    )
    parser.add_argument(
        "--context-mode",
        choices=["none", "breadcrumb", "llm"],
        default="breadcrumb",
        help="enrichment 策略(breadcrumb 为默认,零 LLM 成本)",
    )
    parser.add_argument("--min-tokens", type=int, default=DEFAULT_MIN_TOKENS)
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    args = parser.parse_args(argv)

    pdf = args.input_opt or args.input_pos
    if not pdf:
        parser.error("必须提供输入 PDF")
    pdf_path = Path(pdf).expanduser().resolve()
    if not pdf_path.is_file():
        print(f"error: 文件不存在: {pdf_path}", file=sys.stderr)
        return 2

    output_dir = (
        Path(args.output).expanduser().resolve()
        if args.output
        else Path("./output") / pdf_path.stem.replace(" ", "_").lower()
    )

    mode = args.mode
    if args.context_mode == "llm":
        print(
            "[note] --context-mode llm 需要豆包 API 凭据;"
            "当前版本先产出 breadcrumb 语料,llm 增强由 enrich 子命令提供。",
            file=sys.stderr,
        )

    manifest = run_pipeline(
        pdf_path,
        output_dir,
        mode=mode,
        context_mode=args.context_mode,
        min_tokens=args.min_tokens,
        max_tokens=args.max_tokens,
    )
    print(f"pages={manifest['page_count']} output={output_dir}")
    for key in ("enhanced", "baseline"):
        if key in manifest:
            st = manifest[key]
            print(
                f"{key}: chunks={st['chunk_count']} "
                f"avg_tokens={st['avg_chunk_tokens']} "
                f"max_tokens={st['max_chunk_tokens']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
