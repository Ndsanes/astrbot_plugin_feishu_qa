"""检索评测测试(spec §54-§55):20 真实 query + 负例。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from astrbot_plugin_feishu_qa.corpus.parser import parse_xml
from astrbot_plugin_feishu_qa.retrieval.scorer import (
    Confidence,
    Retriever,
    extract_terms,
    score_entry,
)

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


class TestTopicSupportGate:
    """主题词支撑闸门(2026-09-15 线上误命中修复)。

    线上事故:群里问"cakewalk sonar的混音台在哪",被以 6.59 分直答
    【如何登录激活】——全篇文档都是 Cakewalk 语境,"cakewalk/sonar"两个满语料
    高频词就足以把无关条目顶过阈值(生产阈值即 6.5)。而"混音台"在语料中零命中,
    说明该问题其实无对应条目,应交给主 Agent 而不是硬答一条不相干的。
    """

    LIVE_HIGH = 6.5  # 实例实际配置值(非代码默认 9.0),必须用生产值回归

    def test_domain_word_only_query_not_answered(self, live_retriever: Retriever) -> None:
        top = live_retriever.search("cakewalk sonar的混音台在哪")[0]
        assert top.confidence != Confidence.HIGH, (
            f"共享领域词不应构成直答依据,却命中 {top.title} score={top.score}"
        )
        assert top.score < self.LIVE_HIGH

    def test_related_query_still_answers(self, live_retriever: Retriever) -> None:
        """闸门只惩罚无支撑条目:真问登录激活时仍须高置信直答。"""
        top = live_retriever.search("cakewalk 怎么登录激活")[0]
        assert "登录激活" in top.title
        assert top.confidence == Confidence.HIGH

    def test_unsupported_entry_is_downweighted(self, parsed_xml: str) -> None:
        """无主题词支撑的条目应被压到低分区,不再与支撑条目同档。"""
        from astrbot_plugin_feishu_qa.retrieval.scorer import UNSUPPORTED_PENALTY

        retriever = Retriever(parse_xml(parsed_xml).entries)
        query = "cakewalk sonar的混音台在哪"
        hit = next(
            r for r in retriever.search(query, top_k=len(retriever.entries))
            if "如何登录激活" in r.title
        )
        raw = score_entry(hit.entry, extract_terms(query.lower()), query.lower())
        assert hit.score == pytest.approx(raw * UNSUPPORTED_PENALTY, rel=1e-6)

    def test_gate_inert_when_query_has_no_topic_words(self, parsed_xml: str) -> None:
        """查询词项全是语料高频词时(无主题词),闸门不生效——避免误伤。"""
        retriever = Retriever(parse_xml(parsed_xml).entries)
        query = "cakewalk sonar"
        top = retriever.search(query)[0]
        assert top.score == pytest.approx(
            score_entry(top.entry, extract_terms(query.lower()), query.lower()), rel=1e-6
        )


@pytest.fixture(scope="module")
def parsed_xml(request) -> str:
    return ((Path(__file__).parent / "fixtures") / "qa_r8268.xml").read_text()


@pytest.fixture(scope="module")
def live_retriever(parsed_xml: str) -> Retriever:
    """按实例实际阈值(6.5)构造的检索器;误命中事故发生在该配置下。"""
    return Retriever(
        parse_xml(parsed_xml).entries,
        high_threshold=TestTopicSupportGate.LIVE_HIGH,
        medium_threshold=3.0,
    )
