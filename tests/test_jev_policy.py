"""Jev 决策策略测试(纯逻辑,喂假答案,穷举边界)。

核心断言来源是 2026-09-26 的实测教训:**逐候选 Noul 不能用来排序**。同一批
候选上,逐候选 Noul 让正确条目排第一只有 3/8(绝对分互不竞争),Choice 是
6/8,22 题规模上 Choice 20/8→20/22。因此本文件把"必须用 Choice 排序"钉成
可执行断言,防止有人日后"顺手改回 Noul"。
"""

from __future__ import annotations

import pytest

from astrbot_plugin_feishu_qa.jev.client import ChoiceAnswer, JevResult
from astrbot_plugin_feishu_qa.jev.policy import (
    ACTION_ANSWER_SELF,
    ACTION_FALLBACK,
    ACTION_HAND_OFF,
    ANY_QID,
    RANK_QID,
    JevCandidate,
    build_questions,
    build_state,
    decide_links,
    decide_takeover,
    option_key,
)

ANSWERABLE_T = 0.5
PROB_T = 0.35


def cands(n: int) -> list[JevCandidate]:
    return [
        JevCandidate(entry_id=f"qa_{i}", title=f"标题{i}", text=f"正文{i}")
        for i in range(n)
    ]


def res(
    probs: list[float], *, answerable: float | None = 0.9, with_choice: bool = True
) -> JevResult:
    d = {}
    if answerable is not None:
        d[ANY_QID] = answerable
    if with_choice and probs:
        d[RANK_QID] = ChoiceAnswer(
            choice=option_key(max(range(len(probs)), key=lambda i: probs[i])),
            probabilities={option_key(i): v for i, v in enumerate(probs)},
            confidence=0.7,
        )
    return JevResult(ok=True, nouls={k: v for k, v in d.items() if k == ANY_QID}, choices={
        k: v for k, v in d.items() if k == RANK_QID
    })


class TestStateAndQuestions:
    def test_state_carries_indexed_candidates(self) -> None:
        s = build_state("混音台在哪", cands(2))
        assert s["question"] == "混音台在哪"
        assert [c["index"] for c in s["candidates"]] == [0, 1]

    def test_one_choice_plus_one_noul(self) -> None:
        """两粒度:Choice 选(相对) + Noul 判断该不该用(绝对)。"""
        q = build_questions(cands(3))
        assert set(q) == {RANK_QID, ANY_QID}
        assert q[RANK_QID]["type"] == "choice"
        assert q[ANY_QID]["type"] == "noul"

    def test_choice_criteria_cover_every_candidate(self) -> None:
        q = build_questions(cands(3))
        assert set(q[RANK_QID]["criteria"]) == {"option_0", "option_1", "option_2"}

    def test_noul_has_both_criteria_sides(self) -> None:
        """jaggedness #7:instructions 与 criteria 矛盾会变差,两侧写死。"""
        q = build_questions(cands(2))[ANY_QID]
        assert q["criteria"]["true"] and q["criteria"]["false"]

    def test_choice_criteria_values_are_descriptions(self) -> None:
        q = build_questions(cands(2))[RANK_QID]
        assert all(isinstance(v, str) and v for v in q["criteria"].values())

    def test_asks_no_counting_question(self) -> None:
        """jaggedness #2:数数不可靠,条数只能是结果。"""
        for qid, q in build_questions(cands(2)).items():
            assert "how many" not in q["instructions"].lower()
            assert "几个" not in qid

    def test_asks_no_per_candidate_noul(self) -> None:
        """回归:逐候选 Noul 做排序是错的(实测 3/8),不能再退回那种设计。"""
        q = build_questions(cands(3))
        per_candidate = [
            k for k in q
            if k.startswith("sufficient_") or k.startswith("option_")
        ]
        assert not per_candidate, "不得为每个候选单开一个 Noul 问题"


class TestUsability:
    @pytest.mark.parametrize(
        "bad",
        [
            None,
            JevResult(ok=False),
            JevResult(ok=True, nouls={}, choices={}),
            # 缺 Noul
            JevResult(
                ok=True, nouls={},
                choices={RANK_QID: ChoiceAnswer("option_0", {"option_0": 1.0})},
            ),
            # 缺 Choice
            JevResult(ok=True, nouls={ANY_QID: 0.9}, choices={}),
        ],
    )
    def test_unusable_falls_back(self, bad) -> None:
        d = decide_takeover(
            cands(2), bad,
            answerable_threshold=ANSWERABLE_T, probability_threshold=PROB_T,
        )
        assert d.action == ACTION_FALLBACK and d.entry_ids == ()

    def test_incomplete_probability_table_is_unusable(self) -> None:
        """候选 2 没出现在概率表里 → 不可用,不能默认当 0。"""
        r = JevResult(
            ok=True,
            nouls={ANY_QID: 0.9},
            choices={
                RANK_QID: ChoiceAnswer("option_0", {"option_0": 1.0, "option_1": 0.0})
            },
        )
        d = decide_takeover(
            cands(3), r,
            answerable_threshold=ANSWERABLE_T, probability_threshold=PROB_T,
        )
        assert d.action == ACTION_FALLBACK

    def test_no_candidates_falls_back(self) -> None:
        d = decide_takeover(
            [], res([]), answerable_threshold=ANSWERABLE_T, probability_threshold=PROB_T
        )
        assert d.reason == "no_candidates"


class TestTakeover:
    """C 位点:代价偏向「有把握」,阈值取低。"""

    def test_answerable_and_confident_answers_self(self) -> None:
        d = decide_takeover(
            cands(3), res([0.1, 0.6, 0.3]),
            answerable_threshold=ANSWERABLE_T, probability_threshold=PROB_T,
        )
        assert d.action == ACTION_ANSWER_SELF
        assert d.entry_ids == ("qa_1",), "必须挑概率最高的那条,不是第一条"

    def test_not_answerable_hands_off(self) -> None:
        d = decide_takeover(
            cands(2), res([0.9, 0.1], answerable=0.2),
            answerable_threshold=ANSWERABLE_T, probability_threshold=PROB_T,
        )
        assert d.action == ACTION_HAND_OFF
        assert d.reason == "not_answerable"

    def test_low_confidence_hands_off(self) -> None:
        """材料沾边但分布平(没人明显更对)→ 交回 Agent,不做猜测。"""
        d = decide_takeover(
            cands(2), res([0.34, 0.33]),
            answerable_threshold=ANSWERABLE_T, probability_threshold=PROB_T,
        )
        assert d.action == ACTION_HAND_OFF
        assert d.reason == "low_confidence"

    def test_probability_exactly_at_threshold_answers(self) -> None:
        d = decide_takeover(
            cands(2), res([PROB_T, 0.1]),
            answerable_threshold=ANSWERABLE_T, probability_threshold=PROB_T,
        )
        assert d.action == ACTION_ANSWER_SELF

    def test_answerable_exactly_at_threshold_passes(self) -> None:
        d = decide_takeover(
            cands(2), res([0.9, 0.1], answerable=ANSWERABLE_T),
            answerable_threshold=ANSWERABLE_T, probability_threshold=PROB_T,
        )
        assert d.action == ACTION_ANSWER_SELF

    def test_decision_carries_both_grades_for_logging(self) -> None:
        d = decide_takeover(
            cands(2), res([0.2, 0.8]),
            answerable_threshold=ANSWERABLE_T, probability_threshold=PROB_T,
        )
        assert d.nouls[ANY_QID] == 0.9
        assert d.probabilities["option_1"] == 0.8


class TestLinks:
    """D 位点:代价偏向「别附」,阈值取高,且按 Choice 概率降序。"""

    def test_none_confident_returns_nothing(self) -> None:
        d = decide_links(
            cands(3), res([0.3, 0.2, 0.1]),
            answerable_threshold=ANSWERABLE_T, probability_threshold=0.45, max_links=3,
        )
        assert d.entry_ids == ()
        assert d.reason == "none_confident"

    def test_sorted_by_probability_descending(self) -> None:
        d = decide_links(
            cands(3), res([0.5, 0.9, 0.6]),
            answerable_threshold=ANSWERABLE_T, probability_threshold=0.45, max_links=3,
        )
        assert d.entry_ids == ("qa_1", "qa_2", "qa_0")

    def test_respects_max_links(self) -> None:
        d = decide_links(
            cands(3), res([0.9, 0.95, 0.99]),
            answerable_threshold=ANSWERABLE_T, probability_threshold=0.45, max_links=2,
        )
        assert d.entry_ids == ("qa_2", "qa_1")

    def test_not_answerable_falls_back_to_local(self) -> None:
        """答不上就不动本地闸门,而不是替它做"全灭"决定。"""
        d = decide_links(
            cands(2), res([0.9, 0.8], answerable=0.1),
            answerable_threshold=ANSWERABLE_T, probability_threshold=0.45, max_links=3,
        )
        assert d.action == ACTION_FALLBACK

    def test_unusable_falls_back(self) -> None:
        d = decide_links(
            cands(2), JevResult(ok=False),
            answerable_threshold=ANSWERABLE_T, probability_threshold=0.45, max_links=3,
        )
        assert d.action == ACTION_FALLBACK

    def test_ties_broken_deterministically(self) -> None:
        d = decide_links(
            cands(2), res([0.6, 0.6]),
            answerable_threshold=ANSWERABLE_T, probability_threshold=0.45, max_links=2,
        )
        assert d.entry_ids == ("qa_0", "qa_1")


class TestThresholdDirection:
    """C 偏「有把握」/ D 偏「别附」——同一条证据,两处倾向相反。

    若日后有人把两处阈值并成一个,这里会红。
    """

    MID = 0.4

    def test_same_evidence_opposite_lean(self) -> None:
        r = res([self.MID, 0.3])
        c_take = decide_takeover(
            cands(2), r,
            answerable_threshold=ANSWERABLE_T, probability_threshold=self.MID,
        )
        d_link = decide_links(
            cands(2), r,
            answerable_threshold=ANSWERABLE_T, probability_threshold=0.55, max_links=3,
        )
        assert c_take.action == ACTION_ANSWER_SELF, "C 位点把握一般就该自己答"
        assert d_link.entry_ids == (), "D 位点把握一般就不该附链"
