"""Tier 0 确定性检索(spec §19-§21)。

零 LLM、零外部依赖。评分权重:症状标签 > 标题 > 分类路径 > 关键词 > 正文。
置信分级阈值经真实 fixture 校准(见 tests/fixtures/retrieval_queries.json)。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..corpus.model import QaEntry


class Confidence:
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


# 经 20 真实 query + 负例校准的默认阈值(可在配置中覆盖)
DEFAULT_HIGH_THRESHOLD = 9.0
DEFAULT_MEDIUM_THRESHOLD = 3.0

_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
_TOKEN_SPLIT_RE = re.compile(r"[^0-9a-zA-Z\u4e00-\u9fff]+")


def _ngrams(text: str, sizes: tuple[int, ...] = (2, 3, 4)) -> set[str]:
    """CJK n-gram 集合(容错同义/变体措辞的基础手段)。"""
    grams: set[str] = set()
    for run in _CJK_RUN_RE.findall(text):
        for size in sizes:
            if len(run) < size:
                grams.add(run)
                continue
            for i in range(len(run) - size + 1):
                grams.add(run[i : i + size])
    return grams

def extract_terms(query: str) -> set[str]:
    """查询词项:独立 ASCII 词元(小写)+ 全串 CJK n-gram。

    中英混排(如"复制midi的时候")中的拉丁词必须单独成项,
    否则标题里的产品名永远匹配不上。
    """
    q = query.lower()
    terms: set[str] = set(re.findall(r"[a-z0-9][a-z0-9+.]*", q))
    terms |= _ngrams(q)
    # 纯数字/过短噪声剔除
    return {t for t in terms if not t.isdigit() or len(t) >= 2}


@dataclass
class SearchResult:
    entry: QaEntry
    score: float
    confidence: str

    @property
    def title(self) -> str:
        return self.entry.raw_title


def score_entry(entry: QaEntry, terms: set[str], query_lower: str) -> float:
    """单条目打分。权重严格按 spec §19 排序。

    标题/分类用包含率(交集/较短集合),消除长标题词数偏置;
    稀有 ASCII 词(错误码/产品名,如 xsampler/dll)单独加权。
    """
    score = 0.0

    # 1. 症状标签(最高权重)
    for tag in entry.symptom_tags:
        if tag and tag in query_lower:
            score += 8.0  # 完整命中
        else:
            overlap = len(_ngrams(tag) & terms) / max(1, len(_ngrams(tag)))
            score += overlap * 3.5

    # 2. 标题(包含率,短查询也能公平竞争)
    title_terms = extract_terms(entry.raw_title)
    inter = title_terms & terms
    if inter:
        containment = len(inter) / max(1, min(len(title_terms), len(terms)))
        score += containment * 9.0

    # 2b. 稀有 ASCII 词:标题命中强信号,正文命中弱信号
    for term in terms:
        if term.isascii() and len(term) >= 4:
            if term in entry.raw_title.lower():
                score += 2.5
            elif term in entry.body.lower():
                score += 0.8

    # 3. 分类路径(包含率)
    cat_terms = extract_terms(entry.category)
    cat_inter = cat_terms & terms
    if cat_inter:
        score += (len(cat_inter) / max(1, min(len(cat_terms), len(terms)))) * 2.5

    # 4. 正文(低权重,封顶)
    body_hits = sum(1 for t in terms if len(t) >= 2 and t in entry.body)
    score += min(body_hits, 24) * 0.15

    return score


class Retriever:
    """确定性检索器。持有不可变快照条目;线程安全(只读)。"""

    def __init__(
        self,
        entries: list[QaEntry],
        *,
        high_threshold: float = DEFAULT_HIGH_THRESHOLD,
        medium_threshold: float = DEFAULT_MEDIUM_THRESHOLD,
    ) -> None:
        self.entries = entries
        self.high_threshold = high_threshold
        self.medium_threshold = medium_threshold

    def search(self, query: str, *, top_k: int = 3) -> list[SearchResult]:
        """返回按分数降序的前 top_k 条(spec §20:最多 3,通常取第 1)。"""
        if not query.strip():
            return []
        query_lower = query.lower().strip()
        terms = extract_terms(query_lower)
        scored = [
            (entry, score_entry(entry, terms, query_lower))
            for entry in self.entries
        ]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        results: list[SearchResult] = []
        for entry, score in scored[:top_k]:
            results.append(
                SearchResult(
                    entry=entry,
                    score=score,
                    confidence=self.classify(score),
                )
            )
        return results

    def classify(self, score: float) -> str:
        if score >= self.high_threshold:
            return Confidence.HIGH
        if score >= self.medium_threshold:
            return Confidence.MEDIUM
        return Confidence.LOW
