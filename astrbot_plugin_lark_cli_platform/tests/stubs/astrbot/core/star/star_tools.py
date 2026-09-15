import os
from pathlib import Path


class StarTools:
    @classmethod
    def get_data_dir(cls, plugin_name=None):
        root = os.environ.get("ASTRBOT_DATA_DIR") or "/tmp/astrbot_data"
        p = Path(root) / (plugin_name or "")
        p.mkdir(parents=True, exist_ok=True)
        return p
