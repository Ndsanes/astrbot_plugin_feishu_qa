"""Jev(TypeSafe System One 模型)最小客户端。

**唯一职责**:把一段 state 与若干 Noul 问题发给 `POST /v1/systemone`,取回
每个问题的概率值。除此之外不做任何判断——"该信到什么程度"的策略属于调用方
(answer/router 与 main.py),不在这里。

三条硬约束(决定了本模块的形状):

1. **绝不抛异常**。`evaluate` 在任何失败路径下都返回 `JevResult(ok=False)`,
   调用方据此无条件回落到本地确定性判定。Jev 是外部托管 API,鉴权过期、
   网络抖动、超时、5xx、响应结构变更都可能发生,而这个插件的应答链路
   必须在这些情况下依然能用。**观测/增强能力不得成为业务链路的单点。**

2. **零第三方依赖**。插件运行在 AstrBot 进程里,§0.14 最小依赖原则不允许为
   一个外部 API 引入 SDK,故用标准库 urllib。

3. **不泄漏凭据**。API key 只出现在请求头,不进日志、不进异常消息、不进
   DecisionLog(Modu §0.37 Logging Contract:禁记 secret)。

只实现 Noul:本次的两个接管点都是"这条够不够"的二元判断,Choice/Score 的
表达能力在这里用不上,加了就是 speculative abstraction(Modu §0.33)。
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field

logger = logging.getLogger("feishu_qa.jev")

DEFAULT_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"


@dataclass
class JevResult:
    """一次 Jev 调用的结果。

    同时承载两种答案,因为它们回答的不是同一个问题:
      - ``nouls``   —— 绝对判断:"这条/这堆材料答不答得上"。彼此不竞争。
      - ``choices`` —— 相对判断:"这几个里哪个最对"。概率之和为 1。

    ``ok=False`` 时两者必为空——调用方**必须**检查 ok,否则会把"没问成"
    误读成 "问出了低分"。这正是失败与低置信必须分开的原因。
    """

    ok: bool
    nouls: dict[str, float] = field(default_factory=dict)
    choices: dict[str, ChoiceAnswer] = field(default_factory=dict)
    model: str = ""
    input_tokens: int = 0
    error: str = ""

    def noul(self, question_id: str) -> float | None:
        """取单个 Noul 问题的概率;未问到或未启用返回 None。"""
        return self.nouls.get(question_id) if self.ok else None

    def choice(self, question_id: str) -> ChoiceAnswer | None:
        """取单个 Choice 问题的答案;未问到或未启用返回 None。"""
        return self.choices.get(question_id) if self.ok else None


@dataclass
class ChoiceAnswer:
    """一个 Choice 问题的答案。

    ``probabilities`` 是**相对**分布(和为 1),与逐候选 Noul 的绝对分不可
    互相换算,阈值也不能跨问题类型照搬(jaggedness 第 8 条)。
    """

    choice: str
    probabilities: dict[str, float]
    confidence: float | None = None


def noul_question(
    instructions: str, *, true: str = "", false_: str = ""
) -> dict:
    """构造一个 Noul 问题。

    ``criteria`` 按官方文档给 yes/no 两侧各写一句**具体**描述:jev-1.13 的
    jaggedness 第 1 条是"字面阅读",第 7 条是"instructions 与 criteria 矛盾
    会变差",所以两侧都要写死,不要留空。
    """
    q: dict = {"type": "noul", "instructions": instructions}
    criteria: dict = {}
    if true:
        criteria["true"] = true
    if false_:
        criteria["false"] = false_
    if criteria:
        q["criteria"] = criteria
    return q


def choice_question(instructions: str, criteria: dict) -> dict:
    """构造一个 Choice 问题——**在候选里选一个**时必须用它,不能用 Noul 代替。

    官方 jaggedness 第 8 条把这件事写死了:逐候选的 Noul 是**绝对**判断,
    "这个候选答不答得上";Choice 是**相对**判断,"这几个里哪个最对"。两者回答
    的不是同一个问题,绝对分数之间也不竞争——"这条 0.9"并不会让另一条变成
    0.1。实测(2026-09-26,本语料 8 题同 category 干扰项):逐候选 Noul 让
    正确条目排第一只有 3/8,换成 Choice 后 6/8;22 题规模上 Choice 20/22。

    官方 skill_suggestion cookbook 给的组合是:Choice 选,外加 Noul 判断
    该不该用这个候选集——本模块的 ``JevResult`` 同时持有 choice 与 noul 两种
    答案,正是为这个组合准备的。
    """
    return {"type": "choice", "instructions": instructions, "criteria": dict(criteria)}


class JevClient:
    """极简 Jev 客户端。线程安全(无共享可变状态)。"""

    def __init__(
        self,
        api_key: str,
        *,
        endpoint: str = DEFAULT_ENDPOINT,
        model: str = DEFAULT_MODEL,
        timeout: float = 5.0,
        transport=None,
    ) -> None:
        self._api_key = api_key
        self._endpoint = endpoint
        self._model = model
        self._timeout = max(0.1, float(timeout))
        # transport 形如 (url, headers, body_bytes, timeout) -> dict
        # 仅供测试注入假响应;生产走 urllib。
        self._transport = transport or self._http_post

    @property
    def model(self) -> str:
        return self._model

    def evaluate(
        self, state: object, questions: dict[str, dict]
    ) -> JevResult:
        """一次调用并行求值全部 Noul 与 Choice。**任何失败返回 ok=False,不抛异常。**"""
        if not self._api_key or not questions:
            return JevResult(
                ok=False, error="no_api_key" if not self._api_key else "no_questions"
            )
        body = json.dumps(
            {"state": state, "model": self._model, "questions": questions},
            ensure_ascii=False,
        ).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            payload = self._transport(self._endpoint, headers, body, self._timeout)
        except Exception as exc:  # 传输层任何异常都不得外泄
            # 只记异常类型,不带 key/URL 之外的载荷
            logger.warning("[Jev] 调用失败,回落本地判定: %s", type(exc).__name__)
            return JevResult(ok=False, error=type(exc).__name__)

        nouls, choices, model, tokens = self._parse(payload)
        if not nouls and not choices:
            return JevResult(ok=False, error="bad_response", model=model)
        return JevResult(
            ok=True, nouls=nouls, choices=choices, model=model, input_tokens=tokens
        )

    @staticmethod
    def _parse(
        payload: object,
    ) -> tuple[dict[str, float], dict[str, ChoiceAnswer], str, int]:
        """从响应里抽 Noul 概率与 Choice 分布。结构不符的条目直接丢弃。

        逐条校验而不是整体信任:缺键/类型错/越界都只丢那一问,不让坏数据
        变成"看起来像 0.0 的低分"。
        """
        if not isinstance(payload, dict):
            return {}, {}, "", 0
        answers = payload.get("answers")
        if not isinstance(answers, dict):
            return {}, {}, "", 0
        nouls: dict[str, float] = {}
        choices: dict[str, ChoiceAnswer] = {}
        for qid, ans in answers.items():
            if not isinstance(ans, dict):
                continue
            kind = ans.get("type")
            if kind == "noul":
                v = ans.get("noul")
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    continue
                f = float(v)
                if 0.0 <= f <= 1.0:
                    nouls[str(qid)] = f
            elif kind == "choice":
                picked = ans.get("choice")
                probs = ans.get("probabilities")
                if not isinstance(picked, str) or not isinstance(probs, dict):
                    continue
                clean: dict[str, float] = {}
                for opt, p in probs.items():
                    if isinstance(p, bool) or not isinstance(p, (int, float)):
                        continue
                    f = float(p)
                    if 0.0 <= f <= 1.0:
                        clean[str(opt)] = f
                # 选中项必须出现在概率表里,否则这份分布不可用
                if picked not in clean or not clean:
                    continue
                conf = ans.get("confidence")
                conf_f = (
                    float(conf)
                    if isinstance(conf, (int, float)) and not isinstance(conf, bool)
                    else None
                )
                choices[str(qid)] = ChoiceAnswer(
                    choice=picked, probabilities=clean, confidence=conf_f
                )
        model = payload.get("model") if isinstance(payload.get("model"), str) else ""
        usage = payload.get("usage")
        tokens = 0
        if isinstance(usage, dict):
            t = usage.get("input_tokens")
            if isinstance(t, int) and not isinstance(t, bool):
                tokens = t
        return nouls, choices, model, tokens

    @staticmethod
    def _http_post(
        url: str, headers: dict, body: bytes, timeout: float
    ) -> dict:
        """标准库 POST。HTTPError 携带状态码,便于区分鉴权与其它故障。"""
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            raw = resp.read()
        return json.loads(raw) if raw else {}
