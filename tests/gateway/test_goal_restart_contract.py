"""OPS-221 RED contracts for native goal/restart ownership.

These tests deliberately exercise the concrete ``GatewayRunner`` MRO, a real
SQLite-backed ``SessionStore`` reopened from the temporary Hermes home, and
the native ``GoalManager``.  They are expected to fail on the unfixed tree.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import asyncio

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.run import GatewayRunner
from gateway.session import AsyncSessionStore, SessionSource, SessionStore
from hermes_cli.goals import GoalManager


@pytest.fixture
def restart_world(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))

    config = GatewayConfig(
        sessions_dir=home / "sessions",
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="test")},
    )
    config.sessions_dir.mkdir(parents=True, exist_ok=True)
    store = SessionStore(config.sessions_dir, config)
    runner = object.__new__(GatewayRunner)
    runner.config = config
    runner.session_store = store
    runner._async_session_store = AsyncSessionStore(store)
    runner.__dict__["_sessions"] = {}
    runner._post_turn_work_owners = {}
    runner.adapters = {}

    from hermes_cli import goals

    goals._DB_CACHE.clear()
    yield home, runner, store
    goals._DB_CACHE.clear()
    store.close_all_db_handles()


def _source(chat_id: str) -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id=chat_id,
        chat_type="dm",
        user_id="ops-221-user",
    )


def _persist_continue(session_id: str) -> GoalManager:
    manager = GoalManager(session_id=session_id)
    manager.set("finish the restart contract")
    manager.state.last_verdict = "continue"
    manager._save()
    return manager


def test_restart_goal_obligation_uses_session_id_not_routing_key(restart_world):
    """A native goal belongs to the durable session, not its gateway route key."""
    _home, runner, store = restart_world
    source = _source("chat-with-a-different-id")
    entry = store.get_or_create_session(source)
    assert entry.session_key != entry.session_id
    _persist_continue(entry.session_id)

    reopened = SessionStore(runner.config.sessions_dir, runner.config)
    try:
        runner.session_store = reopened
        assert runner._goal_continuation_pending(entry.session_id)
    finally:
        reopened.close_all_db_handles()


@pytest.mark.asyncio
async def test_release_before_agent_return_remains_active_for_restart_barrier():
    """The real inbound lifecycle owns the release-to-return handoff window."""
    from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source

    runner, _adapter = make_restart_runner()
    source = make_restart_source()
    event = SimpleNamespace(source=source, text="finish", internal=False)
    key = runner._session_key_for_source(source)
    released = asyncio.Event()
    allow_return = asyncio.Event()

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
        runner._release_running_agent_state(key)
        released.set()
        await allow_return.wait()
        return "reply"

    runner._handle_message_with_agent = finished_turn
    runner._run_post_turn_hooks = AsyncMock()
    handling = asyncio.create_task(runner._handle_message(event))
    await asyncio.wait_for(released.wait(), timeout=1.0)
    assert runner._active_work_count() == 1
    allow_return.set()
    await handling
    assert runner._active_work_count() == 0


def test_waiting_goal_with_continue_verdict_is_not_restart_obligation(restart_world):
    """A parked wait is not actionable continuation merely because its last verdict was continue."""
    _home, runner, store = restart_world
    entry = store.get_or_create_session(_source("parked-goal"))
    manager = _persist_continue(entry.session_id)
    manager.wait_for_seconds(600, reason="waiting for the external result")
    # Preserve the durable judge verdict: the wait barrier, not a rewritten verdict,
    # is what makes this obligation non-actionable.
    manager.state.last_verdict = "continue"
    manager._save()

    reopened = SessionStore(runner.config.sessions_dir, runner.config)
    try:
        runner.session_store = reopened
        assert not runner._goal_continuation_pending(entry.session_id)
    finally:
        reopened.close_all_db_handles()


def test_inactive_goal_recovery_aborts_real_turn_before_model_call(restart_world, monkeypatch):
    """The native TurnRunner startup path must fail closed before the model call."""
    _home, runner, store = restart_world
    entry = store.get_or_create_session(_source("stale-continuation"))
    manager = GoalManager(entry.session_id)
    manager.set("goal that was cancelled")
    manager.pause("cancelled during recovery admission")

    from gateway.run_turn_runner import TurnRunner
    from gateway.turn_context import TurnContext

    source = _source("stale-continuation")
    model_calls = []
    class CountingAgent:
        session_id = entry.session_id
        model = "counting-model"

        def run_conversation(self, *_args, **_kwargs):
            model_calls.append(True)
            raise AssertionError("inactive goal recovery reached the model")

    ctx = TurnContext(
        source=source,
        message="",
        history=[],
        session_id=entry.session_id,
        session_key=entry.session_key,
        run_generation=1,
        gateway_event=SimpleNamespace(_gateway_goal_continuation_recovery=True),
        user_config={},
        _run_still_current=lambda: True,
    )
    turn_runner = TurnRunner(runner, ctx)
    runner._provider_routing = {}
    runner._resolve_session_agent_runtime = MagicMock(return_value=("model", {"provider": "test"}))
    runner._resolve_session_reasoning_config = MagicMock(return_value={})
    runner._resolve_session_service_tier = MagicMock(return_value=None)
    runner._resolve_turn_agent_config = MagicMock(return_value={"model": "model", "runtime": {}})
    runner._get_system_prompt_for_channel = MagicMock(return_value="")
    monkeypatch.setattr(turn_runner, "_resolve_turn_agent", MagicMock(return_value=(CountingAgent(), False)))
    monkeypatch.setattr(turn_runner, "_publish_agent_for_interrupt", MagicMock(return_value=True))
    monkeypatch.setattr(turn_runner, "_wire_turn_agent_callbacks", MagicMock())
    monkeypatch.setattr(turn_runner, "_load_turn_history", MagicMock(return_value=([], [], [])))
    monkeypatch.setattr(turn_runner, "_setup_stream_consumer", MagicMock(return_value=(None, None, None, False)))

    result = turn_runner.run_sync()

    assert model_calls == []
    assert result["completed"] is False
    assert result["turn_exit_reason"] == "goal_continuation_recovery_invalid"
