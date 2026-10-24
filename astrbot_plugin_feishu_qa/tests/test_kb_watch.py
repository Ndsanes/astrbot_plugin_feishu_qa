"""官方手册 PDF 每周检查的纯逻辑测试。

不联网:HEAD 与状态文件都用 monkeypatch/临时目录替换。重点是"基线只在
重建成功后才推进"——检测到更新后不能被静默吞掉。
"""

from __future__ import annotations

import json
from pathlib import Path

from astrbot_plugin_feishu_qa.kb_watch import (
    RemoteInfo,
    check_once,
    evaluate,
    load_state,
    save_state,
    status_line,
)


def _remote(etag: str = "abc", size: int = 100, stamp: str = "Fri, 23 Oct 2026") -> RemoteInfo:
    return RemoteInfo("https://example.invalid/x.pdf", size, etag, stamp)


class TestEvaluate:
    def test_first_run_records_baseline(self) -> None:
        state = evaluate({}, _remote())
        assert state["status"] == "baseline"
        assert state["baseline"]["etag"] == "abc"
        assert state["baseline"]["bytes"] == 100
        assert state["pending"] is None

    def test_same_file_is_up_to_date(self) -> None:
        state = evaluate({}, _remote())
        again = evaluate(state, _remote())
        assert again["status"] == "up_to_date"
        assert again["pending"] is None
        assert again["baseline"]["at"] == state["baseline"]["at"]

    def test_changed_file_is_update_available_without_advancing_baseline(self) -> None:
        state = evaluate({}, _remote())
        changed = evaluate(state, _remote(etag="def", size=120))
        assert changed["status"] == "update_available"
        assert changed["pending"]["etag"] == "def"
        # 基线仍是旧的:重建成功前不能"假装已经处理过"
        assert changed["baseline"]["etag"] == "abc"
        assert changed["baseline"]["bytes"] == 100

    def test_changed_again_keeps_single_pending(self) -> None:
        state = evaluate({}, _remote())
        first = evaluate(state, _remote(etag="def", size=120))
        second = evaluate(first, _remote(etag="ghi", size=130))
        assert second["pending"]["etag"] == "ghi"
        assert second["status"] == "update_available"

    def test_no_etag_falls_back_to_size_and_time(self) -> None:
        state = evaluate({}, _remote(etag="", size=100, stamp="t1"))
        assert evaluate(state, _remote(etag="", size=100, stamp="t1"))["status"] == "up_to_date"
        assert (
            evaluate(state, _remote(etag="", size=100, stamp="t2"))["status"]
            == "update_available"
        )


class TestStatePersistence:
    def test_save_and_load(self, tmp_path: Path) -> None:
        path = tmp_path / "kb_watch.json"
        assert load_state(path) == {}
        save_state(path, {"status": "up_to_date"})
        assert json.loads(path.read_text())["status"] == "up_to_date"
        assert load_state(path)["status"] == "up_to_date"

    def test_corrupt_state_degrades_to_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "kb_watch.json"
        path.write_text("{bad")
        assert load_state(path) == {}

    def test_check_once_persists_result(self, tmp_path: Path, monkeypatch) -> None:
        path = tmp_path / "kb_watch.json"
        monkeypatch.setattr(
            "astrbot_plugin_feishu_qa.kb_watch.remote_info",
            lambda *a, **k: _remote(etag="v1", size=10),
        )
        first = check_once(path)
        assert first["status"] == "baseline"
        assert load_state(path)["baseline"]["etag"] == "v1"

        monkeypatch.setattr(
            "astrbot_plugin_feishu_qa.kb_watch.remote_info",
            lambda *a, **k: _remote(etag="v2", size=20),
        )
        second = check_once(path)
        assert second["status"] == "update_available"
        assert load_state(path)["pending"]["etag"] == "v2"
        # 基线没被推进
        assert load_state(path)["baseline"]["etag"] == "v1"


class TestStatusLine:
    def test_empty(self) -> None:
        assert status_line(None) == "手册 PDF: 尚未检查"
        assert status_line({}) == "手册 PDF: 尚未检查"

    def test_update_pending(self) -> None:
        state = evaluate({}, _remote())
        state = evaluate(state, _remote(etag="def", size=120, stamp="Sat, 24 Oct 2026"))
        assert "有更新待同步" in status_line(state)
        assert "Sat, 24 Oct 2026" in status_line(state)

    def test_up_to_date(self) -> None:
        state = evaluate({}, _remote())
        state = evaluate(state, _remote())
        assert "已是最新" in status_line(state)
