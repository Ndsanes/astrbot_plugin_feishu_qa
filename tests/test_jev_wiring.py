"""Jev 接线测试(不联网、不需要 key,靠注入假 transport)。

纯策略在 test_jev_policy.py 已覆盖;这里覆盖**接线**——bug 恰恰住在
"判完之后有没有真的改行为"这一步,纯函数测试看不见。

覆盖的不变量:
  - Jev 说够格 → 真的自己答了并阻断 Agent;
  - Jev 说交回 → 真的不答不阻断;
  - Jev 不可用/未启用 → 行为与 v0.9.0 完全一致(走本地判定);
  - API key 绝不出现在决策日志里。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from astrbot.api.event import AstrMessageEvent

from astrbot_plugin_feishu_qa.jev.client import JevClient
from astrbot_plugin_feishu_qa.jev.policy import ANY_QID, RANK_QID, option_key
from astrbot_plugin_feishu_qa.main import FeishuQaPlugin

FIXTURES = Path(__file__).parent / "fixtures"

# MEDIUM 区的真实问题(2026-09-26 实测 3.56 分,top-1 为错答)
MEDIUM_QUERY = "效果器怎么添加"
# HIGH 区(实测 20.31)
HIGH_QUERY = "声卡设置没问题但是cakewalk就是没声音"
# 本地 linkable_entries 闸门实测放行 2 条的查询(用于 D 位点的"与 v0.9.0 一致"对照)
GATE_PASSING_QUERY = "cakewalk没有自带模板怎么办"


def _answer(probs: list[float], *, answerable: float = 0.9) -> dict:
    """Choice 分布 + Noul 门槛,模拟 jev-1.13 的真实响应形状。

    **不做自动归一化**:补齐余量会把刻意构造的"分布很平"变成"某条独大",
    于是测试想验证的边界被 helper 自己改掉了。概率必须由调用方显式给足。
    """
    probs = list(probs)
    assert abs(sum(probs) - 1.0) < 1e-6, f"Choice 概率应和为 1,收到 {probs}"
    return {
        "model": "jev-1.13.0",
        "answers": {
            RANK_QID: {
                "type": "choice",
                "choice": option_key(max(range(len(probs)), key=lambda i: probs[i])),
                "probabilities": {option_key(i): v for i, v in enumerate(probs)},
                "confidence": 0.7,
            },
            ANY_QID: {"type": "noul", "noul": answerable},
        },
        "usage": {"input_tokens": 1200, "output_tokens": 30},
    }


def _patch_jev(monkeypatch, payload=None, *, raises: Exception | None = None):
    """让插件里的 JevClient 走假 transport。记录每次调用以便断言。"""
    calls: list[dict] = []

    def fake(url, headers, body, timeout):
        calls.append(json.loads(body))
        if raises is not None:
            raise raises
        return payload

    monkeypatch.setattr(JevClient, "_http_post", staticmethod(fake))
    return calls


@pytest.fixture()
def make_plugin(tmp_path: Path, monkeypatch):
    def _make(**cfg_extra):
        monkeypatch.setenv("ASTRBOT_DATA_DIR", str(tmp_path))
        config = {
            "WIKI_URL": "https://my.feishu.cn/wiki/test",
            "ENABLED_GROUPS": ["*"],
            "ADMIN_USERS": ["admin1"],
            "SYNC_INTERVAL_HOURS": 0,
        }
        config.update(cfg_extra)
        p = FeishuQaPlugin(context=None, config=config)
        from astrbot_plugin_feishu_qa.corpus.builder import build_manifest
        from astrbot_plugin_feishu_qa.corpus.parser import parse_xml
        from astrbot_plugin_feishu_qa.storage.snapshot import SnapshotStore

        xml = (FIXTURES / "qa_r8268.xml").read_text()
        parsed = parse_xml(xml, source_revision=8268)
        SnapshotStore(p.data_root).commit(
            build_manifest(parsed, revision_id=8268, document_id="doc")
        )
        assert p._load_corpus() is True
        return p

    return _make


def _at_event(query: str = MEDIUM_QUERY) -> AstrMessageEvent:
    """@ 唤醒已被 AstrBot 内核 WakingCheckStage 剥掉(见 v0.8.8 修复记录),
    handler 收到的是裸问题;`_strip_wake` 只做 strip。"""
    ev = AstrMessageEvent()
    ev.message_str = query
    ev.is_at_or_wake_command = True
    return ev


def _sent_text(ev) -> str:
    """取出已发送链里的全部文本。

    链有两种形态:合并转发 Node(Node.content 里的 Plain),或 qq_official 的
    raw ("plain", text) 元组。这里都收,避免测试绑死其中一种。
    """
    out: list[str] = []
    for item in ev.sent[0].chain:
        if isinstance(item, tuple):
            out.append(str(item[1]))
        elif hasattr(item, "content"):
            out.extend(
                str(getattr(c, "text", "")) for c in item.content
                if getattr(c, "text", None)
            )
    return "\n".join(out)


def _run(coro):
    import asyncio

    async def consume():
        if hasattr(coro, "__anext__"):
            async for _ in coro:
                pass
        else:
            await coro

    asyncio.run(consume())


def _decisions(plugin) -> list[dict]:
    return plugin._decision_log.read_all()


# ── C 位点 ──


class TestHookC:
    def test_jev_says_sufficient_bot_answers(self, make_plugin, monkeypatch) -> None:
        calls = _patch_jev(monkeypatch, _answer([0.10, 0.80, 0.10]))
        p = make_plugin(JEV_ENABLED=True, JEV_API_KEY="k")
        ev = _at_event()
        _run(p.on_group_message(ev))
        assert len(ev.sent) == 1, "Jev 判定够格就必须自己答"
        assert ev.stopped is True, "自己答了必须阻断主 Agent"
        assert "可能与你的问题相关" in _sent_text(ev)
        assert len(calls) == 1, "应只发一次 Jev 调用(一次并行求值全部问题)"

    def test_jev_says_synthesis_hands_off(self, make_plugin, monkeypatch) -> None:
        _patch_jev(monkeypatch, _answer([0.34, 0.33, 0.33]))
        p = make_plugin(JEV_ENABLED=True, JEV_API_KEY="k")
        ev = _at_event()
        _run(p.on_group_message(ev))
        assert ev.sent == [], "需要综合时不得自己答"
        assert ev.stopped is not True, "交回主 Agent 就不能 stop_event"

    def test_not_answerable_hands_off(self, make_plugin, monkeypatch) -> None:
        """材料答不上(绝对 Noul 过不了)→ 交回 Agent。"""
        _patch_jev(monkeypatch, _answer([0.90, 0.05, 0.05], answerable=0.10))
        p = make_plugin(JEV_ENABLED=True, JEV_API_KEY="k")
        ev = _at_event()
        _run(p.on_group_message(ev))
        assert ev.sent == []
        assert ev.stopped is not True

    def test_one_call_carries_synthesis_and_all_candidates(
        self, make_plugin, monkeypatch
    ) -> None:
        calls = _patch_jev(monkeypatch, _answer([0.60, 0.20, 0.20]))
        p = make_plugin(JEV_ENABLED=True, JEV_API_KEY="k")
        _run(p.on_group_message(_at_event()))
        body = calls[0]
        assert RANK_QID in body["questions"]
        assert ANY_QID in body["questions"]
        assert body["state"]["question"] == MEDIUM_QUERY
        assert len(body["state"]["candidates"]) >= 1

    def test_jev_timeout_falls_back_to_local_switch(
        self, make_plugin, monkeypatch
    ) -> None:
        """Jev 挂掉时,行为必须与 v0.9.0 一致:由 TENTATIVE_ANSWER_ENABLED 决定。"""
        _patch_jev(monkeypatch, raises=TimeoutError())
        p = make_plugin(
            JEV_ENABLED=True, JEV_API_KEY="k", TENTATIVE_ANSWER_ENABLED=True
        )
        ev = _at_event()
        _run(p.on_group_message(ev))
        assert len(ev.sent) == 1, "Jev 挂了仍应由本地模糊档接管"
        assert ev.stopped is True

    def test_jev_timeout_and_switch_off_behaves_like_v090(
        self, make_plugin, monkeypatch
    ) -> None:
        _patch_jev(monkeypatch, raises=TimeoutError())
        p = make_plugin(JEV_ENABLED=True, JEV_API_KEY="k", TENTATIVE_ANSWER_ENABLED=False)
        ev = _at_event()
        _run(p.on_group_message(ev))
        assert ev.sent == []
        assert ev.stopped is not True

    def test_jev_disabled_makes_no_call(self, make_plugin, monkeypatch) -> None:
        calls = _patch_jev(monkeypatch, _answer([0.90, 0.06, 0.04]))
        p = make_plugin(JEV_ENABLED=False, JEV_API_KEY="k")
        ev = _at_event()
        _run(p.on_group_message(ev))
        assert calls == [], "未启用时不得发起任何 Jev 调用"
        assert ev.sent == []

    def test_enabled_without_key_is_treated_as_disabled(
        self, make_plugin, monkeypatch
    ) -> None:
        calls = _patch_jev(monkeypatch, _answer([0.90, 0.06, 0.04]))
        p = make_plugin(JEV_ENABLED=True, JEV_API_KEY="")
        assert p._jev is None
        _run(p.on_group_message(_at_event()))
        assert calls == []

    def test_high_query_never_reaches_jev(self, make_plugin, monkeypatch) -> None:
        """HIGH 走直答,不该为它花一次 Jev 调用。"""
        calls = _patch_jev(monkeypatch, _answer([0.85, 0.10, 0.05]))
        p = make_plugin(JEV_ENABLED=True, JEV_API_KEY="k")
        ev = _at_event(HIGH_QUERY)
        _run(p.on_group_message(ev))
        assert calls == [], "高置信直答不经过 Jev"
        assert ev.stopped is True

    def test_decision_log_records_jev_action_and_nouls(
        self, make_plugin, monkeypatch
    ) -> None:
        _patch_jev(monkeypatch, _answer([0.10, 0.80, 0.10]))
        p = make_plugin(JEV_ENABLED=True, JEV_API_KEY="k")
        _run(p.on_group_message(_at_event()))
        rows = [r for r in _decisions(p) if r["action"].startswith("jev_")]
        assert rows, "必须落 jev_* 记录"
        assert rows[-1]["extra"]["jev_action"] == "answer_self"
        assert ANY_QID in rows[-1]["extra"]["jev_nouls"]
        assert RANK_QID not in rows[-1]["extra"]["jev_nouls"], "Choice 不是 noul"
        assert "option_0" in rows[-1]["extra"]["jev_probs"]
        assert rows[-1]["extra"]["jev_picked"]

    def test_api_key_never_written_to_decision_log(
        self, make_plugin, monkeypatch
    ) -> None:
        _patch_jev(monkeypatch, _answer([0.85, 0.10, 0.05]))
        p = make_plugin(JEV_ENABLED=True, JEV_API_KEY="SUPER-SECRET-KEY")
        _run(p.on_group_message(_at_event()))
        raw = p._decision_log.path.read_text(encoding="utf-8")
        assert "SUPER-SECRET-KEY" not in raw


# ── D 位点 ──


def _kb_result(codes: list[str]):
    text = "".join(
        f"【全家桶FAQ > 章节】标题\n正文\n[ref:{c}]\n相关度: 1.00\n\n" for c in codes
    )
    return SimpleNamespace(content=[SimpleNamespace(text=text)])


def _tool(name="astr_kb_search"):
    return SimpleNamespace(name=name)


class TestHookD:
    def _two_entries(self, p):
        return [e for e in p._entries if e.source_locator][:2]

    def test_jev_selects_only_high_noul_entries(self, make_plugin, monkeypatch) -> None:
        # 候选0 本地分更高但 noul 更低 → Jev 应当否掉它
        _patch_jev(monkeypatch, _answer([0.10, 0.90]))
        p = make_plugin(JEV_ENABLED=True, JEV_API_KEY="k")
        entries = self._two_entries(p)
        ev = _at_event()
        _run(
            p.auto_send_faq_links(
                ev, _tool(), None, _kb_result([entries[0].id[3:8], entries[1].id[3:8]])
            )
        )
        text = _sent_text(ev)
        assert len(ev.sent) == 1
        assert entries[0].source_locator not in text, "noul 不够的候选不应附"
        assert entries[1].source_locator in text

    def test_jev_suppresses_all_links(self, make_plugin, monkeypatch) -> None:
        _patch_jev(monkeypatch, _answer([0.20, 0.10, 0.05, 0.65]))
        p = make_plugin(JEV_ENABLED=True, JEV_API_KEY="k")
        entries = self._two_entries(p)
        ev = _at_event()
        _run(
            p.auto_send_faq_links(
                ev, _tool(), None, _kb_result([entries[0].id[3:8], entries[1].id[3:8]])
            )
        )
        assert ev.sent == [], "Jev 认为都不够格时不得发任何链接"
        rows = [r for r in _decisions(p) if r["action"] == "jev_links_suppressed"]
        assert rows, "抑制必须留痕"

    def test_jev_unusable_falls_back_to_local_gate(
        self, make_plugin, monkeypatch
    ) -> None:
        _patch_jev(monkeypatch, raises=OSError("down"))
        p = make_plugin(JEV_ENABLED=True, JEV_API_KEY="k")
        entries = self._two_entries(p)
        ev = _at_event(GATE_PASSING_QUERY)
        _run(
            p.auto_send_faq_links(
                ev, _tool(), None, _kb_result([entries[0].id[3:8], entries[1].id[3:8]])
            )
        )
        # 回落 linkable_entries:该问题与条目同域,本地闸门应放行
        assert len(ev.sent) == 1, "Jev 挂了必须回落到本地闸门,不能整个不附链"

    def test_jev_disabled_uses_local_gate_only(self, make_plugin, monkeypatch) -> None:
        calls = _patch_jev(monkeypatch, _answer([0.10, 0.10, 0.80]))
        p = make_plugin(JEV_ENABLED=False, JEV_API_KEY="k")
        entries = self._two_entries(p)
        ev = _at_event(GATE_PASSING_QUERY)
        _run(
            p.auto_send_faq_links(
                ev, _tool(), None, _kb_result([entries[0].id[3:8], entries[1].id[3:8]])
            )
        )
        assert calls == []
        assert len(ev.sent) == 1, "未启用 Jev 时行为与 v0.9.0 一致(本地闸门放行)"

    def test_d_uses_user_original_question_not_retrieval_query(
        self, make_plugin, monkeypatch
    ) -> None:
        """9·15 事故的根因守卫:state 里必须是用户原问题。"""
        calls = _patch_jev(monkeypatch, _answer([0.05, 0.90, 0.05]))
        p = make_plugin(JEV_ENABLED=True, JEV_API_KEY="k")
        entries = self._two_entries(p)
        ev = _at_event()
        _run(
            p.auto_send_faq_links(
                ev, _tool(), None, _kb_result([entries[0].id[3:8], entries[1].id[3:8]])
            )
        )
        assert calls, "D 位点应发起调用"
        assert calls[0]["state"]["question"] == MEDIUM_QUERY
        assert "@小脑呆" not in calls[0]["state"]["question"], "必须剥掉 @ 唤醒"

    def test_d_candidate_text_is_truncated(self, make_plugin, monkeypatch) -> None:
        """长 state 会稀释准确率,候选正文必须截断。"""
        calls = _patch_jev(monkeypatch, _answer([0.05, 0.90, 0.05]))
        p = make_plugin(JEV_ENABLED=True, JEV_API_KEY="k")
        entries = self._two_entries(p)
        ev = _at_event()
        _run(
            p.auto_send_faq_links(
                ev, _tool(), None, _kb_result([entries[0].id[3:8], entries[1].id[3:8]])
            )
        )
        for c in calls[0]["state"]["candidates"]:
            assert len(c["text"]) <= 600


# ── /jev_probe 诊断命令 ──


def _probe_result(plugin, event):
    """收集 async generator 的产出(模块级 _run 会丢弃 item,这里要文本)。"""
    import asyncio

    async def go():
        return [item async for item in plugin.jev_probe(event)]

    return asyncio.run(go())


class TestJevProbe:
    def _admin_event(self, msg: str = "/jev_probe midi设备怎么连接"):
        ev = AstrMessageEvent(message_str=msg, sender_id="admin1", group_id="g1")
        ev._is_admin_flag = True
        ev.is_at_or_wake_command = True
        return ev

    def test_non_admin_rejected(self, make_plugin, monkeypatch) -> None:
        _patch_jev(monkeypatch, _answer([0.8, 0.1, 0.1]))
        p = make_plugin(JEV_ENABLED=True, JEV_API_KEY="k")
        ev = AstrMessageEvent(message_str="/jev_probe x", sender_id="stranger", group_id="g1")
        ev._is_admin_flag = False
        assert "仅管理员" in _probe_result(p, ev)[0][1]

    def test_disabled_reports_not_enabled(self, make_plugin, monkeypatch) -> None:
        p = make_plugin(JEV_ENABLED=False, JEV_API_KEY="")
        assert "Jev 未启用" in _probe_result(p, self._admin_event())[0][1]

    def test_empty_question_shows_usage(self, make_plugin, monkeypatch) -> None:
        _patch_jev(monkeypatch, _answer([0.8, 0.1, 0.1]))
        p = make_plugin(JEV_ENABLED=True, JEV_API_KEY="k")
        assert "用法" in _probe_result(p, self._admin_event("/jev_probe "))[0][1]

    def test_reports_probabilities_and_decision(self, make_plugin, monkeypatch) -> None:
        _patch_jev(monkeypatch, _answer([0.10, 0.80, 0.10]))
        p = make_plugin(JEV_ENABLED=True, JEV_API_KEY="k")
        text = _probe_result(p, self._admin_event())[0][1]
        assert "answerable" in text
        assert "P=0.800" in text
        assert "←选中" in text
        assert "answer_self" in text

    def test_reports_fallback_when_jev_unavailable(self, make_plugin, monkeypatch) -> None:
        _patch_jev(monkeypatch, raises=TimeoutError())
        p = make_plugin(JEV_ENABLED=True, JEV_API_KEY="k")
        text = _probe_result(p, self._admin_event())[0][1]
        assert "不可用" in text and "回落" in text

    def test_probe_bypasses_group_whitelist(self, make_plugin, monkeypatch) -> None:
        """私聊(C2C)下 get_group_id() 为 None,走 router 会被判 denied。
        诊断命令必须仍能出结果,否则管理员在自己的私聊里看不到。"""
        _patch_jev(monkeypatch, _answer([0.10, 0.80, 0.10]))
        p = make_plugin(JEV_ENABLED=True, JEV_API_KEY="k", ENABLED_GROUPS=["grp_x"])
        ev = self._admin_event()
        ev._group_id = None          # 私聊
        ev.unified_msg_origin = "inst:FriendMessage:u1"
        text = _probe_result(p, ev)[0][1]
        assert "denied" not in text
        assert "answerable" in text

    def test_decision_logged_at_info(self, make_plugin, monkeypatch, caplog) -> None:
        """决策必须同时进日志,否则 plugin_data 里的记录在面板上回读不了。"""
        import logging as _logging

        _patch_jev(monkeypatch, _answer([0.10, 0.80, 0.10]))
        p = make_plugin(JEV_ENABLED=True, JEV_API_KEY="k")
        with caplog.at_level(_logging.INFO):
            _run(p.on_group_message(_at_event()))
        assert any("Jev 决策" in r.getMessage() for r in caplog.records), \
            "Jev 决策未进日志"

    def test_probe_never_sends_to_group(self, make_plugin, monkeypatch) -> None:
        """诊断命令只回显给提问者,不得向群里发内容、不得阻断。"""
        _patch_jev(monkeypatch, _answer([0.10, 0.80, 0.10]))
        p = make_plugin(JEV_ENABLED=True, JEV_API_KEY="k")
        ev = self._admin_event()
        _probe_result(p, ev)
        assert ev.sent == [], "诊断命令不得投递消息"
        assert ev.stopped is False, "诊断命令不得阻断流水线"


# ── 启动自检:决策链金丝雀 ──


class TestSelfTestChain:
    def test_probe_query_derived_from_corpus(self, make_plugin, monkeypatch) -> None:
        _patch_jev(monkeypatch, _answer([0.10, 0.80, 0.10]))
        p = make_plugin(JEV_ENABLED=True, JEV_API_KEY="k")
        q = p._jev_probe_query()
        assert len(q) >= 6, "探针问题应取自语料标题"
        assert not q.endswith(("？", "?")), "探针应去掉句末问号"

    def test_selftest_writes_jev_record(self, make_plugin, monkeypatch) -> None:
        _patch_jev(monkeypatch, _answer([0.10, 0.80, 0.10]))
        p = make_plugin(JEV_ENABLED=True, JEV_API_KEY="k")
        p._jev_selftest_chain(p._jev_probe_query())
        rows = [r for r in _decisions(p) if r["action"] == "jev_selftest"]
        assert rows, "决策链自检必须落一条 jev_selftest 记录"
        assert "jev_probs" in rows[-1]["extra"]
        assert "changed_vs_local" in rows[-1]["extra"]

    def test_selftest_detects_divergence_from_local(self, make_plugin, monkeypatch) -> None:
        """Jev 选中非本地 top-1 时,changed_vs_local 必须为 True。"""
        _patch_jev(monkeypatch, _answer([0.05, 0.05, 0.90]))
        p = make_plugin(JEV_ENABLED=True, JEV_API_KEY="k")
        p._jev_selftest_chain(p._jev_probe_query())
        row = [r for r in _decisions(p) if r["action"] == "jev_selftest"][-1]
        assert row["extra"]["jev_action"] == "answer_self"
        assert row["extra"]["changed_vs_local"] is True

    def test_selftest_survives_jev_outage(self, make_plugin, monkeypatch) -> None:
        _patch_jev(monkeypatch, raises=TimeoutError())
        p = make_plugin(JEV_ENABLED=True, JEV_API_KEY="k")
        p._jev_selftest_chain(p._jev_probe_query())  # 不得抛
        assert not [r for r in _decisions(p) if r["action"] == "jev_selftest"]

    def test_selftest_bypasses_group_whitelist(self, make_plugin, monkeypatch) -> None:
        """回归:自检曾走 router,而 router 带群白名单门禁,group_id 不在白名单
        时被判 denied 拿不到候选,金丝雀被静默跳过。现自检直接用检索器。"""
        _patch_jev(monkeypatch, _answer([0.05, 0.05, 0.90]))
        p = make_plugin(JEV_ENABLED=True, JEV_API_KEY="k", ENABLED_GROUPS=["grp_x"])
        p._jev_selftest_chain(p._jev_probe_query())
        rows = [r for r in _decisions(p) if r["action"] == "jev_selftest"]
        assert rows, "白名单受限时自检也必须出记录,否则金丝雀静默失效"
        assert rows[-1]["extra"]["changed_vs_local"] is True

    def test_selftest_noop_when_disabled(self, make_plugin, monkeypatch) -> None:
        _patch_jev(monkeypatch, _answer([0.9, 0.05, 0.05]))
        p = make_plugin(JEV_ENABLED=False, JEV_API_KEY="")
        p._jev_selfcheck()  # 不得抛,也不得调 Jev
        assert _decisions(p) == []
