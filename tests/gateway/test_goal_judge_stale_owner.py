"""RED coverage for a post-turn goal judge that outlives its session owner.

The post-turn hook owns a cached ``GoalManager`` while the synchronous judge runs in the
gateway executor.  A real lifecycle mutation can therefore replace that manager's state
before the judge returns.  These tests deliberately fail until the production path rejects
the stale judge before it saves, notices, or enqueues anything.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from threading import Event

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_cli import goals


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))

    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    token = set_hermes_home_override(str(home))
    goals._DB_CACHE.clear()
    goals._get_session_db()  # use the real SessionDB before the async hook starts
    yield home
    goals._DB_CACHE.clear()
    reset_hermes_home_override(token)


class _NoticeAndFifoCapture:
    def __init__(self):
        self._pending_messages = {}
        self.notices = []

    async def send(self, chat_id, content, **_kwargs):
        self.notices.append((chat_id, content))
        return SimpleNamespace(success=True)


def _runner(adapter, session_key):
    """A concrete GatewayRunner MRO with only transport lookup supplied by the test."""
    runner = object.__new__(GatewayRunner)
    runner._adapter_for_source = lambda _source: adapter
    runner._session_key_for_source = lambda _source: session_key
    runner.config = {}
    runner._sessions = {}
    return runner


def _source():
    return SessionSource(
        platform=Platform.DISCORD,
        chat_id="goal-race-chat",
        chat_type="channel",
        user_id="goal-race-user",
    )


async def _run_paused_judge(runner, session_entry, source, judge, mutate):
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(goals, "judge_goal", judge)
        task = asyncio.create_task(
            runner._post_turn_goal_continuation(
                session_entry=session_entry, source=source, final_response="old turn",
            )
        )
        await asyncio.wait_for(asyncio.to_thread(judge.entered.wait), timeout=2)
        mutate()
        judge.release.set()
        await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_clear_during_paused_judge_stays_cleared_and_is_silent(hermes_home):
    session_id = "goal-stale-clear"
    session_key = "agent:main:discord:goal-race-clear"
    manager = goals.GoalManager(session_id)
    manager.set("finish the old task")

    judge = _BlockingJudge(("done", "old task is done", False, None, False))
    adapter = _NoticeAndFifoCapture()
    runner = _runner(adapter, session_key)

    await _run_paused_judge(
        runner,
        SimpleNamespace(session_id=session_id),
        _source(),
        judge,
        lambda: goals.clear_goal(session_id),
    )

    state = goals.GoalManager(session_id).state
    assert state is not None and state.status == "cleared"
    assert adapter.notices == []
    assert adapter._pending_messages == {}


@pytest.mark.asyncio
async def test_replacement_during_paused_judge_keeps_replacement_and_is_silent(hermes_home):
    session_id = "goal-stale-replace"
    session_key = "agent:main:discord:goal-race-replace"
    manager = goals.GoalManager(session_id)
    manager.set("finish the old task")

    judge = _BlockingJudge(("continue", "old task needs more work", False, None, False))
    adapter = _NoticeAndFifoCapture()
    runner = _runner(adapter, session_key)

    def replace_goal():
        goals.clear_goal(session_id)
        goals.GoalManager(session_id).set("finish the replacement task")

    await _run_paused_judge(
        runner,
        SimpleNamespace(session_id=session_id),
        _source(),
        judge,
        replace_goal,
    )

    state = goals.GoalManager(session_id).state
    assert state is not None
    assert state.status == "active"
    assert state.goal == "finish the replacement task"
    assert state.turns_used == 0
    assert adapter.notices == []
    assert adapter._pending_messages == {}


@pytest.mark.asyncio
async def test_generation_invalidation_rejects_late_executor_result(hermes_home):
    session_id = "goal-stale-generation"
    session_key = "agent:main:discord:goal-race-generation"
    manager = goals.GoalManager(session_id)
    manager.set("finish the old task")

    judge = _BlockingJudge(("done", "old task is done", False, None, False))
    adapter = _NoticeAndFifoCapture()
    adapter._active_sessions = {session_key: SimpleNamespace(_hermes_run_generation=7)}
    runner = _runner(adapter, session_key)
    current = True
    runner._is_session_run_current = lambda key, generation: current and key == session_key and generation == 7

    async def run_in_executor(fn):
        return await asyncio.to_thread(fn)

    runner._run_in_executor_with_context = run_in_executor
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(goals, "judge_goal", judge)

    task = asyncio.create_task(
        runner._post_turn_goal_continuation(
            session_entry=SimpleNamespace(session_id=session_id),
            source=_source(),
            final_response="old turn",
        )
    )
    for _ in range(200):
        if judge.entered.is_set():
            break
        await asyncio.sleep(0.01)
    assert judge.entered.is_set()
    current = False
    judge.release.set()
    await asyncio.wait_for(task, timeout=5)

    state = goals.GoalManager(session_id).state
    assert state is not None and state.status == "active"
    assert state.turns_used == 0
    assert adapter.notices == []
    assert adapter._pending_messages == {}
    monkeypatch.undo()


class _BlockingJudge:
    def __init__(self, result):
        self.entered = Event()
        self.release = Event()
        self.result = result

    def __call__(self, *_args, **_kwargs):
        self.entered.set()
        if not self.release.wait(timeout=5):
            raise AssertionError("judge barrier was not released")
        return self.result
