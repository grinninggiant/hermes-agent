"""OPS-221: delivery recovery must not consume a judged native goal continuation."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import asyncio

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.delivery_ledger import record_obligation
from gateway.run import GatewayRunner, _GOAL_CONTINUATION_RESUME_REASON
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
    config.sessions_dir.mkdir(parents=True)
    store = SessionStore(config.sessions_dir, config)
    runner = object.__new__(GatewayRunner)
    runner.config = config
    runner.session_store = store
    runner._async_session_store = AsyncSessionStore(store)
    runner.adapters = {Platform.TELEGRAM: SimpleNamespace()}
    runner._profile_adapters = {}
    runner._post_turn_work_owners = {}
    from hermes_cli import goals
    goals._DB_CACHE.clear()
    yield runner, store
    goals._DB_CACHE.clear()
    store.close_all_db_handles()


def _source(chat_id="goal-delivery"):
    return SessionSource(
        platform=Platform.TELEGRAM, chat_id=chat_id, chat_type="dm", user_id="ops-221",
    )


@pytest.mark.asyncio
async def test_claimed_answer_delivery_preserves_goal_handoff_and_schedules_once(restart_world):
    runner, store = restart_world
    entry = store.get_or_create_session(_source())
    manager = GoalManager(session_id=entry.session_id)
    manager.set("finish the durable task")
    manager.state.last_verdict = "continue"
    manager._save()
    assert store.mark_resume_pending(entry.session_key, _GOAL_CONTINUATION_RESUME_REASON)

    record_obligation(
        obligation_id="ops-221-goal-answer",
        session_key=entry.session_key,
        platform="telegram",
        chat_id="goal-delivery",
        thread_id=None,
        content="the judged answer",
    )
    from gateway import delivery_ledger
    with delivery_ledger._connect() as conn:
        conn.execute(
            "UPDATE delivery_obligations SET owner_pid=? WHERE obligation_id=?",
            (999999999, "ops-221-goal-answer"),
        )

    claimed = await runner._claim_pending_obligations()
    assert [row["obligation_id"] for row in claimed] == ["ops-221-goal-answer"]

    reopened = SessionStore(runner.config.sessions_dir, runner.config)
    try:
        runner.session_store = reopened
        preserved = reopened.lookup_by_session_key(entry.session_key)
        assert preserved.resume_pending is True
        assert preserved.resume_reason == _GOAL_CONTINUATION_RESUME_REASON
        assert [e.session_key for e in runner._resume_pending_candidates()] == [entry.session_key]
    finally:
        reopened.close_all_db_handles()

    runner._adapter_for_source = lambda source: runner.adapters[Platform.TELEGRAM]
    runner._is_user_authorized = lambda source: True
    runner._is_session_running = lambda _key: False
    runner._session_state = lambda _key: SimpleNamespace(
        turn=SimpleNamespace(agent=None, started_ts=None),
    )
    runner._persist_active_agents = lambda: None
    tasks = []
    runner._retain_background_task = lambda task: (tasks.append(task) or task)
    resumed = AsyncMock()
    runner._run_startup_resume_event = resumed

    assert runner._schedule_resume_pending_sessions() == 1
    await asyncio.gather(*tasks)
    resumed.assert_awaited_once()
    assert resumed.await_args.args[1]._gateway_goal_continuation_recovery is True


def test_timeout_mark_preserves_explicit_goal_reason_after_reopen(restart_world):
    runner, store = restart_world
    entry = store.get_or_create_session(_source("preserve-reason"))
    assert store.mark_resume_pending(entry.session_key, _GOAL_CONTINUATION_RESUME_REASON)
    assert store.mark_resume_pending(entry.session_key, "restart_timeout")

    reopened = SessionStore(runner.config.sessions_dir, runner.config)
    try:
        restored = reopened.lookup_by_session_key(entry.session_key)
        assert restored.resume_pending is True
        assert restored.resume_reason == _GOAL_CONTINUATION_RESUME_REASON
    finally:
        reopened.close_all_db_handles()


def test_goal_recovery_reads_entry_origin_profile_home(restart_world, monkeypatch, tmp_path):
    runner, store = restart_world
    runner.config.multiplex_profiles = True
    secondary_home = tmp_path / "secondary"
    secondary_home.mkdir()
    entry = store.get_or_create_session(_source("secondary-goal"))
    entry.origin.profile = "secondary"
    store._save_entry(entry.session_key, entry_data=entry.to_dict())
    monkeypatch.setattr(runner, "_resolve_profile_home_for_source", lambda _source: secondary_home)

    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from hermes_cli import goals
    token = set_hermes_home_override(str(secondary_home))
    try:
        manager = GoalManager(entry.session_id)
        manager.set("secondary profile goal")
        manager.state.last_verdict = "continue"
        manager._save()
    finally:
        reset_hermes_home_override(token)
        goals._DB_CACHE.clear()

    assert runner._goal_continuation_pending_for_entry(entry)
