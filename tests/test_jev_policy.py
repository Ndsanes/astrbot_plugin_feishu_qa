"""Jev 决策策略测试(纯逻辑,喂假概率,穷举边界)。

两个位点的阈值方向相反是本文件的核心断言来源;C 偏"答得上"、D 偏"别附"。
"""

from __future__ import annotations

import pytest

from astrbot_plugin_feishu_qa.jev.client import JevResult
from astrbot_plugin_feishu_qa.jev.policy import (
    ACTION_ANSWER_SELF,
    ACTION_FALLBACK,
    ACTION_HAND_OFF,
    SYNTHESIS_QID,
    JevCandidate,
    build_questions,
    build_state,
    decide_links,
    decide_takeover,
    sufficient_qid,
)

SUFF_T = 0.6
SYNTH_T = 0.6


def cands(n: int) -> list[JevCandidate]:
    return [
        JevCandidate(entry_id=f"qa_{i}", title=f"标题{i}", text=f"正文{i}")
        for i in range(n)
    ]


def res(synthesis: float, sufficients: list[float], *, ok: bool = True):
    nouls = {SYNTHESIS_QID: synthesis}
    for i, v in enumerate(sufficients):
        nouls[sufficient_qid(i)] = v
    return JevResult(ok=ok, nouls=nouls)


class TestStateAndQuestions:
    def test_state_shape(self) -> None:
        s = build_state("混音台在哪", cands(2))
        assert s["question"] == "混音台在哪"
        assert [c["id"] for c in s["candidates"]] == ["qa_0", "qa_1"]

    def test_questions_cover_synthesis_plus_each_candidate(self) -> None:
        q = build_questions(cands(3))
        assert set(q) == {SYNTHESIS_QID, "sufficient_0", "sufficient_1", "sufficient_2"}
        assert all(v["type"] == "noul" for v in q.values())

    def test_every_question_has_both_criteria_sides(self) -> None:
        """jaggedness 第 7 条:instructions 与 criteria 矛盾会变差,两侧写死。"""
        for q in build_questions(cands(2)).values():
            assert q["criteria"]["true"]
            assert q["criteria"]["false"]

    def test_asks_no_counting_question(self) -> None:
        """jaggedness 第 2 条:数数不可靠。条数只能是结果,不能是问题。"""
        for qid, q in build_questions(cands(2)).items():
            assert "how many" not in q["instructions"].lower()
            assert "几个" not in qid


class TestUsability:
    @pytest.mark.parametrize(
        "bad",
        [
            None,
            JevResult(ok=False),
            JevResult(ok=True, nouls={}),  # 什么都没有
            JevResult(ok=True, nouls={SYNTHESIS_QID: 0.1}),  # 缺候选问题
        ],
    )
    def test_unusable_falls_back_not_guesses(self, bad) -> None:
        d = decide_takeover(
            cands(2), bad, sufficiency_threshold=SUFF_T, synthesis_threshold=SYNTH_T
        )
        assert d.action == ACTION_FALLBACK
        assert d.entry_ids == ()

    def test_missing_candidate_answer_is_unusable_not_zero(self) -> None:
        """只答到候选0、候选1缺失 → 整体不可用,不能用 0.0 顶替。"""
        partial = JevResult(ok=True, nouls={SYNTHESIS_QID: 0.0, sufficient_qid(0): 0.99})
        d = decide_takeover(
            cands(2), partial, sufficiency_threshold=SUFF_T, synthesis_threshold=SYNTH_T
        )
        assert d.action == ACTION_FALLBACK

    def test_no_candidates_falls_back(self) -> None:
        d = decide_takeover(
            [], res(0.0, []),
            sufficiency_threshold=SUFF_T, synthesis_threshold=SYNTH_T,
        )
        assert d.action == ACTION_FALLBACK
        assert d.reason == "no_candidates"


class TestTakeover:
    """C 位点:代价偏向"答得上",阈值取低。"""

    def test_one_sufficient_no_synthesis_answers_self(self) -> None:
        d = decide_takeover(cands(2), res(0.0, [0.9, 0.1]),
                            sufficiency_threshold=SUFF_T, synthesis_threshold=SYNTH_T)
        assert d.action == ACTION_ANSWER_SELF
        assert d.entry_ids == ("qa_0",)

    def test_multiple_sufficient_all_returned(self) -> None:
        d = decide_takeover(cands(3), res(0.0, [0.8, 0.7, 0.1]),
                            sufficiency_threshold=SUFF_T, synthesis_threshold=SYNTH_T)
        assert d.entry_ids == ("qa_0", "qa_1")

    def test_below_threshold_is_not_sufficient(self) -> None:
        d = decide_takeover(
            cands(1), res(0.0, [SUFF_T - 0.01]),
            sufficiency_threshold=SUFF_T, synthesis_threshold=SYNTH_T,
        )
        assert d.action == ACTION_HAND_OFF

    def test_exactly_at_threshold_counts(self) -> None:
        d = decide_takeover(
            cands(1), res(0.0, [SUFF_T]),
            sufficiency_threshold=SUFF_T, synthesis_threshold=SYNTH_T,
        )
        assert d.action == ACTION_ANSWER_SELF

    def test_needs_synthesis_hands_off(self) -> None:
        d = decide_takeover(
            cands(2), res(0.9, [0.1, 0.1]),
            sufficiency_threshold=SUFF_T, synthesis_threshold=SYNTH_T,
        )
        assert d.action == ACTION_HAND_OFF
        assert d.reason == "needs_synthesis"

    def test_synthesis_overrides_even_a_good_candidate(self) -> None:
        """需要综合时,单条高分也不自己答——综合正是单条最该让位的情况。"""
        d = decide_takeover(
            cands(2), res(0.95, [0.99, 0.0]),
            sufficiency_threshold=SUFF_T, synthesis_threshold=SYNTH_T,
        )
        assert d.action == ACTION_HAND_OFF
        assert d.reason == "mixed_evidence"

    def test_synthesis_exactly_at_threshold_triggers(self) -> None:
        d = decide_takeover(
            cands(2), res(SYNTH_T, [0.0, 0.0]),
            sufficiency_threshold=SUFF_T, synthesis_threshold=SYNTH_T,
        )
        assert d.action == ACTION_HAND_OFF

    def test_decision_carries_nouls_for_logging(self) -> None:
        d = decide_takeover(
            cands(1), res(0.0, [0.9]),
            sufficiency_threshold=SUFF_T, synthesis_threshold=SYNTH_T,
        )
        assert d.nouls[SYNTHESIS_QID] == 0.0
        assert d.nouls[sufficient_qid(0)] == 0.9


class TestLinks:
    """D 位点:代价偏向"别附",阈值取高,且按 noul 降序而非本地分数。"""

    def test_none_sufficient_returns_nothing(self) -> None:
        d = decide_links(cands(3), res(0.0, [0.1, 0.2, 0.3]),
                         sufficiency_threshold=SUFF_T, max_links=3)
        assert d.action == ACTION_ANSWER_SELF
        assert d.entry_ids == ()
        assert d.reason == "none_sufficient"

    def test_sorted_by_noul_descending(self) -> None:
        """候选0 本地分最高但 noul 最低 → 必须排在最后。9·15 事故的教训。"""
        d = decide_links(cands(3), res(0.0, [0.65, 0.95, 0.7]),
                         sufficiency_threshold=SUFF_T, max_links=3)
        assert d.entry_ids == ("qa_1", "qa_2", "qa_0")

    def test_respects_max_links(self) -> None:
        d = decide_links(cands(3), res(0.0, [0.9, 0.95, 0.99]),
                         sufficiency_threshold=SUFF_T, max_links=2)
        assert d.entry_ids == ("qa_2", "qa_1")

    def test_ties_broken_by_entry_id_deterministically(self) -> None:
        a = decide_links(cands(2), res(0.0, [0.8, 0.8]),
                         sufficiency_threshold=SUFF_T, max_links=2)
        assert a.entry_ids == ("qa_0", "qa_1")

    def test_unusable_falls_back(self) -> None:
        d = decide_links(cands(2), JevResult(ok=False),
                         sufficiency_threshold=SUFF_T, max_links=3)
        assert d.action == ACTION_FALLBACK

    def test_synthesis_answer_is_ignored_at_d(self) -> None:
        """D 点只问"够不够",不因"需综合"而改变链接判定——那是 C 的事。"""
        d = decide_links(cands(2), res(1.0, [0.9, 0.1]),
                         sufficiency_threshold=SUFF_T, max_links=3)
        assert d.entry_ids == ("qa_0",)


class ThresholdDirection:
    """把"C 偏答得上 / D 偏别附"钉成可执行断言。

    同一条证据,在两个位点上应给出**相反**的倾向:中等把握(0.5)时,
    C 判"我们自己答",D 判"别附"。若日后有人把两个阈值并成一个,这里会红。
    """

    MID = 0.5
    HIGH = 0.6

    def test_same_evidence_opposite_lean_at_two_sites(self) -> None:
        r = res(0.0, [self.MID, self.MID])
        c_take = decide_takeover(cands(2), r, sufficiency_threshold=self.MID,
                                 synthesis_threshold=0.9)
        d_link = decide_links(cands(2), r, sufficiency_threshold=self.HIGH, max_links=3)
        assert c_take.action == ACTION_ANSWER_SELF, "C 位点中等把握就该自己答"
        assert d_link.entry_ids == (), "D 位点中等把握就不该附链"
