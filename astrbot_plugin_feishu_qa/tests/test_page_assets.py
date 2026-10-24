"""可视化页面静态资源存在性测试(防止页面文件被误删/漏打包)。"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PAGE = ROOT / "pages" / "feishu_qa"


def test_page_files_and_references() -> None:
    for name in ("index.html", "app.js", "style.css"):
        assert (PAGE / name).is_file(), name
    html = (PAGE / "index.html").read_text(encoding="utf-8")
    assert "./app.js" in html and "./style.css" in html
    js = (PAGE / "app.js").read_text(encoding="utf-8")
    for endpoint in (
        'apiGet("status")',
        'apiGet("settings")',
        'apiGet("fuuumusic/sections")',
        'apiPost("settings"',
        'apiPost("reload"',
        'apiPost("kb_watch/check"',
    ):
        assert endpoint in js, endpoint


def test_page_i18n_title() -> None:
    for locale in ("zh-CN", "en-US"):
        path = ROOT / ".astrbot-plugin" / "i18n" / f"{locale}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["pages"]["feishu_qa"]["title"]


def test_package_mirror_has_pages() -> None:
    mirror = ROOT / "astrbot_plugin_feishu_qa" / "pages" / "feishu_qa" / "index.html"
    assert mirror.is_file()
