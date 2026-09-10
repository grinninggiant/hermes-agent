"""Ownership regression through the real AsyncSessionStore offload boundary."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.run import GatewayRunner
from gateway.run_goals import GatewayGoalsMixin
from gateway.run_shutdown import GatewayShutdownMixin
from gateway.session import AsyncSessionStore, SessionSource, SessionStore


@pytest.mark.asyncio
@pytest.mark.parametrize("consumer", ("goal_handoff", "shutdown"))
async def test_delayed_marker_cannot_mark_replacement_session(tmp_path, consumer):
    """Both gateway marker consumers reject a reset that wins before store-lock entry."""
    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    source = SessionSource(
        platform=Platform.TELEGRAM, chat_id="owner-race", chat_type="dm", user_id="user",
    )
    entry = store.get_or_create_session(source)
    async_store = AsyncSessionStore(store)
    owner_started = threading.Event()
    allow_owner_check = threading.Event()

    underlying_mark = store.mark_resume_pending

    def delayed_mark(*args, **kwargs):
        """Pause before the actual store method can acquire ``SessionStore._lock``."""
        owner_started.set()
        assert allow_owner_check.wait(timeout=5)
        return underlying_mark(*args, **kwargs)

    # AsyncSessionStore resolves this instance attribute when the worker is invoked. Wrapping
    # the real synchronous method here keeps the test on the production async boundary while
    # ensuring reset_session() never contends with a callback already under the store lock.
    store.mark_resume_pending = delayed_mark

    if consumer == "goal_handoff":
        runner = object.__new__(GatewayRunner)
        key = entry.session_key
        adapter = SimpleNamespace(
            _active_sessions={key: SimpleNamespace(_hermes_run_generation=3)},
        )
        runner._adapter_for_source = lambda _source: adapter
        runner._session_key_for_source = lambda _source: key
        runner._is_session_run_current = lambda *_args: True
        runner._async_session_store = async_store
        runner.session_store = store
        runner._draining = True
        runner._restart_requested = True
        runner._warm_goals_session_db = AsyncMock()
        runner._post_turn_manager = AsyncMock(
            return_value=SimpleNamespace(
                is_active=lambda: True,
                evaluate_after_turn=lambda *_args, **_kwargs: {
                    "should_continue": True,
                    "continuation_prompt": "continue",
                    "message": "continuing",
                    "verdict": "continue",
                },
            ),
        )
        runner._defer_goal_status_notice_after_delivery = AsyncMock()
        operation = GatewayGoalsMixin._post_turn_goal_continuation(
            runner, session_entry=entry, source=source, final_response="done",
        )
    else:
        runner = object.__new__(GatewayRunner)
        runner.session_store = store
        runner._async_session_store = async_store
        runner._running_agents = {entry.session_key: SimpleNamespace(session_id=entry.session_id)}
        runner._restart_requested = True
        runner._peek_session_state = lambda _key: SimpleNamespace(
            persistent=SimpleNamespace(run_generation=3),
        )
        runner._is_session_run_current = lambda *_args: True
        operation = GatewayShutdownMixin._mark_running_sessions_resume_pending(
            runner, "owner reset race",
        )

    marking = asyncio.create_task(operation)
    await asyncio.wait_for(asyncio.to_thread(owner_started.wait, 5), timeout=5)
    try:
        replacement = store.reset_session(entry.session_key)
    finally:
        allow_owner_check.set()

    result = await asyncio.wait_for(marking, timeout=5)
    assert result == ([] if consumer == "shutdown" else None)
    assert replacement.session_id != entry.session_id
    assert store.lookup_by_session_key(entry.session_key).resume_pending is False
