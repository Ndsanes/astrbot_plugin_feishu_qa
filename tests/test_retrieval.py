"""检索评测测试(spec §54-§55):20 真实 query + 负例。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from astrbot_plugin_feishu_qa.corpus.parser import parse_xml
from astrbot_plugin_feishu_qa.retrieval.scorer import Confidence, Retriever

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def retriever() -> Retriever:
    xml = (FIXTURES / "qa_r8268.xml").read_text()
    parsed = parse_xml(xml, source_revision=8268)
    return Retriever(parsed.entries)


@pytest.fixture(scope="module")
def eval_set() -> dict:
    return json.loads((FIXTURES / "retrieval_queries.json").read_text())


class TestRealQueries:
    def test_twenty_real_queries_top1(self, retriever: Retriever, eval_set: dict) -> None:
        misses = []
        for case in eval_set["queries"]:
            top = retriever.search(case["query"])[0]
            haystack = (top.title + top.entry.category).lower()
            if not any(s.lower() in haystack for s in case["expect_title_substr"]):
                misses.append(f"{case['query']} -> {top.title}")
        # 允许至多 1 条歧义;spec 要求"不能出现大量 LOW"
        assert len(misses) <= 1, f"未命中: {misses}"

    def test_positive_scores_not_low(self, retriever: Retriever, eval_set: dict) -> None:
        lows = [
            case["query"]
            for case in eval_set["queries"]
            if retriever.search(case["query"])[0].confidence == Confidence.LOW
        ]
        assert not lows, f"真实问题被判 LOW(不可接受): {lows}"


class TestNegativeQueries:
    def test_irrelevant_queries_stay_low(self, retriever: Retriever, eval_set: dict) -> None:
        for query in eval_set["negative_queries"]:
            top = retriever.search(query)[0]
            assert top.confidence == Confidence.LOW, (
                f"负例误命中 {query!r} -> {top.title} score={top.score}"
            )

    def test_empty_query_returns_empty(self, retriever: Retriever) -> None:
        assert retriever.search("") == []
        assert retriever.search("   ") == []


class TestRetrieverContract:
    def test_top_k_cap(self, retriever: Retriever) -> None:
        results = retriever.search("cakewalk", top_k=3)
        assert 0 < len(results) <= 3
        scores = [r.score for r in results]
        assert scores == sorted(scores, reverse=True)

    def test_thresholds_configurable(self, parsed_xml: str) -> None:
        xml = parsed_xml
        entries = parse_xml(xml).entries
        strict = Retriever(entries, high_threshold=100.0, medium_threshold=50.0)
        assert strict.search("cakewalk 没声音")[0].confidence == Confidence.LOW


@pytest.fixture(scope="module")
def parsed_xml(request) -> str:
    return ((Path(__file__).parent / "fixtures") / "qa_r8268.xml").read_text()
