"""离线检索评测:BM25-lite 检索代理 + Recall/MRR/FP 指标。

说明(规格 §58):本模块是**离线代理**,用纯 Python BM25 近似 AstrBot 的
sparse 检索路径。dense 路径依赖真实 bge-m3 端点,由 --dense-api 启用;
未启用时 benchmark 只代表 sparse 行为,结论外推需谨慎并在报告标注。
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

_TOKEN_RE = re.compile(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]")


def tokenize(text: str) -> list[str]:
    """英文按词、中文按字切分(bge-m3 对中文查询的常见处理近似)。"""
    return [t.lower() for t in _TOKEN_RE.findall(text)]


class BM25Lite:
    """Okapi BM25(k1=1.5, b=0.75)。纯 Python 排序代理。"""

    def __init__(self, docs: list[str], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self.docs_tokens = [tokenize(d) for d in docs]
        self.doc_lens = [len(t) for t in self.docs_tokens]
        self.avg_len = sum(self.doc_lens) / max(1, len(self.doc_lens))
        self.df: dict[str, int] = {}
        for toks in self.docs_tokens:
            for term in set(toks):
                self.df[term] = self.df.get(term, 0) + 1
        self.n = len(docs)

    def search(self, query: str, top_k: int = 10) -> list[tuple[int, float]]:
        q_tokens = tokenize(query)
        scores: list[float] = []
        for idx, toks in enumerate(self.docs_tokens):
            tf: dict[str, int] = {}
            for t in toks:
                tf[t] = tf.get(t, 0) + 1
            score = 0.0
            for qt in q_tokens:
                f = tf.get(qt, 0)
                if not f:
                    continue
                df = self.df.get(qt, 0)
                idf = math.log((self.n - df + 0.5) / (df + 0.5) + 1.0)
                denom = f + self.k1 * (
                    1 - self.b + self.b * self.doc_lens[idx] / self.avg_len
                )
                score += idf * (f * (self.k1 + 1)) / denom
            scores.append(score)
        ranked = sorted(range(len(scores)), key=lambda i: -scores[i])[:top_k]
        return [(i, scores[i]) for i in ranked if scores[i] > 0]


@dataclass(slots=True)
class QueryCase:
    query: str
    category: str
    answerable: bool
    # 命中判定证据:chunk 内容需包含其中至少一个片段(不区分大小写)
    evidence: list[str]


def load_queries(path: Path) -> list[QueryCase]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [
        QueryCase(
            query=item["query"],
            category=item["category"],
            answerable=item.get("answerable", True),
            evidence=item["evidence"],
        )
        for item in raw
    ]


def _hit(chunk_text: str, evidence: list[str]) -> bool:
    low = chunk_text.lower()
    return any(ev.lower() in low for ev in evidence)


def evaluate(
    chunks_texts: list[str],
    queries: list[QueryCase],
    *,
    ks: tuple[int, ...] = (1, 3, 5),
) -> dict:
    bm25 = BM25Lite(chunks_texts)
    recalls = {f"R@{k}": [] for k in ks}
    rr_values: list[float] = []
    fp_cases = 0
    unanswerable = 0

    for case in queries:
        ranked = bm25.search(case.query, top_k=max(ks))
        hits_in_order = [
            1 if _hit(chunks_texts[idx], case.evidence) else 0 for idx, _ in ranked
        ]

        if not case.answerable:
            unanswerable += 1
            if any(hits_in_order[: min(3, len(hits_in_order))]):
                fp_cases += 1
            continue

        for k in ks:
            window = hits_in_order[:k]
            recalls[f"R@{k}"].append(1.0 if any(window) else 0.0)
        rr = 0.0
        for rank, hit in enumerate(hits_in_order, 1):
            if hit:
                rr = 1.0 / rank
                break
        rr_values.append(rr)

    answered = sum(1 for q in queries if q.answerable)

    def avg(values: list[float]) -> float:
        return round(sum(values) / len(values), 4) if values else 0.0

    result = {
        "answered_queries": answered,
        "unanswerable_queries": unanswerable,
        "false_positive_top3": fp_cases,
        "fp_rate": round(fp_cases / max(1, unanswerable), 4),
    }
    for k in ks:
        result[f"R@{k}"] = avg(recalls[f"R@{k}"])
    result["MRR@5"] = avg(rr_values)
    return result
