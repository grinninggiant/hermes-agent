"""Offline owner handoff: real bounded hooks, inline dispatch and gateway registration."""
import asyncio
import contextvars
from concurrent.futures import Future
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.agent_runtime_helpers import invoke_tool
from agent.tool_executor import _ToolCallRef, _pre_tool_block, _resolve_sequential_dispatch
from gateway.platforms.base import SendResult
from gateway.run_turn_runner import TurnRunner
from hermes_cli import plugins
from tools import clarify_gateway


@pytest.mark.parametrize("route", ["sequential", "invoke"])
def test_owner_is_captured_before_bounded_hook_and_reaches_registry(monkeypatch, tmp_path, route):
    """A delayed old call must never borrow the new turn's identity or callback."""
    session_key = "owner-handoff"
    sent = []
    hook_threads = []
    hook_owner = contextvars.ContextVar("test_hook_owner", default=None)
    caller_thread = threading.get_ident()
    manager = plugins.PluginManager(scope_key=str(tmp_path))

    class Adapter:
        def pause_typing_for_chat(self, chat_id):
            pass

        def resume_typing_for_chat(self, chat_id):
            pass

        async def send_clarify(self, **kwargs):
            entry = clarify_gateway.get_pending_for_session(session_key, include_choice_prompts=True)
            sent.append((getattr(entry, "turn_owner", None), kwargs["metadata"]))
            clarify_gateway.resolve_gateway_clarify(entry.clarify_id, "yes")
            return SendResult(success=True)

    metadata = {"thread_id": "thread"}
    runner = TurnRunner(None, None)
    runner._ctx = SimpleNamespace(
        _status_adapter=Adapter(), session_key=session_key,
        _status_chat_id="chat", _status_thread_metadata=metadata,
    )
    runner._close_native_stream_boundary = lambda *a, **k: None
    runner._stream_consumer = lambda: None

    def schedule(coro, _message):
        future = Future()
        future.set_result(asyncio.run(coro))
        return future

    runner._schedule = schedule
    replacement_callback = Mock(return_value="wrong callback")
    agent = SimpleNamespace(
        session_id="session-old", _current_turn_id="turn-old",
        clarify_callback=runner._clarify_callback_sync, _memory_manager=None,
    )

    def hook(**ids):
        hook_threads.append(threading.get_ident())
        hook_owner.set((ids["session_id"], ids["turn_id"]))
        # Simulate reuse after the old dispatch was captured, before its callback.
        agent.session_id, agent._current_turn_id = "session-new", "turn-new"
        agent.clarify_callback = replacement_callback

    manager._hooks["pre_tool_call"] = [hook]
    monkeypatch.setattr(plugins, "invoke_hook", manager.invoke_hook)
    monkeypatch.setattr(plugins, "_resolve_hook_callback_timeout", lambda: 5)
    args = {"question": "Target?", "choices": ["yes"], "turn_owner": ["forged", "forged"]}
    try:
        if route == "sequential":
            ref = _ToolCallRef("clarify", args, "task", "call", [])
            dispatch = _resolve_sequential_dispatch(agent, ref, [])
            blocked, final_args = _pre_tool_block(agent, ref)
            assert blocked is None
            result = dispatch.execute(final_args)
        else:
            result = invoke_tool(agent, "clarify", args, "task", "call")
        assert json.loads(result)["user_response"] == "yes"
        assert sent == [(("session-old", "turn-old"), metadata)]
        replacement_callback.assert_not_called()
        assert hook_threads and all(t != caller_thread for t in hook_threads)
        assert hook_owner.get() is None  # copied hook context never came back
    finally:
        clarify_gateway.clear_session(session_key)


def test_stop_cancels_owned_waiter_without_rebinding_it():
    session_key = "owner-stop"
    owner = ("session", "old")
    try:
        entry = clarify_gateway.register("old-prompt", session_key, "Target?", None, turn_owner=owner)
        assert clarify_gateway.clear_session(session_key) == 1
        assert entry.turn_owner == owner
        assert entry.event.is_set()
        assert not clarify_gateway.resolve_gateway_clarify("old-prompt", "late answer")
        assert clarify_gateway.get_pending_for_session(session_key) is None
    finally:
        clarify_gateway.clear_session(session_key)
