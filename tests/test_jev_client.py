"""Jev 客户端测试(全部用注入的假 transport,不联网、不需要 API key)。

重点不是"能问出 0.87",而是**任何失败路径都不得冒充成低分**:一旦把
"没问成"当成"答案不相关"读,降级逻辑就会把本该答的问题静默掉。
"""

from __future__ import annotations

import json

import pytest

from astrbot_plugin_feishu_qa.jev.client import (
    DEFAULT_ENDPOINT,
    DEFAULT_MODEL,
    JevClient,
    noul_question,
)


def _client(payload=None, *, raises: Exception | None = None, **kw) -> JevClient:
    """假 transport:要么返回 payload,要么抛 raises。"""

    def transport(url, headers, body, timeout):
        if raises is not None:
            raise raises
        return payload

    return JevClient("test-key", transport=transport, **kw)


ANSWER_OK = {
    "model": "jev-1.13.0",
    "answers": {"sufficient_0": {"type": "noul", "noul": 0.87}},
    "usage": {"input_tokens": 296, "output_tokens": 20},
}


class TestHappyPath:
    def test_parses_noul_and_metadata(self) -> None:
        res = _client(ANSWER_OK).evaluate("s", {"sufficient_0": {"type": "noul"}})
        assert res.ok is True
        assert res.noul("sufficient_0") == 0.87
        assert res.model == "jev-1.13.0"
        assert res.input_tokens == 296

    def test_parses_multiple_questions(self) -> None:
        payload = {
            "answers": {
                "needs_synthesis": {"type": "noul", "noul": 0.31},
                "sufficient_0": {"type": "noul", "noul": 0.9},
                "sufficient_1": {"type": "noul", "noul": 0.2},
            }
        }
        res = _client(payload).evaluate("s", {"a": {}, "b": {}, "c": {}})
        assert res.nouls == {"needs_synthesis": 0.31, "sufficient_0": 0.9, "sufficient_1": 0.2}

    def test_integer_noul_accepted(self) -> None:
        payload = {"answers": {"q": {"type": "noul", "noul": 1}}}
        assert _client(payload).evaluate("s", {"q": {}}).noul("q") == 1.0

    def test_state_may_be_structured(self) -> None:
        seen = {}

        def transport(url, headers, body, timeout):
            seen["body"] = json.loads(body)
            return ANSWER_OK

        c = JevClient("k", transport=transport)
        c.evaluate({"question": "混音台在哪", "candidates": ["a", "b"]}, {"q": {}})
        assert seen["body"]["state"]["question"] == "混音台在哪"
        assert seen["body"]["state"]["candidates"] == ["a", "b"]

    def test_authorization_header_set_and_model_sent(self) -> None:
        seen = {}

        def transport(url, headers, body, timeout):
            seen.update(url=url, headers=headers, body=json.loads(body))
            return ANSWER_OK

        JevClient("secret-key", transport=transport).evaluate("s", {"q": {}})
        assert seen["url"] == DEFAULT_ENDPOINT
        assert seen["headers"]["Authorization"] == "Bearer secret-key"
        assert seen["body"]["model"] == DEFAULT_MODEL


class TestNeverRaises:
    """失败必须变成 ok=False,而不是异常。"""

    @pytest.mark.parametrize(
        "exc",
        [
            TimeoutError("timed out"),
            ConnectionResetError("reset"),
            OSError("network down"),
            ValueError("bad"),
        ],
    )
    def test_transport_exception_is_swallowed(self, exc: Exception) -> None:
        res = _client(raises=exc).evaluate("s", {"q": {}})
        assert res.ok is False
        assert res.noul("q") is None

    @pytest.mark.parametrize(
        "payload",
        [
            None,
            {},
            {"answers": None},
            {"answers": []},
            {"answers": {"q": "not a dict"}},
            {"answers": {"q": {"type": "choice", "choice": "x"}}},
            {"answers": {"q": {"type": "noul"}}},
            {"answers": {"q": {"type": "noul", "noul": "high"}}},
            {"answers": {"q": {"type": "noul", "noul": True}}},
            {"answers": {"q": {"type": "noul", "noul": 1.7}}},
            {"answers": {"q": {"type": "noul", "noul": -0.1}}},
            [],
            "string payload",
        ],
    )
    def test_malformed_response_is_not_ok(self, payload) -> None:
        res = _client(payload).evaluate("s", {"q": {}})
        assert res.ok is False
        assert res.noul("q") is None

    def test_missing_key_is_not_ok(self) -> None:
        c = JevClient("", transport=lambda *a: ANSWER_OK)
        assert c.evaluate("s", {"q": {}}).ok is False

    def test_empty_questions_is_not_ok(self) -> None:
        assert _client(ANSWER_OK).evaluate("s", {}).ok is False

    def test_error_never_contains_key(self) -> None:
        res = _client(raises=RuntimeError("boom-secret-key")).evaluate("s", {"q": {}})
        assert "secret-key" not in res.error


class TestFailureIsNotLowScore:
    """核心不变量:失败 ≠ 低分。二者必须可区分。"""

    def test_failed_call_yields_none_not_zero(self) -> None:
        res = _client(raises=TimeoutError()).evaluate("s", {"q": {}})
        assert res.ok is False
        assert res.noul("q") is None  # 绝不是 0.0

    def test_real_low_score_is_still_zero_with_ok_true(self) -> None:
        payload = {"answers": {"q": {"type": "noul", "noul": 0.0}}}
        res = _client(payload).evaluate("s", {"q": {}})
        assert res.ok is True
        assert res.noul("q") == 0.0

    def test_absent_answer_within_ok_call_is_none(self) -> None:
        """部分问题缺失:整体 ok,但缺的那一问是 None 而非 0。"""
        payload = {"answers": {"a": {"type": "noul", "noul": 0.4}}}
        res = _client(payload).evaluate("s", {"a": {}, "b": {}})
        assert res.ok is True
        assert res.noul("a") == 0.4
        assert res.noul("b") is None


class TestNoulQuestion:
    def test_minimal_shape(self) -> None:
        q = noul_question("这条能回答问题吗?")
        assert q == {"type": "noul", "instructions": "这条能回答问题吗?"}

    def test_criteria_included_when_given(self) -> None:
        q = noul_question("q", true="是", false_="否")
        assert q["criteria"] == {"true": "是", "false": "否"}

    def test_criteria_omitted_when_blank(self) -> None:
        q = noul_question("q", true="", false_="")
        assert "criteria" not in q

    def test_partial_criteria_kept(self) -> None:
        assert noul_question("q", true="是")["criteria"] == {"true": "是"}


class TestConfigurability:
    def test_custom_endpoint_and_model(self) -> None:
        seen = {}

        def transport(url, headers, body, timeout):
            seen["url"] = url
            seen["model"] = json.loads(body)["model"]
            return ANSWER_OK

        JevClient(
            "k", endpoint="https://x/v1/systemone", model="jev-1.13.0",
            transport=transport,
        ).evaluate("s", {"q": {}})
        assert seen["url"] == "https://x/v1/systemone"
        assert seen["model"] == "jev-1.13.0"

    def test_timeout_floor_applied(self) -> None:
        seen = {}
        JevClient(
            "k", timeout=0.0,
            transport=lambda u, h, b, t: seen.setdefault("t", t) and ANSWER_OK or ANSWER_OK,
        ).evaluate("s", {"q": {}})
        assert seen["t"] >= 0.1
