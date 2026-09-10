"""Real SQLite ordering regressions for goal-judge ownership CAS."""

from __future__ import annotations

from pathlib import Path
from threading import Event, Thread
from unittest.mock import patch

import pytest

from hermes_cli import goals
from hermes_state import SessionDB


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    goals._DB_CACHE.clear()
    yield home
    goals._DB_CACHE.clear()


def _hold_write_lock(db: SessionDB, entered: Event, release: Event) -> Thread:
    def hold(conn):
        entered.set()
        assert release.wait(timeout=5)

    thread = Thread(target=lambda: db._execute_write(hold), daemon=True)
    thread.start()
    assert entered.wait(timeout=5)
    return thread


def test_owner_invalidation_while_cas_waits_drops_stale_verdict(hermes_home):
    session_id = "goal-cas-lock-race"
    manager = goals.GoalManager(session_id)
    manager.set("finish the old task")
    db = goals._get_session_db()
    assert db is not None
    blocker = SessionDB(db_path=db.db_path)
    lock_entered, release_lock = Event(), Event()
    holder = _hold_write_lock(blocker, lock_entered, release_lock)
    cas_started = Event()
    original_execute_write = db._execute_write

    def observe_cas(fn, patience_s=None):
        cas_started.set()
        return original_execute_write(fn, patience_s=patience_s)

    db._execute_write = observe_cas
    owner_is_current = True

    def owner_check():
        return owner_is_current

    try:
        with patch.object(goals, "judge_goal", return_value=("done", "old task is done", False, None, False)):
            result = {}
            worker = Thread(
                target=lambda: result.setdefault(
                    "decision", manager.evaluate_after_turn("late response", owner_check=owner_check)
                ),
                daemon=True,
            )
            worker.start()
            assert cas_started.wait(timeout=5)
            owner_is_current = False
            release_lock.set()
            worker.join(timeout=5)
            assert not worker.is_alive()
    finally:
        release_lock.set()
        holder.join(timeout=5)
        blocker.close()

    decision = result["decision"]
    assert decision["stale_owner"] is True
    assert decision["message"] == ""
    persisted = goals.GoalManager(session_id).state
    assert persisted is not None
    assert persisted.status == "active"
    assert persisted.turns_used == 0


def test_owner_condition_allows_current_judge_cas(hermes_home):
    manager = goals.GoalManager("goal-cas-success")
    manager.set("finish the task")

    with patch.object(goals, "judge_goal", return_value=("done", "finished", False, None, False)):
        decision = manager.evaluate_after_turn("complete", owner_check=lambda: True)

    assert decision["verdict"] == "done"
    assert goals.GoalManager("goal-cas-success").state.status == "done"


def test_owner_condition_errors_fail_closed(hermes_home):
    manager = goals.GoalManager("goal-cas-condition-error")
    manager.set("finish the task")
    db = goals._get_session_db()
    assert db is not None
    raw = manager.state.to_json()

    assert not db.set_meta_if_equals(
        "goal:goal-cas-condition-error", raw, '{"status":"done"}',
        condition=lambda: (_ for _ in ()).throw(RuntimeError("owner check failed")),
    )
    assert goals.GoalManager("goal-cas-condition-error").state.status == "active"
