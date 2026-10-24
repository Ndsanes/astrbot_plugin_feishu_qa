"""群消息旁听:把"一条提问"拼成"会话片段",再决定要不要主动答。

背景(2026-10-24 讨论):群里大量提问不会 @ 机器人,而且一条提问经常被拆成
好几条消息送出("我装完了" → "打开就闪退" → "怎么办"),中间可能隔几分钟。
按单条消息判定,检索拿到的是半边话;按很短的间隔合并,打字慢一点就断成两段。

本模块只做纯逻辑(不依赖 AstrBot、不发消息、不碰网络):

- ``GroupWatcher`` 维护每个群、每个发言人的 burst(同人连续消息)。合并条件
  是"同一个人 + 期间没有别人实质插话 + 总时长 ≤10 分钟 + ≤8 条";续话词
  (而且/还有/然后/…)允许跨过更长间隔接着拼。结构优先,时间只做上限。
- ``candidate_windows`` 取 burst 内**所有连续子窗口**:"问题在前、细节在后"
  与"细节在前、问题在后"都能覆盖。窗口里必须真的出现问句才允许开口,这条
  让长窗口变安全——"我装完了"单独一条永远不会触发。
- ``ProactivePlanner`` 对每个窗口跑本地确定性检索,取最高分的窗口;只有
  HIGH 才建议出手,与 Tier-0 直答同一套证据标准。
- ``ProactiveLimiter`` 管每群每天 / 每人每小时 / 同问题 24h 去重。
- 热聊判定:最近 30s 内别人已经说了 ≥3 条,不插话。

对应主程序的四个模式(见 ``_conf_schema.json`` 的 ``PROACTIVE_MODE``):
``off`` 不旁听;``shadow`` 只落决策日志;``human_only`` 仅在"有人 @ 真人且
无人回应"时兜底;``first_ask`` 首问就答。
"""

from __future__ import annotations

import hashlib
import re
import time
from collections import deque
from dataclasses import dataclass, field

from .retrieval.scorer import Confidence, Retriever, SearchResult

_WS_RE = re.compile(r"\s+")

# 问句标记:出现任意一个才认为这一段"真的在提问/求助"。症状词(闪退/打不开/
# 没声音)也算,因为支持群里"打不开"本身就是求助句。
QUESTION_MARKERS = (
    "?",
    "？",
    "吗",
    "呢",
    "怎么",
    "怎样",
    "为什么",
    "为啥",
    "如何",
    "哪",
    "能不能",
    "可不可以",
    "是不是",
    "有没有",
    "什么",
    "多少",
    "哪个",
    "区别",
    "值不值",
    "要不要",
    "求助",
    "请问",
    "报错",
    "怎么办",
    # 症状词本身就是求助句("扫不到""没声音"),支持群里很少写成完整问句。
    "闪退",
    "打不开",
    "没声音",
    "没反应",
    "找不到",
    "扫不到",
    "连不上",
    "不能用",
    "识别不到",
    "失败",
    "崩溃",
    "卡死",
    "咋",
)
# 续话词:隔了很久才补的一句,仍是同一个问题的上下文。
CONTINUATION_MARKERS = (
    "而且",
    "还有",
    "然后",
    "但是",
    "另外",
    "就是",
    "现在",
    "还是",
    "并且",
    "以及",
    "对了",
    "补充",
    "同时",
    "但",
)
# 收尾词:提问者自己说问题解决了 → 不必再兜底。
CLOSING_MARKERS = ("解决了", "好了", "没事了", "谢谢", "知道了", "搞定了", "不用了")


def _squash(text: str) -> str:
    return _WS_RE.sub(" ", (text or "").strip())


def has_question(text: str) -> bool:
    """这一段是否真的在提问/求助(含症状词)。"""
    lowered = _squash(text).lower()
    return any(marker in lowered for marker in QUESTION_MARKERS)


def starts_with_continuation(text: str) -> bool:
    squashed = _squash(text)
    return any(squashed.startswith(marker) for marker in CONTINUATION_MARKERS)


def is_closing(text: str) -> bool:
    """短句里出现收尾词才算解决;带症状/问句的"谢谢,但还是闪退"不算。"""
    squashed = _squash(text)
    if len(squashed) > 20 or has_question(squashed):
        return False
    return any(marker in squashed for marker in CLOSING_MARKERS)


def is_command(text: str) -> bool:
    return _squash(text).startswith("/")


def is_substantive(text: str) -> bool:
    """别人的插话是否会打断 burst。

    "同问 / +1 / 顶"这类短插话不打断(它反而是"我也想知道"的信号),
    但只要带问句或足够长,就视为另一个话题,原 burst 关闭。
    """
    squashed = _squash(text)
    if not squashed:
        return False
    if has_question(squashed):
        return True
    return len(squashed) > 4


def _query_digest(text: str) -> str:
    return hashlib.sha256(_squash(text).lower().encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class WatchMessage:
    """旁听到的一条群消息(已剥掉 @ 与引用,只留纯文本)。"""

    group_id: str
    sender_id: str
    text: str
    ts: float
    audience: str = "group"  # group | human(对真人说) | bot(对机器人说)
    targets: frozenset[str] = frozenset()
    has_image: bool = False


@dataclass
class Burst:
    """同一个发言人的一段连续消息。"""

    group_id: str
    sender_id: str
    started_at: float
    updated_at: float
    messages: list[WatchMessage] = field(default_factory=list)
    audience: str = "group"  # 粘性:human 一旦出现就一直按真人提问处理
    targets: set[str] = field(default_factory=set)
    closed: bool = False  # 别人插话后不再往里并新碎片,但这一段仍可被评估
    answered_entry_id: str | None = None
    answered_at: float | None = None

    @property
    def text(self) -> str:
        return " ".join(_squash(m.text) for m in self.messages if _squash(m.text))

    @property
    def span(self) -> float:
        return self.updated_at - self.started_at


def _merge_audience(current: str, incoming: str) -> str:
    """human 优先级最高:一条 @真人 就足以让整段按"对人提问"处理。"""
    if current == "human" or incoming == "human":
        return "human"
    if current == "bot" or incoming == "bot":
        return "bot"
    return "group"


class GroupWatcher:
    """群内 burst 拼装与热聊判定(纯内存,重启即清空)。

    内存有界:每个群只保留最近 ``recent_limit`` 条消息,burst 超过
    ``max_span_seconds`` 自动清掉,不会随运行时间无限增长。
    """

    def __init__(
        self,
        *,
        max_span_seconds: float = 600.0,
        max_messages: int = 8,
        continuation_gap_seconds: float = 120.0,
        hot_window_seconds: float = 30.0,
        hot_messages: int = 3,
        recent_limit: int = 200,
    ) -> None:
        self.max_span_seconds = float(max_span_seconds)
        self.max_messages = max(1, int(max_messages))
        self.continuation_gap_seconds = float(continuation_gap_seconds)
        self.hot_window_seconds = float(hot_window_seconds)
        self.hot_messages = max(1, int(hot_messages))
        self.recent_limit = max(10, int(recent_limit))
        self._bursts: dict[tuple[str, str], Burst] = {}
        self._recent: dict[str, deque[WatchMessage]] = {}

    # ── 旁听 ──

    def note(self, message: WatchMessage) -> Burst | None:
        """记一条消息并返回它所属的 burst。

        返回 None 表示这条消息不参与主动答:空文本、指令、或**对机器人说的**
        (后者交给现有 Tier-0 路径,避免同一条提问被答两次)。
        """
        text = _squash(message.text)
        if not text or is_command(text):
            return None
        self._remember(message)
        if message.audience == "bot":
            return None

        key = (message.group_id, message.sender_id)
        burst = self._bursts.get(key)
        if burst is not None and not self._continues(burst, message, text):
            self._bursts.pop(key, None)
            burst = None
        if burst is None:
            burst = Burst(
                group_id=message.group_id,
                sender_id=message.sender_id,
                started_at=message.ts,
                updated_at=message.ts,
            )
            self._bursts[key] = burst

        burst.messages.append(message)
        # 超过条数上限时丢最早的一条,保留最近的上下文
        if len(burst.messages) > self.max_messages:
            burst.messages = burst.messages[-self.max_messages :]
        burst.updated_at = message.ts
        burst.audience = _merge_audience(burst.audience, message.audience)
        burst.targets |= set(message.targets)
        self._prune(message.group_id, message.ts)

        if is_substantive(text):
            # 别人开始说正事:之前那些同人 burst 一律"封口"(不再并新碎片)。
            # 但**不删除**——它还要留给定时器评估/兜底,也要能看出"有人回应了"。
            for other_key, other in self._bursts.items():
                if other_key[0] == message.group_id and other_key[1] != message.sender_id:
                    other.closed = True
        return burst

    def get(self, group_id: str, sender_id: str) -> Burst | None:
        return self._bursts.get((group_id, sender_id))

    def mark_answered(self, burst: Burst, entry_id: str, *, now: float | None = None) -> None:
        """标记这段已答过:后续碎片不再重复触发(除非用续话词接着补)。"""
        burst.answered_entry_id = entry_id
        burst.answered_at = time.time() if now is None else now

    def is_hot(self, group_id: str, sender_id: str, now: float) -> bool:
        """最近 30s 内除提问者外已经说了 ≥3 条 → 正在热聊,不插话。"""
        recent = self._recent.get(group_id)
        if not recent:
            return False
        count = 0
        for message in reversed(recent):
            if now - message.ts > self.hot_window_seconds:
                break
            if message.sender_id != sender_id:
                count += 1
                if count >= self.hot_messages:
                    return True
        return False

    def addressed_human_replied(self, burst: Burst) -> bool:
        """有人回应了吗(被 @ 的真人开口、别人引用提问者、提问者说解决了)。

        宽限期到点前用它撤销兜底——被人回过的问题不该再由机器人补一刀。
        """
        recent = self._recent.get(burst.group_id)
        if not recent:
            return False
        for message in recent:
            if message.ts < burst.started_at:
                continue
            if message.sender_id == burst.sender_id:
                if is_closing(message.text):
                    return True
                continue
            if message.sender_id in burst.targets:
                return True
            if burst.sender_id in message.targets:
                return True
        return False

    def forget_group(self, group_id: str) -> None:
        for key in [k for k in self._bursts if k[0] == group_id]:
            self._bursts.pop(key, None)
        self._recent.pop(group_id, None)

    # ── 内部 ──

    def _continues(self, burst: Burst, message: WatchMessage, text: str) -> bool:
        if burst.closed:
            return False
        gap = message.ts - burst.updated_at
        if gap > self.max_span_seconds:
            return False
        if len(burst.messages) >= self.max_messages:
            return False
        continuation = starts_with_continuation(text)
        # 已答过的一段:只有续话词才继续吸进来,否则算新问题。
        if burst.answered_entry_id is not None and not continuation:
            return False
        if continuation:
            return True
        return gap <= self.continuation_gap_seconds

    def _remember(self, message: WatchMessage) -> None:
        recent = self._recent.setdefault(message.group_id, deque(maxlen=self.recent_limit))
        recent.append(message)

    def _prune(self, group_id: str, now: float) -> None:
        stale = [
            key
            for key, burst in self._bursts.items()
            if key[0] == group_id and now - burst.updated_at > self.max_span_seconds
        ]
        for key in stale:
            self._bursts.pop(key, None)


def candidate_windows(burst: Burst) -> list[list[WatchMessage]]:
    """burst 内全部连续子窗口(≤8 条 → 最多 36 个)。"""
    messages = [m for m in burst.messages if _squash(m.text)]
    return [
        messages[i : j + 1]
        for i in range(len(messages))
        for j in range(i, len(messages))
    ]


def assemble(messages: list[WatchMessage], *, max_chars: int = 400) -> str:
    text = " ".join(_squash(m.text) for m in messages if _squash(m.text))
    return text[:max_chars]


@dataclass
class ProactiveDecision:
    """是否出手、用哪段文本、命中哪一条。"""

    action: str  # answer | wait
    reason: str
    text: str = ""
    result: SearchResult | None = None
    window_size: int = 0


class ProactivePlanner:
    """burst → 决策。只用本地确定性检索,零 LLM、零网络。"""

    def __init__(self, retriever: Retriever, *, max_chars: int = 400) -> None:
        self.retriever = retriever
        self.max_chars = max_chars

    def plan(self, burst: Burst) -> ProactiveDecision:
        best_high: tuple[str, SearchResult, int] | None = None
        best_low: tuple[str, SearchResult, int] | None = None
        saw_question = False
        saw_topic = False

        for window in candidate_windows(burst):
            text = assemble(window, max_chars=self.max_chars)
            if not has_question(text):
                continue
            saw_question = True
            # 只有"怎么办/怎么"这类无主题词的碎片不许出手:它们在标题里
            # 满语料都是,containment 会给满分,答出去等于瞎猜。
            if not self.retriever.topic_words(text):
                continue
            saw_topic = True
            results = self.retriever.search(text, top_k=1)
            if not results:
                continue
            result = results[0]
            supported = self.retriever.supports_query(result.entry, text)
            if result.confidence == Confidence.HIGH and supported:
                if best_high is None or result.score > best_high[1].score:
                    best_high = (text, result, len(window))
            elif best_low is None or result.score > best_low[1].score:
                best_low = (text, result, len(window))

        if best_high is not None:
            text, result, window_size = best_high
            return ProactiveDecision(
                action="answer",
                reason="high_confidence",
                text=text,
                result=result,
                window_size=window_size,
            )
        if best_low is not None:
            _, result, window_size = best_low
            return ProactiveDecision(
                action="wait",
                reason=f"below_high:{result.confidence}",
                result=result,
                window_size=window_size,
            )
        if saw_topic:
            return ProactiveDecision(action="wait", reason="no_match")
        if saw_question:
            return ProactiveDecision(action="wait", reason="no_topic_words")
        return ProactiveDecision(action="wait", reason="no_question")


class ProactiveLimiter:
    """主动答的限流与去重(内存,重启清零;宁可少答,不可刷屏)。"""

    def __init__(
        self,
        *,
        per_group_day: int = 8,
        per_user_hour: int = 2,
        duplicate_ttl_seconds: float = 86400.0,
    ) -> None:
        self.per_group_day = max(0, int(per_group_day))
        self.per_user_hour = max(0, int(per_user_hour))
        self.duplicate_ttl_seconds = float(duplicate_ttl_seconds)
        self._group: dict[str, deque[float]] = {}
        self._user: dict[str, deque[float]] = {}
        self._hashes: dict[str, float] = {}

    def allow(
        self, *, group_id: str, sender_id: str, text: str, now: float
    ) -> tuple[bool, str]:
        key = f"{group_id}|{_query_digest(text)}"
        seen = self._hashes.get(key)
        if seen is not None and now - seen < self.duplicate_ttl_seconds:
            return False, "duplicate"
        if len(self._within(self._group.get(group_id), 86400.0, now)) >= self.per_group_day:
            return False, "group_daily_cap"
        if len(self._within(self._user.get(sender_id), 3600.0, now)) >= self.per_user_hour:
            return False, "user_hourly_cap"
        return True, ""

    def record(self, *, group_id: str, sender_id: str, text: str, now: float) -> None:
        self._within(self._group.setdefault(group_id, deque()), 86400.0, now).append(now)
        self._within(self._user.setdefault(sender_id, deque()), 3600.0, now).append(now)
        self._hashes[f"{group_id}|{_query_digest(text)}"] = now

    @staticmethod
    def _within(
        stamps: deque[float] | None, window: float, now: float
    ) -> deque[float]:
        """丢掉窗口外的记录;返回同一 deque,方便调用方直接 append。"""
        if stamps is None:
            stamps = deque()
        while stamps and now - stamps[0] > window:
            stamps.popleft()
        return stamps
