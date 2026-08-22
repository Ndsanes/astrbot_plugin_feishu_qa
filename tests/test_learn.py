"""/learn 候选提取测试(spec §35/§38/§59)。"""

from __future__ import annotations

from pathlib import Path

import pytest

from astrbot_plugin_feishu_qa.corpus.parser import parse_xml
from astrbot_plugin_feishu_qa.learn.candidate import (
    build_learn_prompt,
    candidate_to_pending_record,
    find_duplicate,
    format_candidate_display,
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
