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


@pytest.mark.asyncio
async def test_restart_wait_owns_post_turn_goal_judge_until_handoff_finishes():
    """A restart must not stop while the post-turn goal judge still owns the turn.

    ``_run_agent`` releases its running-agent slot before the outer inbound handler runs the
    post-turn hooks.  The barrier makes that ordering deterministic and catches a restart task
    that observes the released slot while the auxiliary judge is still pending.
    """
    from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source

    runner, _adapter = make_restart_runner()
    source = make_restart_source()
    event = SimpleNamespace(source=source, text="finish", internal=False)
    key = runner._session_key_for_source(source)
    judge_started = asyncio.Event()
    allow_judge = asyncio.Event()

    runner._hm_admit_event = AsyncMock(return_value=(event, source, False))
    runner._hm_estop_gate = MagicMock(return_value=None)
    runner._hm_pending_reply_intercepts = AsyncMock(return_value=None)
    runner._hm_evict_idle_stale_agent = MagicMock()
    runner._hm_evict_reaped_agent = MagicMock()
    runner._is_session_running = lambda session_key: session_key in runner._running_agents
    runner._hm_dispatch_idle_commands = AsyncMock(return_value=(False, None))
    runner._claim_active_session_slot = MagicMock(return_value=(None, None))
    runner._begin_session_run_generation = MagicMock(return_value=1)
    runner._restore_moa_one_shot = MagicMock()
    runner._restore_pending_one_turn_model_override = MagicMock()
    runner._clear_durable_active_turn = AsyncMock()
    runner._release_turn_lease = MagicMock()

    async def finished_turn(*_args):
        # This is the release performed by _run_agent's cleanup before its caller returns.
        runner._release_running_agent_state(key)
        runner.request_restart(detached=False, via_service=True)
        return "reply"

    async def blocked_post_turn(**_kwargs):
        judge_started.set()
        await allow_judge.wait()

    runner._handle_message_with_agent = finished_turn
    runner._run_post_turn_hooks = blocked_post_turn
    runner.stop = AsyncMock()
    runner._restart_after_turn_timeout = 1.0

    handling = asyncio.create_task(runner._handle_message(event))
    await asyncio.wait_for(judge_started.wait(), timeout=1.0)
    assert runner._active_work_count() == 1
    await asyncio.sleep(0.15)

    assert runner.stop.await_count == 0
    allow_judge.set()
    await handling
    await runner._restart_task
    runner.stop.assert_awaited_once_with(
        restart=True, detached_restart=False, service_restart=True,
    )


@pytest.mark.asyncio
async def test_draining_goal_continuation_is_handed_to_durable_resume_path():
    """A judged continuation must survive adapter FIFO teardown once drain owns shutdown."""
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="chat", chat_type="dm", user_id="user")
    runner = object.__new__(GatewayRunner)
    marker = AsyncMock()
    runner.session_store = MagicMock()
    runner._async_session_store = SimpleNamespace(_store=runner.session_store, mark_resume_pending=marker)
    runner._draining = True
    runner._restart_requested = True
    runner._session_key_for_source = MagicMock(return_value="telegram:chat")
    runner._post_turn_manager = AsyncMock(
        return_value=SimpleNamespace(
            is_active=lambda: True,
            evaluate_after_turn=lambda *_args, **_kwargs: {
                "should_continue": True,
                "continuation_prompt": "continue",
                "message": "continuing",
                "verdict": "continue",
            },
        )
    )
    runner._run_in_executor_with_context = lambda fn: asyncio.sleep(0, result=fn())
    runner._defer_goal_status_notice_after_delivery = AsyncMock()
    runner._enqueue_fifo = MagicMock()

    await GatewayGoalsMixin._post_turn_goal_continuation(
        runner, session_entry=SimpleNamespace(session_id="session"),
        source=source, final_response="done",
    )

    marker.assert_awaited_once_with(
        "telegram:chat", "goal_continuation",
        expected_session_id="session", owner_check=None,
    )
    runner._enqueue_fifo.assert_not_called()


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
