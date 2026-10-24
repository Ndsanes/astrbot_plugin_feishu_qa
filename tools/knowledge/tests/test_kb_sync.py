"""kb_sync 的检查/打包/知识库定位测试。

这些用例不碰网络、不碰 AstrBot:远端 HEAD、下载与 API 全部用假对象替换。
真正要锁住的是"什么时候该说没更新"——官方 PDF 被重新发布但内容没变时,
不能触发重建,更不能整库重导。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import kb_sync  # noqa: E402


def _write_manifest(output: Path, sha: str, *, bytes_: int = 100) -> None:
    kb_sync.save_json(
        kb_sync.manifest_path(output),
        {
            "source": {
                "file": kb_sync.PDF_FILENAME,
                "bytes": bytes_,
                "sha256": sha,
                "page_count": 1900,
                "version": "30.12.00",
            }
        },
    )


def _write_pdf(output: Path, payload: bytes = b"pdf") -> str:
    path = kb_sync.pdf_path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return kb_sync.file_sha256(path)


class TestFileHash:
    def test_matches_hashlib(self, tmp_path: Path) -> None:
        import hashlib

        path = tmp_path / "a.bin"
        path.write_bytes(b"x" * 5000)
        assert kb_sync.file_sha256(path) == hashlib.sha256(b"x" * 5000).hexdigest()


class TestCheck:
    def test_up_to_date_by_etag_skips_download(self, tmp_path, monkeypatch) -> None:
        sha = _write_pdf(tmp_path)
        _write_manifest(tmp_path, sha)
        kb_sync.record_remote_state(
            tmp_path, kb_sync.RemoteInfo("u", 3, "etag-1", "now"), sha
        )
        monkeypatch.setattr(
            kb_sync, "remote_info", lambda *a, **k: kb_sync.RemoteInfo("u", 3, "etag-1", "now")
        )

        def _boom(*a, **k):  # 不应被调用
            raise AssertionError("ETag 命中时不应下载")

        monkeypatch.setattr(kb_sync, "download", _boom)
        result = kb_sync.check(tmp_path)
        assert result.status == "up_to_date"
        assert "ETag" in result.reason

    def test_metadata_changed_but_content_same(self, tmp_path, monkeypatch) -> None:
        sha = _write_pdf(tmp_path)
        _write_manifest(tmp_path, sha)
        kb_sync.record_remote_state(
            tmp_path, kb_sync.RemoteInfo("u", 3, "etag-old", "t1"), sha
        )
        monkeypatch.setattr(
            kb_sync, "remote_info", lambda *a, **k: kb_sync.RemoteInfo("u", 3, "etag-new", "t2")
        )
        result = kb_sync.check(tmp_path)
        assert result.status == "up_to_date"
        assert "sha256 未变" in result.reason
        state = kb_sync.load_json(kb_sync.remote_state_path(tmp_path))
        assert state["etag"] == "etag-new"

    def test_new_content_is_update_available(self, tmp_path, monkeypatch) -> None:
        _write_pdf(tmp_path, b"old")
        _write_manifest(tmp_path, "0" * 64)
        kb_sync.record_remote_state(
            tmp_path, kb_sync.RemoteInfo("u", 3, "etag-old", "t1"), "0" * 64
        )
        monkeypatch.setattr(
            kb_sync, "remote_info", lambda *a, **k: kb_sync.RemoteInfo("u", 4, "etag-new", "t2")
        )

        def _download(url, dest, **k):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"new!")
            return kb_sync.file_sha256(dest), 4

        monkeypatch.setattr(kb_sync, "download", _download)
        result = kb_sync.check(tmp_path)
        assert result.status == "update_available"
        assert result.sha256 == kb_sync.file_sha256(kb_sync.pdf_path(tmp_path))

    def test_missing_baseline_is_unknown(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(
            kb_sync, "remote_info", lambda *a, **k: kb_sync.RemoteInfo("u", 3, "e", "t")
        )
        result = kb_sync.check(tmp_path)
        assert result.status == "unknown"

    def test_head_failure_is_error(self, tmp_path, monkeypatch) -> None:
        def _fail(*a, **k):
            raise OSError("no network")

        monkeypatch.setattr(kb_sync, "remote_info", _fail)
        result = kb_sync.check(tmp_path)
        assert result.status == "error"


class TestDocuments:
    def test_chapter_grouping_uses_breadcrumb(self) -> None:
        chunks = [
            "【Doc > Chapter A > Sec 1】\n\nbody a1",
            "【Doc > Chapter A > Sec 2】\n\nbody a2",
            "【Doc > Chapter B > Sec 1】\n\nbody b1",
        ]
        docs = kb_sync.build_documents(
            chunks, version="30.12.00", sha256="abcdef1234567890"
        )
        assert docs[0]["file_name"].startswith(
            "Cakewalk Sonar Reference Guide v30.12.00 - Chapter A ("
        )
        assert docs[1]["file_name"].startswith(
            "Cakewalk Sonar Reference Guide v30.12.00 - Chapter B ("
        )
        assert [len(doc["chunks"]) for doc in docs] == [2, 1]
        assert docs[0]["chunks"] == chunks[:2]

    def test_chapter_hash_changes_only_for_changed_chapter(self) -> None:
        base = [
            "【Doc > A】\n\nx",
            "【Doc > B】\n\ny",
        ]
        changed = [
            "【Doc > A】\n\nx",
            "【Doc > B】\n\ny CHANGED",
        ]
        first = kb_sync.build_documents(base, version="1", sha256="0" * 64)
        second = kb_sync.build_documents(changed, version="1", sha256="1" * 64)
        assert first[0]["file_name"] == second[0]["file_name"]
        assert first[1]["file_name"] != second[1]["file_name"]

    def test_missing_breadcrumb_goes_to_misc(self) -> None:
        docs = kb_sync.build_documents(
            ["plain text"], version="1", sha256="0" * 64
        )
        assert len(docs) == 1
        assert " - misc (" in docs[0]["file_name"]

    def test_unsafe_chapter_chars_are_sanitized(self) -> None:
        docs = kb_sync.build_documents(
            ["【Doc > a/b:c*d?e】\n\nx"], version="1", sha256="0" * 64
        )
        assert "/" not in docs[0]["file_name"].split(" - ", 1)[1]
        assert ":" not in docs[0]["file_name"]

    def test_fixed_mode_split_keeps_every_chunk_once(self) -> None:
        chunks = [f"c{i}" for i in range(10)]
        docs = kb_sync.build_documents(
            chunks, version="1.0.0", sha256="x" * 64, split=3, group_by="fixed"
        )
        assert len(docs) == 3
        assert [len(d["chunks"]) for d in docs] == [4, 4, 2]
        joined = [chunk for doc in docs for chunk in doc["chunks"]]
        assert joined == chunks
        assert docs[0]["file_name"].endswith("part1of3.md")

    def test_fixed_mode_split_larger_than_chunks_is_single(self) -> None:
        docs = kb_sync.build_documents(
            ["a"], version="v", sha256="y" * 64, split=9, group_by="fixed"
        )
        assert len(docs) == 1


class TestPlanSync:
    PREFIX = "Cakewalk Sonar Reference Guide"

    def _doc(self, name: str, doc_id: str = "1") -> dict:
        return {"file_name": name, "chunks": ["x"]}

    def _existing(self, name: str, doc_id: str) -> dict:
        return {"doc_id": doc_id, "doc_name": name, "chunk_count": 1}

    def test_unchanged_plan_is_empty(self) -> None:
        docs = [
            self._doc(f"{self.PREFIX} - A (11111111).md"),
            self._doc(f"{self.PREFIX} - B (22222222).md"),
        ]
        existing = [self._existing(doc["file_name"], str(i)) for i, doc in enumerate(docs)]
        to_import, to_delete = kb_sync.plan_sync(docs, existing, self.PREFIX)
        assert to_import == [] and to_delete == []

    def test_changed_chapter_imports_one_and_deletes_one(self) -> None:
        docs = [
            self._doc(f"{self.PREFIX} - A (11111111).md"),
            self._doc(f"{self.PREFIX} - B (99999999).md"),
        ]
        existing = [
            self._existing(f"{self.PREFIX} - A (11111111).md", "1"),
            self._existing(f"{self.PREFIX} - B (22222222).md", "2"),
        ]
        to_import, to_delete = kb_sync.plan_sync(docs, existing, self.PREFIX)
        assert [d["file_name"] for d in to_import] == [docs[1]["file_name"]]
        assert [d["doc_id"] for d in to_delete] == ["2"]

    def test_removed_chapter_only_deletes(self) -> None:
        docs = [self._doc(f"{self.PREFIX} - A (11111111).md")]
        existing = [
            self._existing(f"{self.PREFIX} - A (11111111).md", "1"),
            self._existing(f"{self.PREFIX} - Old (33333333).md", "3"),
        ]
        to_import, to_delete = kb_sync.plan_sync(docs, existing, self.PREFIX)
        assert to_import == []
        assert [d["doc_id"] for d in to_delete] == ["3"]

    def test_unrelated_documents_are_never_deleted(self) -> None:
        docs = [self._doc(f"{self.PREFIX} - A (11111111).md")]
        existing = [
            self._existing(f"{self.PREFIX} - A (11111111).md", "1"),
            self._existing("有福同享全家桶FAQ.md", "9"),
        ]
        _, to_delete = kb_sync.plan_sync(docs, existing, self.PREFIX)
        assert to_delete == []


class TestMatchingDocs:
    DOCS = [
        {"doc_id": "1", "doc_name": "Cakewalk Sonar Reference Guide v30.12.00 (abc).md"},
        {"doc_id": "2", "doc_name": "cakewalk sonar reference guide part1of9.md"},
        {"doc_id": "3", "doc_name": "有福同享全家桶FAQ.md"},
    ]

    def test_prefix_matches_case_insensitively(self) -> None:
        got = kb_sync.matching_docs(self.DOCS, "Cakewalk Sonar Reference Guide")
        assert [doc["doc_id"] for doc in got] == ["1", "2"]

    def test_empty_prefix_matches_nothing(self) -> None:
        assert kb_sync.matching_docs(self.DOCS, "") == []


class _FakeKB:
    def __init__(self, items):
        self.items = items

    def get(self, path, **kwargs):
        assert path.startswith("/api/v1/knowledge-bases?")
        return {"data": {"items": self.items, "total": len(self.items)}}


class TestResolveKB:
    def test_exact_name_wins_over_similar(self) -> None:
        api = _FakeKB(
            [
                {"kb_id": "a", "kb_name": "Cakewalk sonar 旧"},
                {"kb_id": "b", "kb_name": "Cakewalk sonar"},
            ]
        )
        assert kb_sync.resolve_kb(api, kb_name="Cakewalk sonar") == "b"

    def test_fuzzy_contains(self) -> None:
        api = _FakeKB([{"kb_id": "a", "kb_name": "📒 Cakewalk sonar"}])
        assert kb_sync.resolve_kb(api, kb_name="cakewalk SONAR") == "a"

    def test_missing_name_raises_with_hint(self) -> None:
        api = _FakeKB([{"kb_id": "a", "kb_name": "别的库"}])
        try:
            kb_sync.resolve_kb(api, kb_name="Cakewalk sonar")
        except LookupError as exc:
            assert "别的库" in str(exc)
        else:
            raise AssertionError("应当抛 LookupError")


class _TaskAPI:
    def __init__(self):
        self.calls = 0

    def get(self, path, **kwargs):
        self.calls += 1
        if self.calls < 3:
            return {"data": {"status": "processing"}}
        return {"data": {"status": "completed", "result": {"uploaded": []}}}


class TestWaitTask:
    def test_polls_until_completed(self, monkeypatch) -> None:
        monkeypatch.setattr(kb_sync.time, "sleep", lambda _s: None)
        data = kb_sync.wait_task(_TaskAPI(), "t1", timeout=10)
        assert data["status"] == "completed"

    def test_failure_raises(self) -> None:
        class _Fail:
            def get(self, path, **kwargs):
                return {"data": {"status": "failed", "error": "boom"}}

        try:
            kb_sync.wait_task(_Fail(), "t1", timeout=10)
        except RuntimeError as exc:
            assert "boom" in str(exc)
        else:
            raise AssertionError("应当抛 RuntimeError")


class TestPayload:
    def test_written_payload_has_documents_and_batch(self, tmp_path: Path) -> None:
        docs = kb_sync.build_documents(["a"], version="1", sha256="z" * 64)
        path = kb_sync.write_payload(tmp_path, docs)
        payload = json.loads(path.read_text())
        assert payload["documents"][0]["chunks"] == ["a"]
        assert payload["batch_size"] == 32
