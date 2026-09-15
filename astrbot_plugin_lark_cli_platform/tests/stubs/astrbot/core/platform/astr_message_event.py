"""最小桩:MessageSesion。"""

from __future__ import annotations


class MessageSesion:
    def __init__(self, session_id="", platform_name=None, message_type=None):
        self.session_id = session_id
        self.platform_name = platform_name
        self.message_type = message_type
