"""kb_pipeline 单元与集成测试(规格 §52-54)。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kb_pipeline.chunker import chunk_sections, token_len  # noqa: E402
from kb_pipeline.cleaner import detect_repeated_lines  # noqa: E402
from kb_pipeline.context_llm import ContextCache, enrich_chunks  # noqa: E402
from kb_pipeline.structure import Section  # noqa: E402

# ── 测试用章节树工厂 ──

def _mk_tree() -> Section:
    root = Section(level=0, title="Manual", page=0)
    ch = Section(level=1, title="01 Chapter A", page=1)
    ch.parent = root
    root.children.append(ch)

    def _leaf(title: str, page: int, paras: list[str]) -> Section:

        node = Section(level=3 if "detail" in title else 2, title=title, page=page)
        node.parent = ch
        for p in paras:
            node.blocks.append(
                __import__("kb_pipeline.structure", fromlist=["Block"]).Block(
                    text=p, page=page
                )
            )
        return node

    ch.children.append(_leaf("Overview detail", 1, ["Intro text here."]))
    ch.children.append(
        _leaf("Procedure", 2, ["1. Do this.\n\n2. Do that.\n\n3. Finish.", "4. Verify."])
    )
    root.children.append(
        Section(level=1, title="02 Chapter B", page=9, parent=root)
    )
    return root


# ── cleaner ──

class TestCleaner:
    def test_repeated_header_detected(self):
        from kb_pipeline.extract import PageLines

        pages = []
        for i in range(20):
            lines = [
                __import__("kb_pipeline.extract", fromlist=["Line"]).Line(
                    page=i, text="Cakewalk Sonar Reference Guide", size=8.0, y=20
                ),
                __import__("kb_pipeline.extract", fromlist=["Line"]).Line(
                    page=i, text=f"Body content page {i} with unique words.", size=9, y=300
                ),
            ]
            pages.append(PageLines(page=i, lines=lines))
        banned = detect_repeated_lines(pages)
        assert any("Reference Guide" in b for b in banned)


# ── structure / breadcrumb ──

class TestBreadcrumb:
    def test_breadcrumb_excludes_root(self):
        tree = _mk_tree()
        leaf = tree.children[0].children[0]
        bc = leaf.breadcrumb("Manual")
        assert bc == "01 Chapter A > Overview detail"

    def test_levels(self):
        tree = _mk_tree()
        levels = [s.level for s in tree.walk()]
        assert levels.count(1) == 2 and levels.count(3) == 1


# ── chunker ──

class TestChunking:
    def test_deterministic(self):
        tree = _mk_tree()
        c1 = chunk_sections(tree, "Manual")
        c2 = chunk_sections(tree, "Manual")
        assert [c.chunk_id for c in c1] == [c.chunk_id for c in c2]

    def test_no_cross_chapter(self):
        tree = _mk_tree()
        chunks = chunk_sections(tree, "Manual")
        for c in chunks:
            assert ("Chapter A" in c.breadcrumb) != ("Chapter B" in c.breadcrumb)

    def test_breadcrumb_in_content(self):
        tree = _mk_tree()
        for c in chunk_sections(tree, "Manual"):
            assert c.content.startswith("【") and "】" in c.content

    def test_procedure_list_atomic(self):
        tree = _mk_tree()
        chunks = chunk_sections(tree, "Manual", max_tokens=400)
        proc_chunks = [
            c for c in chunks if "1. Do this" in c.body
        ]
        assert proc_chunks, "procedure chunk missing"
        body = proc_chunks[0].body
        assert "1. Do this" in body and "3. Finish" in body  # 步骤不拆散

    def test_token_limits_respected(self):
        tree = _mk_tree()
        for c in chunk_sections(tree, "Manual", max_tokens=500):
            assert c.tokens <= 500 * 2  # 原子性优先,允许超限但记录


# ── context cache ──

class TestContextCache:
    def test_cache_hit_zero_calls(self, tmp_path):
        class FakeChunk:
            def __init__(self, cid, content):
                self.chunk_id, self.content = cid, content

        calls = {"n": 0}

        def llm(system, user):
            calls["n"] += 1
            return "\n".join(f"{i}|ctx {i}" for i in range(1, 4))

        cache = ContextCache(tmp_path / "c.json")
        chunks = [FakeChunk(f"c{i}", f"text {i}") for i in range(1, 4)]
        r1, s1 = enrich_chunks(chunks, "doc", cache, "m", llm)
        assert s1.llm_calls == 1 and all(r1.values())
        r2, s2 = enrich_chunks(chunks, "doc", cache, "m", llm)
        assert s2.llm_calls == 0 and s2.cache_hits == 3
        assert calls["n"] == 1

    def test_deterministic_keys(self, tmp_path):
        from kb_pipeline.context_llm import _chunk_key

        assert _chunk_key("d", "c", "m") == _chunk_key("d", "c", "m")


# ── token counting ──

def test_token_len_approximation():
    assert token_len("") >= 1
    assert token_len("a" * 400) == 100
