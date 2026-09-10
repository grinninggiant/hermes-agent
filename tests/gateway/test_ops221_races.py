"""Deterministic regressions for OPS-221 turn ownership races."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.run_goals import GatewayGoalsMixin
from gateway.run_turn_runner import TurnRunner
from gateway.session import SessionSource
from gateway.turn_context import TurnContext


def _turn_context(is_current):
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="chat", chat_type="dm", user_id="user")
    ctx = TurnContext(
        source=source, session_key="telegram:chat", run_generation=3,
        session_id="session-3", history=[], context_prompt="", user_config={},
        enabled_toolsets=[], disabled_toolsets=[], AIAgent=MagicMock(),
        _run_still_current=is_current,
    )
    ctx._hooks_ref = SimpleNamespace(loaded_hooks=False)
    return ctx


def test_interrupt_publication_timeout_abandons_late_callback(monkeypatch):
    callbacks = []
    ctx = _turn_context(lambda: True)
    ctx._voice_ack_loop = SimpleNamespace(
        is_running=lambda: True,
        call_soon_threadsafe=lambda callback: callbacks.append(callback),
    )

    class BoundedEvent:
        def __init__(self):
            self.flag = False

        def set(self):
            self.flag = True

        def is_set(self):
            return self.flag

        def wait(self, timeout=None):
            assert timeout is not None and 0 < timeout <= 10
            return False

    monkeypatch.setattr("gateway.run_turn_runner.threading.Event", BoundedEvent)
    runner = MagicMock()
    agent = MagicMock()
    assert TurnRunner(runner, ctx)._publish_agent_for_interrupt(agent) is False
    callbacks[0]()
    assert ctx.agent_holder[0] is None
    runner._session_state.assert_not_called()


def test_worker_fences_generation_change_before_agent_publication():
    """A stop between agent construction and wiring must not enter model/tool work."""
    current = True
    ctx = _turn_context(lambda: current)
    runner = MagicMock()
    runner._provider_routing = {}
    turn_runner = TurnRunner(runner, ctx)
    runner._resolve_session_agent_runtime.return_value = ("model", {"provider": "test"})
    runner._resolve_session_reasoning_config.return_value = {}
    runner._resolve_session_service_tier.return_value = None
    runner._resolve_turn_agent_config.return_value = {"model": "model", "runtime": {}}
    turn_runner._load_turn_history = MagicMock(return_value=([], [], []))
    turn_runner._prepare_turn_message = MagicMock(return_value=(None, None))
    turn_runner._finish_stream_consumer = MagicMock()
    turn_runner._sync_session_after_run = MagicMock(return_value=(False, "session-3", 0))
    agent = MagicMock()

    turn_runner._combined_ephemeral_prompt = MagicMock(return_value="")
    turn_runner._setup_stream_consumer = MagicMock(return_value=(None, None, None, False))

    def resolve_agent(*_args):
        nonlocal current
        current = False
        return agent, False

    turn_runner._resolve_turn_agent = MagicMock(side_effect=resolve_agent)
    turn_runner._wire_turn_agent_callbacks = MagicMock()

    with patch.object(
        turn_runner, "_run_conversation_with_approval",
        return_value={"final_response": "done", "messages": []},
    ) as run_conversation:
        result = turn_runner.run_sync()

    assert result["interrupted"] is True
    turn_runner._wire_turn_agent_callbacks.assert_not_called()
    run_conversation.assert_not_called()
    assert ctx.agent_holder[0] is None


@pytest.mark.asyncio
async def test_stale_policy_text_does_not_resolve_or_judge_replacement_session():
    """Discarded policy text from generation N cannot drive hooks for generation N+1."""
    runner = object.__new__(GatewayRunner)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="chat", chat_type="dm", user_id="user")
    event = SimpleNamespace(
        _gateway_post_turn_response="old response",
        _gateway_post_turn_response_policy_staged=True,
        _gateway_post_turn_response_session_key="telegram:chat",
        _gateway_post_turn_response_session_id="old-session",
        _gateway_post_turn_response_generation=3,
        _gateway_post_turn_response_stale=True,
    )
    runner._is_session_run_current = MagicMock(return_value=False)
    runner._session_key_for_source = MagicMock(return_value="telegram:chat")
    runner.session_store = MagicMock()
    runner._async_session_store = SimpleNamespace(
        _store=runner.session_store, get_or_create_session=AsyncMock(),
    )
    runner._post_turn_goal_continuation = AsyncMock()
    runner._post_turn_loop_completion = AsyncMock()

    await GatewayGoalsMixin._run_post_turn_hooks(
        runner, agent_result=None, source=source, is_internal=True, event=event,
    )

    runner.async_session_store.get_or_create_session.assert_not_awaited()
    runner._post_turn_goal_continuation.assert_not_awaited()
    runner._post_turn_loop_completion.assert_not_awaited()


@pytest.mark.asyncio
async def test_policy_hook_rechecks_generation_after_session_lookup():
    runner = object.__new__(GatewayRunner)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="chat", chat_type="dm", user_id="user")
    event = SimpleNamespace(
        _gateway_post_turn_response="old response",
        _gateway_post_turn_response_policy_staged=True,
        _gateway_post_turn_response_session_key="telegram:chat",
        _gateway_post_turn_response_session_id="old-session",
        _gateway_post_turn_response_generation=3,
    )
    current = True

    async def lookup(_key):
        nonlocal current
        current = False
        return SimpleNamespace(session_id="old-session")

    runner._is_session_run_current = lambda *_args: current
    runner._session_key_for_source = lambda _source: "telegram:chat"
    runner.session_store = MagicMock()
    runner._async_session_store = SimpleNamespace(
        _store=runner.session_store,
        lookup_by_session_key=AsyncMock(side_effect=lookup),
        get_or_create_session=AsyncMock(return_value=SimpleNamespace(session_id="replacement")),
    )
    runner._post_turn_goal_continuation = AsyncMock()
    runner._post_turn_loop_completion = AsyncMock()
    await runner._run_post_turn_hooks(
        agent_result=None, source=source, is_internal=True, event=event,
    )
    runner._post_turn_goal_continuation.assert_not_awaited()
    runner._post_turn_loop_completion.assert_not_awaited()
    runner.async_session_store.get_or_create_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_public_interrupt_before_loop_publication_rejects_pending_agent():
    """A public stop that wins while the slot is pending prevents worker admission."""
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="chat", chat_type="dm", user_id="user")
    state = SimpleNamespace(turn=SimpleNamespace(agent=object()))
    current = True
    runner = MagicMock()
    runner._session_state.return_value = state
    runner._is_session_run_current.side_effect = lambda *_args: current
    turn_ctx = _turn_context(lambda: current)
    turn_ctx._voice_ack_loop = asyncio.get_running_loop()
    turn_ctx.session_key = "telegram:chat"
    turn_runner = TurnRunner(runner, turn_ctx)

    async def public_interrupt():
        nonlocal current
        current = False

    # This is the public interrupt's invalidation effect; the publication rendezvous must observe
    # it on the gateway loop rather than promoting the pending worker from its executor thread.
    await public_interrupt()
    assert await asyncio.to_thread(turn_runner._publish_agent_for_interrupt, MagicMock()) is False
    assert turn_ctx.agent_holder[0] is None
    assert state.turn.agent is not turn_ctx.agent_holder[0]


@pytest.mark.asyncio
async def test_policy_hook_accepts_native_compaction_child_for_goal_and_loop():
    runner = object.__new__(GatewayRunner)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="chat", chat_type="dm", user_id="user")
    event = SimpleNamespace(
        _gateway_post_turn_response="compressed response",
        _gateway_post_turn_response_policy_staged=True,
        _gateway_post_turn_response_session_key="telegram:chat",
        _gateway_post_turn_response_session_id="compressed-child",
        _gateway_post_turn_response_generation=3,
    )
    child = SimpleNamespace(session_id="compressed-child")
    runner._is_session_run_current = MagicMock(return_value=True)
    runner._session_key_for_source = MagicMock(return_value="telegram:chat")
    runner.session_store = MagicMock()
    runner._async_session_store = SimpleNamespace(
        _store=runner.session_store, lookup_by_session_key=AsyncMock(return_value=child),
    )
    runner._post_turn_goal_continuation = AsyncMock()
    runner._post_turn_loop_completion = AsyncMock()

    await runner._run_post_turn_hooks(
        agent_result=None, source=source, is_internal=True, event=event,
    )

    runner._post_turn_goal_continuation.assert_awaited_once_with(
        session_entry=child, source=source, final_response="compressed response",
    )
    runner._post_turn_loop_completion.assert_awaited_once_with(
        session_entry=child, source=source, final_response="compressed response",
    )
