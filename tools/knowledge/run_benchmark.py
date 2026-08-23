"""run_benchmark.py — 三方案消融评测(baseline / breadcrumb / breadcrumb+LLM)。

用法:
    python run_benchmark.py --output ./output/cakewalk
前置:build_knowledge.py 已产出 chunks/ 与 baseline/。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from kb_pipeline.evaluate import evaluate, load_queries  # noqa: E402


def _load_chunks(chunks_dir: Path) -> list[str]:
    files = sorted(chunks_dir.glob("*.txt"))
    return [f.read_text(encoding="utf-8") for f in files]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default="./output/cakewalk")
    ap.add_argument("--queries", default=None, help="queries JSON(默认内置数据集)")
    ap.add_argument(
        "--llm-chunks",
        default=None,
        help="breadcrumb+LLM 变体的 chunks 目录(存在时参与对比)",
    )
    args = ap.parse_args()

    out_dir = Path(args.output)
    queries_path = (
        Path(args.queries)
        if args.queries
        else Path(__file__).resolve().parent / "queries_cakewalk.json"
    )

    variants: dict[str, list[str]] = {}
    base_dir = out_dir / "baseline"
    enh_dir = out_dir / "chunks"
    if base_dir.is_dir():
        variants["Baseline"] = _load_chunks(base_dir)
    if enh_dir.is_dir():
        variants["Breadcrumb"] = _load_chunks(enh_dir)
    if args.llm_chunks and Path(args.llm_chunks).is_dir():
        variants["Breadcrumb+LLM"] = _load_chunks(Path(args.llm_chunks))

    if len(variants) < 2:
        print("error: 至少需要 baseline 与 enhanced 两个变体", file=sys.stderr)
        return 2

    queries = load_queries(queries_path)

    results = {}
    for name, texts in variants.items():
        results[name] = evaluate(texts, queries)
        print(f"== {name} ({len(texts)} chunks)")
        for key, value in results[name].items():
            print(f"   {key}: {value}")

    report = {
        "query_count": len(queries),
        "matrix": {name: res for name, res in results.items()},
    }
    (out_dir / "eval").mkdir(exist_ok=True)
    (out_dir / "eval" / "benchmark.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nsaved -> {out_dir / 'eval' / 'benchmark.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
