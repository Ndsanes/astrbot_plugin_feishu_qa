"""pytest 全局配置:astrbot 不可用(或缺 Platform API)时注入桩包。"""

from __future__ import annotations

import sys
from pathlib import Path

STUBS = Path(__file__).parent / "stubs"

try:
    import astrbot.api.platform  # noqa: F401
except ImportError:
    sys.path.insert(0, str(STUBS))
