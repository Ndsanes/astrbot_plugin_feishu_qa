"""/learn 写回(learn/writeback.py)单元测试与"diff 仅新增"离线验收。

离线验收模拟真实闭环的关键不变量:
在旧语料 XML 末尾追加一个写回 block 后重新解析,
必须满足 old_ids ⊂ new_ids 且 new_ids - old_ids == 预期新增条目。
"""

from __future__ import annotations

from pathlib import Path

from astrbot_plugin_feishu_qa.corpus.model import (
    derive_entry_id,
    extract_symptom_tags,
)
from astrbot_plugin_feishu_qa.corpus.parser import parse_xml
from astrbot_plugin_feishu_qa.learn.writeback import (
    build_entry_markdown,
    derive_record_key,
    duplicate_guard_result,
    extract_h3_titles,
    is_duplicate_title,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures"

RECORD = {
    "question": "导出视频时提示磁盘空间不足怎么办",
    "answer": "清理临时目录或更换输出路径到剩余空间充足的磁盘。",
    "symptom_tags": ["导出", "磁盘"],
}


def test_build_entry_markdown_含标签标题与正文():
    md = build_entry_markdown(RECORD)
    assert md.startswith("### 【导出】【磁盘】导出视频时提示磁盘空间不足怎么办\n")
    assert "清理临时目录" in md
    assert md.endswith("\n")
    assert md.count("### ") == 1  # 单一 h3 标题,一次完整块


def test_build_entry_markdown_无标签与空字段():
    md = build_entry_markdown({"question": "裸问题", "answer": "", "symptom_tags": []})
    assert md == "### 裸问题\n\n\n"


def test_extract_h3_titles_只取三级标题且归一化():
    md = "\n".join(
        [
            "# 主章节",
            "## 二级分类",
            "### 1、安装后闪退",
            "### 【崩溃】启动即退出",
            "正文段落不是标题",
        ]
    )
    titles = extract_h3_titles(md)
    assert titles == {"安装后闪退", "启动即退出"}


def test_is_duplicate_title_归一化后命中():
    titles = extract_h3_titles("### 1、导出视频时提示磁盘空间不足怎么办\n")
    assert is_duplicate_title(RECORD, titles)


def test_derive_record_key_稳定且区分大小写文本():
    assert derive_record_key(RECORD) == derive_record_key(dict(RECORD))
    other = {**RECORD, "question": "另一个完全不同的问题"}
    assert derive_record_key(RECORD) != derive_record_key(other)


def test_duplicate_guard_双保险():
    titles = set()
    assert duplicate_guard_result(RECORD, titles) == "ok"
    # 标题命中(归一化后精确相等)→ already_exists
    assert (
        duplicate_guard_result(
            RECORD, extract_h3_titles("### 【导出】导出视频时提示磁盘空间不足怎么办\n")
        )
        == "already_exists"
    )
    # 序号前缀被归一化掉后仍命中
    assert (
        duplicate_guard_result(
            RECORD, extract_h3_titles("### 1、导出视频时提示磁盘空间不足怎么办\n")
        )
        == "already_exists"
    )
    # 记录键命中 → already_exists
    key = derive_record_key(RECORD)
    assert (
        duplicate_guard_result(RECORD, titles, already_synced_keys={key})
        == "already_exists"
    )


def test_writeback_block_经_xml_解析后_ID_与内容派生一致():
    """写回块进入文档后(h3 + 正文)必须被 parser 还原为同一条目。"""
    md = build_entry_markdown(RECORD)
    # 模拟 lark-cli 把追加的 markdown 转成文档块后的 DocxXML 形态
    title_line = md.splitlines()[0].removeprefix("### ")
    answer = md.split("\n\n", 1)[1].strip()
    xml = f"<h3>{title_line}</h3><p>{answer}</p>"
    result = parse_xml(xml)
    assert len(result.entries) == 1
    entry = result.entries[0]
    tags, stripped = extract_symptom_tags(title_line)
    expected_id = derive_entry_id([], stripped, entry.source_locator)
    assert entry.id == expected_id
    assert entry.symptom_tags == tags
    assert entry.body == answer


def test_append_to_fixture_diff_仅新增(tmp_path: Path):
    """机器验收:旧语料 + 写回块 → old_ids ⊂ new_ids 且差集恰为新条目。"""
    old_xml = (FIXTURE_DIR / "qa_r8268.xml").read_text()
    old_result = parse_xml(old_xml)
    old_ids = {e.id for e in old_result.entries}

    md = build_entry_markdown(RECORD)
    title_line = md.splitlines()[0].removeprefix("### ")
    answer = md.split("\n\n", 1)[1].strip()
    appended_block = f"<h3>{title_line}</h3><p>{answer}</p>"

    new_result = parse_xml(old_xml + appended_block)
    new_ids = {e.id for e in new_result.entries}
    added = new_ids - old_ids
    assert old_ids <= new_ids, "既有条目 ID 必须全部保持稳定"
    assert len(added) == 1, f"diff 必须仅含预期新增条目,实际:{added}"
    new_entry = next(e for e in new_result.entries if e.id in added)
    _, stripped = extract_symptom_tags(title_line)
    assert new_entry.title == stripped


def test_写回后的新条目可被检索命中():
    """Phase 3 验收:写回产生的新 QA 加入语料后必须能被确定性检索找到。"""
    from astrbot_plugin_feishu_qa.corpus.parser import parse_xml
    from astrbot_plugin_feishu_qa.retrieval.scorer import Retriever

    old_xml = (FIXTURE_DIR / "qa_r8268.xml").read_text()
    md = build_entry_markdown(RECORD)
    title_line = md.splitlines()[0].removeprefix("### ")
    answer = md.split("\n\n", 1)[1].strip()
    parsed = parse_xml(old_xml + f"<h3>{title_line}</h3><p>{answer}</p>")
    retriever = Retriever(parsed.entries)
    hits = retriever.search("导出视频 磁盘空间不足")
    assert hits, "新条目应至少出现在候选里"
    top = hits[0]
    top_title = getattr(top, "title", "") or ""
    assert "磁盘空间不足" in top_title, f"top 命中不是新条目: {top!r}"


