"""/learn 候选提取测试(spec §35/§38/§59)。"""

from __future__ import annotations

from pathlib import Path

import pytest

from astrbot_plugin_feishu_qa.corpus.parser import parse_xml
from astrbot_plugin_feishu_qa.learn.candidate import (
    MAX_HISTORY_CHARS,
    build_learn_prompt,
    candidate_to_pending_record,
    extract_transcript,
    find_duplicate,
    format_candidate_display,
    is_chat_record_transcript,
    parse_candidate,
)
from astrbot_plugin_feishu_qa.retrieval.scorer import Retriever

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def retriever() -> Retriever:
    xml = (FIXTURES / "qa_r8268.xml").read_text()
    return Retriever(parse_xml(xml, source_revision=8268).entries)


GOOD_CANDIDATE = """{"is_candidate": true,
  "question": "安装完打开提示缺少 msvcp140.dll 怎么办",
  "answer": "安装 VC++ 运行库后重启电脑。",
  "symptom_tags": ["缺少DLL"],
  "category": "报错闪退相关",
  "evidence": ["群友: 打开就报 dll 错误"],
  "confidence": 0.9}"""


class TestParseCandidate:
    def test_valid_candidate(self) -> None:
        obj = parse_candidate(GOOD_CANDIDATE)
        assert obj is not None and obj["question"].startswith("安装")

    def test_wrapped_in_prose_still_parses(self) -> None:
        raw = f"分析结果如下:\n{GOOD_CANDIDATE}\n以上。"
        assert parse_candidate(raw) is not None

    def test_not_candidate_returns_none(self) -> None:
        assert parse_candidate('{"is_candidate": false}') is None

    def test_free_text_rejected(self) -> None:
        assert parse_candidate("我觉得可以加一条关于闪退的") is None

    def test_missing_answer_rejected(self) -> None:
        bad = '{"is_candidate": true, "question": "q"}'
        assert parse_candidate(bad) is None


class TestPromptBuilder:
    def test_history_size_capped(self) -> None:
        lines = ["x" * 500] * 20  # 共 10000 字符 > 上限
        prompt = build_learn_prompt(lines)
        assert len(prompt) < 6000

    def test_contains_output_schema(self) -> None:
        prompt = build_learn_prompt(["你好"])
        assert "is_candidate" in prompt and "evidence" in prompt


class TestDeduplication:
    def test_duplicate_detected(self, retriever: Retriever) -> None:
        dup = {
            "question": "cakewalk 没有自带的模板怎么办",
            "answer": "x",
            "symptom_tags": [],
        }
        hit = find_duplicate(dup, retriever)
        assert hit is not None and "没有自带模板" in hit.raw_title

    def test_new_topic_no_duplicate(self, retriever: Retriever) -> None:
        fresh = {"question": "flstudio 如何导出 mp3", "answer": "x", "symptom_tags": []}
        assert find_duplicate(fresh, retriever) is None


class TestDisplayAndRecord:
    def test_display_shows_confirm_instruction(self) -> None:
        obj = parse_candidate(GOOD_CANDIDATE)
        text = format_candidate_display(obj)
        assert "/learn ok" in text

    def test_record_carries_metadata(self) -> None:
        obj = parse_candidate(GOOD_CANDIDATE)
        record = candidate_to_pending_record(
            obj, group_id="g1", approved_by="admin1"
        )
        assert record["learned_from_group"] == "g1"
        assert record["approved_by"] == "admin1"


class TestPromptBudget:
    """/learn 素材预算:超长单条不得被静默丢弃。

    旧实现"放不下就 break",遇到一条超过 MAX_HISTORY_CHARS 的素材(如整段
    合并转发转录)会整条丢弃 → prompt 素材区为空 → 模型无从判断,是静默失败。
    """

    def test_oversized_single_blob_is_kept_head_and_tail(self) -> None:
        blob = "HEAD" + ("中" * 9000) + "TAIL"
        prompt = build_learn_prompt([blob])
        body = prompt.split("消息记录:")[1].split("输出格式:")[0]
        assert "HEAD" in body and "TAIL" in body, "超长素材被整条丢弃"
        assert "省略" in body, "截断处应显式标注,避免模型误当对话结束"

    def test_budget_respected(self) -> None:
        prompt = build_learn_prompt(["x" * 9000])
        body = prompt.split("消息记录:")[1].split("输出格式:")[0].strip()
        assert len(body) <= MAX_HISTORY_CHARS + 80

    def test_multiple_lines_still_capped(self) -> None:
        prompt = build_learn_prompt(["x" * 500] * 20)
        assert len(prompt) < 6000


class TestChatRecordTranscript:
    """QQ 官方机器人的"聊天记录"转录识别(message_type=102)。

    平台在服务端把合并转发展开成纯文本随 content 下发,故 plugin 拿
    events.message_str 即为完整转录,无需解析任何转发组件。
    """

    REAL_LOG = (
        "[At:qq_official] [群聊的聊天记录]\n"
        "=== 消息 1 ===\n[消息内容]  我去\n[发送者] ㅤㅤㅤ\n"
        "=== 消息 2 ===\n[消息内容] 61键1100左右\n[发送者] 小闻鸭鸭鸭鸭\n"
    )

    def test_detects_real_transcript(self) -> None:
        assert is_chat_record_transcript(self.REAL_LOG) is True

    def test_extract_strips_at_prefix(self) -> None:
        body = extract_transcript(self.REAL_LOG)
        assert body.startswith("[群聊的聊天记录]")
        assert "[At:qq_official]" not in body
        assert "=== 消息 1 ===" in body

    def test_plain_chat_not_treated_as_transcript(self) -> None:
        assert is_chat_record_transcript("cakewalk 的混音台怎么打开") is False
        assert is_chat_record_transcript("") is False

    def test_merged_forward_without_header_still_detected(self) -> None:
        """有些客户端不带 [群聊的聊天记录] 头,只给 === 消息 N === 分节。"""
        assert is_chat_record_transcript("=== 消息 1 ===\n[消息内容] hi") is True
