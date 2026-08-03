"""
空响应 guard 测试（8_03_011：orchestrator 空响应导致 fallback 裸结束）。

覆盖：_is_empty_response 判定各形态；ainvoke_with_content_guard 的
重发（1 次）、双空返回、allow_empty 直通、正常响应零开销。
"""

import asyncio
from types import SimpleNamespace

from langchain_core.messages import AIMessage

from utils.model import _is_empty_response, ainvoke_with_content_guard


def _tc():
    return {"name": "bash", "args": {}, "id": "tc-1", "type": "tool_call"}


class TestIsEmptyResponse:
    """空响应判定：无 tool_calls 且 content 空才算空。"""

    def test_none_content_is_empty(self):
        # AIMessage 不允许 content=None，真实空响应是空串；
        # None 分支用普通对象防御（某些 provider 边界情况）。
        assert _is_empty_response(SimpleNamespace(content=None))

    def test_blank_string_is_empty(self):
        assert _is_empty_response(AIMessage(content="   \n  "))

    def test_empty_text_blocks_are_empty(self):
        assert _is_empty_response(AIMessage(content=[{"type": "text", "text": ""}]))

    def test_normal_text_not_empty(self):
        assert not _is_empty_response(AIMessage(content="done"))

    def test_tool_calls_not_empty_even_without_content(self):
        # 纯工具调用轮：content 空是正常的，绝不能误判为空响应。
        assert not _is_empty_response(AIMessage(content="", tool_calls=[_tc()]))

    def test_partial_blocks_not_empty(self):
        assert not _is_empty_response(
            AIMessage(
                content=[{"type": "text", "text": ""}, {"type": "text", "text": "x"}]
            )
        )

    def test_non_dict_block_conservative(self):
        # 意外形态（非 dict block）→ 保守判非空，避免误重发。
        assert not _is_empty_response(AIMessage(content=["unexpected"]))


class _FakeRunnable:
    """按调用次数依次返回预设结果，记录调用次数。"""

    def __init__(self, results):
        self.results = results
        self.calls = 0

    async def ainvoke(self, *args, **kwargs):
        self.calls += 1
        return self.results[min(self.calls - 1, len(self.results) - 1)]


class TestAinvokeWithContentGuard:
    """guard 行为：空响应重发一次；双空返回原样；正常/allow_empty 不重发。"""

    def test_empty_then_ok_retries_once(self):
        fake = _FakeRunnable([AIMessage(content=""), AIMessage(content="ok")])
        result = asyncio.run(ainvoke_with_content_guard(fake, []))
        assert fake.calls == 2
        assert result.content == "ok"

    def test_double_empty_returns_second_as_is(self):
        fake = _FakeRunnable([AIMessage(content=""), AIMessage(content="")])
        result = asyncio.run(ainvoke_with_content_guard(fake, []))
        assert fake.calls == 2
        assert _is_empty_response(result)

    def test_normal_response_no_retry(self):
        fake = _FakeRunnable([AIMessage(content="ok")])
        result = asyncio.run(ainvoke_with_content_guard(fake, []))
        assert fake.calls == 1
        assert result.content == "ok"

    def test_allow_empty_no_retry(self):
        fake = _FakeRunnable([AIMessage(content="")])
        result = asyncio.run(ainvoke_with_content_guard(fake, [], allow_empty=True))
        assert fake.calls == 1
        assert _is_empty_response(result)

    def test_tool_calls_response_no_retry(self):
        fake = _FakeRunnable([AIMessage(content="", tool_calls=[_tc()])])
        result = asyncio.run(ainvoke_with_content_guard(fake, []))
        assert fake.calls == 1
        assert result.tool_calls
