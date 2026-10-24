"""群消息旁听(burst 拼装/窗口打分/限流)测试。

这里的用例对应用户 2026-10-24 提的两个现实约束:
  1. 一条提问会被拆成好几条消息,间隔可能好几分钟;
  2. 有人直接 @ 群主问问题(跳过机器人)。

所以断言的重心不是"能不能答",而是"什么时候**不该**答":
没人问、问的是别人、正在热聊、同一问题刚答过——这些都必须闭嘴。
"""

from __future__ import annotations

from astrbot_plugin_feishu_qa.corpus.model import QaEntry
from astrbot_plugin_feishu_qa.group_watch import (
    Burst,
    GroupWatcher,
    ProactiveLimiter,
    ProactivePlanner,
    WatchMessage,
    candidate_windows,
    has_question,
    is_closing,
    is_substantive,
)
from astrbot_plugin_feishu_qa.retrieval.scorer import Retriever


def msg(
    ts: float,
    text: str,
    *,
    sender: str = "u1",
    group: str = "g1",
    audience: str = "group",
    targets: frozenset[str] = frozenset(),
    has_image: bool = False,
) -> WatchMessage:
    return WatchMessage(
        group_id=group,
        sender_id=sender,
        text=text,
        ts=ts,
        audience=audience,
        targets=targets,
        has_image=has_image,
    )


def _entry(entry_id: str, title: str, body: str, tags: list[str]) -> QaEntry:
    return QaEntry(
        id=entry_id,
        section_path=["测试章节"],
        category="测试章节",
        symptom_tags=tags,
        title=title,
        raw_title=title,
        body=body,
        source="fuuumusic",
    )


def _retriever() -> Retriever:
    return Retriever(
        [
            _entry(
                "qa_crash",
                "Cakewalk 打开就闪退怎么办",
                "先重装 VC 运行库,再检查安装路径。",
                ["闪退", "打开就闪退", "打不开"],
            ),
            _entry(
                "qa_template",
                "Cakewalk 没有自带模板怎么办",
                "自己新建模板并保存。",
                ["没有自带模板"],
            ),
        ],
        high_threshold=5.0,
        medium_threshold=2.0,
    )


class TestQuestionMarkers:
    def test_question_needs_marker(self) -> None:
        assert has_question("打开就闪退怎么办")
        assert has_question("插件扫不到")
        assert not has_question("我装完了")

    def test_closing_words(self) -> None:
        assert is_closing("解决了,谢谢")
        assert not is_closing("谢谢,但是打开还是闪退")

    def test_interjection_is_not_substantive(self) -> None:
        assert not is_substantive("同问")
        assert is_substantive("我也遇到了一样的情况")
        assert is_substantive("同问,而且我的还会爆音")


class TestBurstAssembly:
    def test_fragments_within_gap_merge(self) -> None:
        watcher = GroupWatcher(continuation_gap_seconds=120)
        for ts, text in ((0, "我装完了"), (5, "打开就闪退"), (10, "怎么办")):
            burst = watcher.note(msg(ts, text))
        assert burst is not None
        assert len(burst.messages) == 3
        assert burst.text == "我装完了 打开就闪退 怎么办"

    def test_slow_typing_needs_continuation_word(self) -> None:
        watcher = GroupWatcher(continuation_gap_seconds=120, max_span_seconds=600)
        watcher.note(msg(0, "我装完了"))
        # 3 分钟后没有续话词 → 算新的一段
        first = watcher.get("g1", "u1")
        assert first is not None
        burst = watcher.note(msg(180, "打开就闪退"))
        assert burst is not None and burst.started_at == 180
        assert burst.text == "打开就闪退"

    def test_slow_typing_with_continuation_word_merges(self) -> None:
        watcher = GroupWatcher(continuation_gap_seconds=120, max_span_seconds=600)
        watcher.note(msg(0, "我装完了"))
        burst = watcher.note(msg(180, "而且打开就闪退"))
        assert burst is not None
        assert burst.started_at == 0
        assert len(burst.messages) == 2

    def test_other_speaker_closes_burst_but_keeps_it(self) -> None:
        watcher = GroupWatcher()
        watcher.note(msg(0, "我装完了"))
        watcher.note(msg(1, "插件扫不到", sender="u2"))
        burst = watcher.get("g1", "u1")
        assert burst is not None and burst.closed is True
        # 封口后同一个人再说,算新的一段
        fresh = watcher.note(msg(10, "而且打开就闪退"))
        assert fresh is not None and fresh is not burst

    def test_short_interjection_does_not_break(self) -> None:
        watcher = GroupWatcher()
        watcher.note(msg(0, "我装完了"))
        watcher.note(msg(1, "同问", sender="u2"))
        burst = watcher.note(msg(2, "打开就闪退"))
        assert burst is not None and len(burst.messages) == 2

    def test_span_cap_starts_new_burst(self) -> None:
        watcher = GroupWatcher(max_span_seconds=600)
        watcher.note(msg(0, "我装完了"))
        burst = watcher.note(msg(601, "而且打开就闪退"))
        assert burst is not None and burst.started_at == 601

    def test_max_messages_keeps_recent(self) -> None:
        watcher = GroupWatcher(max_messages=3, max_span_seconds=9999)
        for i in range(6):
            watcher.note(msg(i, f"第{i}条"))
        burst = watcher.get("g1", "u1")
        assert burst is not None
        assert [m.text for m in burst.messages] == ["第3条", "第4条", "第5条"]

    def test_commands_and_bot_directed_are_ignored(self) -> None:
        watcher = GroupWatcher()
        assert watcher.note(msg(0, "/问 闪退")) is None
        assert watcher.note(msg(1, "在吗", audience="bot")) is None
        assert watcher.get("g1", "u1") is None

    def test_audience_human_is_sticky(self) -> None:
        watcher = GroupWatcher()
        watcher.note(msg(0, "@群主 闪退怎么办", audience="human", targets=frozenset({"owner"})))
        burst = watcher.note(msg(5, "装完打开就这样"))
        assert burst is not None
        assert burst.audience == "human"
        assert burst.targets == {"owner"}

    def test_answered_burst_does_not_swallow_new_question(self) -> None:
        watcher = GroupWatcher()
        burst = watcher.note(msg(0, "闪退怎么办"))
        assert burst is not None
        watcher.mark_answered(burst, "qa_crash", now=0)
        fresh = watcher.note(msg(30, "那导出没反应呢"))
        assert fresh is not None and fresh is not burst

    def test_continuation_after_answer_stays_in_burst(self) -> None:
        watcher = GroupWatcher()
        burst = watcher.note(msg(0, "闪退怎么办"))
        assert burst is not None
        watcher.mark_answered(burst, "qa_crash", now=0)
        same = watcher.note(msg(30, "还有就是爆音"))
        assert same is burst


class TestHotChat:
    def test_three_others_in_window_is_hot(self) -> None:
        watcher = GroupWatcher(hot_window_seconds=30, hot_messages=3)
        for ts in (0, 5, 10):
            watcher.note(msg(ts, "我也一样", sender=f"u{ts}"))
        assert watcher.is_hot("g1", "u1", now=12) is True

    def test_own_messages_do_not_make_it_hot(self) -> None:
        watcher = GroupWatcher(hot_window_seconds=30, hot_messages=3)
        for ts in (0, 5, 10):
            watcher.note(msg(ts, f"第{ts}条"))
        assert watcher.is_hot("g1", "u1", now=12) is False

    def test_old_messages_expire(self) -> None:
        watcher = GroupWatcher(hot_window_seconds=30, hot_messages=3)
        for ts in (0, 5, 10):
            watcher.note(msg(ts, "我也一样", sender=f"u{ts}"))
        assert watcher.is_hot("g1", "u1", now=100) is False


class TestAddressedHumanReplied:
    def test_target_speaks(self) -> None:
        watcher = GroupWatcher()
        watcher.note(msg(0, "@群主 闪退怎么办", audience="human", targets=frozenset({"owner"})))
        burst = watcher.get("g1", "u1")
        assert burst is not None
        watcher.note(msg(20, "你重装一下试试", sender="owner"))
        assert watcher.addressed_human_replied(burst) is True

    def test_someone_quotes_the_asker(self) -> None:
        watcher = GroupWatcher()
        watcher.note(msg(0, "@群主 闪退怎么办", audience="human", targets=frozenset({"owner"})))
        burst = watcher.get("g1", "u1")
        assert burst is not None
        watcher.note(msg(20, "我帮你看看", sender="u9", targets=frozenset({"u1"})))
        assert watcher.addressed_human_replied(burst) is True

    def test_asker_says_solved(self) -> None:
        watcher = GroupWatcher()
        watcher.note(msg(0, "@群主 闪退怎么办", audience="human", targets=frozenset({"owner"})))
        burst = watcher.get("g1", "u1")
        assert burst is not None
        watcher.note(msg(20, "解决了,谢谢"))
        assert watcher.addressed_human_replied(burst) is True

    def test_unrelated_chatter_does_not_cancel(self) -> None:
        watcher = GroupWatcher()
        watcher.note(msg(0, "@群主 闪退怎么办", audience="human", targets=frozenset({"owner"})))
        burst = watcher.get("g1", "u1")
        assert burst is not None
        watcher.note(msg(20, "今天好热", sender="u9"))
        assert watcher.addressed_human_replied(burst) is False


class TestWindowsAndPlanner:
    def _burst(self, texts: list[str]) -> Burst:
        watcher = GroupWatcher()
        burst = None
        for i, text in enumerate(texts):
            burst = watcher.note(msg(i, text))
        assert burst is not None
        return burst

    def test_all_contiguous_windows(self) -> None:
        assert len(candidate_windows(self._burst(["a", "b", "c"]))) == 6

    def test_question_last_fragment_reaches_high(self) -> None:
        planner = ProactivePlanner(_retriever())
        decision = planner.plan(self._burst(["我装完了", "打开就闪退", "怎么办"]))
        assert decision.action == "answer"
        assert decision.result is not None and decision.result.entry.id == "qa_crash"
        assert decision.window_size >= 1

    def test_question_first_then_details(self) -> None:
        planner = ProactivePlanner(_retriever())
        decision = planner.plan(self._burst(["闪退怎么办", "装完打开就这样"]))
        assert decision.action == "answer"
        assert decision.result is not None and decision.result.entry.id == "qa_crash"
        assert decision.window_size >= 1

    def test_no_question_waits(self) -> None:
        planner = ProactivePlanner(_retriever())
        decision = planner.plan(self._burst(["我装完了", "还没打开"]))
        assert decision.action == "wait"
        assert decision.reason == "no_question"

    def test_question_without_match_waits(self) -> None:
        planner = ProactivePlanner(_retriever())
        decision = planner.plan(self._burst(["今天天气怎么样"]))
        assert decision.action == "wait"
        assert decision.reason.startswith("below_high")

    def test_lower_confidence_window_does_not_win(self) -> None:
        # 只有带上症状词的那段才能到 HIGH;单独一句"怎么办"不该触发
        planner = ProactivePlanner(_retriever())
        decision = planner.plan(self._burst(["怎么办", "插件扫不到"]))
        assert decision.action == "wait"


class TestLimiter:
    def test_duplicate_within_ttl(self) -> None:
        limiter = ProactiveLimiter()
        ok, _ = limiter.allow(group_id="g1", sender_id="u1", text="闪退怎么办", now=0)
        assert ok
        limiter.record(group_id="g1", sender_id="u1", text="闪退怎么办", now=0)
        ok, reason = limiter.allow(group_id="g1", sender_id="u1", text="闪退怎么办", now=600)
        assert not ok and reason == "duplicate"

    def test_group_daily_cap(self) -> None:
        limiter = ProactiveLimiter(per_group_day=2, per_user_hour=99)
        for i in range(2):
            limiter.record(group_id="g1", sender_id=f"u{i}", text=f"问题{i}", now=i)
        ok, reason = limiter.allow(group_id="g1", sender_id="u9", text="另一个问题", now=10)
        assert not ok and reason == "group_daily_cap"

    def test_user_hourly_cap(self) -> None:
        limiter = ProactiveLimiter(per_group_day=99, per_user_hour=1)
        limiter.record(group_id="g1", sender_id="u1", text="问题一", now=0)
        ok, reason = limiter.allow(group_id="g1", sender_id="u1", text="问题二", now=60)
        assert not ok and reason == "user_hourly_cap"

    def test_user_cap_expires_after_an_hour(self) -> None:
        limiter = ProactiveLimiter(per_group_day=99, per_user_hour=1)
        limiter.record(group_id="g1", sender_id="u1", text="问题一", now=0)
        ok, _ = limiter.allow(group_id="g1", sender_id="u1", text="问题二", now=3700)
        assert ok

    def test_group_cap_expires_after_a_day(self) -> None:
        limiter = ProactiveLimiter(per_group_day=1, per_user_hour=99)
        limiter.record(group_id="g1", sender_id="u1", text="问题一", now=0)
        assert limiter.allow(group_id="g1", sender_id="u2", text="问题二", now=3700)[0] is False
        assert limiter.allow(group_id="g1", sender_id="u2", text="问题二", now=90000)[0] is True

    def test_same_question_in_another_group_is_not_duplicate(self) -> None:
        limiter = ProactiveLimiter()
        limiter.record(group_id="g1", sender_id="u1", text="闪退怎么办", now=0)
        ok, _ = limiter.allow(group_id="g2", sender_id="u2", text="闪退怎么办", now=10)
        assert ok
