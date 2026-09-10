"""Real SessionDB regressions for goal-judge conditional persistence."""

from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_cli import goals


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    goals._DB_CACHE.clear()
    yield home
    goals._DB_CACHE.clear()


def test_expired_wait_cas_cannot_clear_a_newer_goal_owner(hermes_home):
    manager = goals.GoalManager("cas-expired-wait")
    manager.set("old goal")
    manager.wait_for_seconds(60, reason="old wait")

    goals.clear_goal("cas-expired-wait")
    manager.state.waiting_until = goals.time.time() - 1

    with patch.object(goals, "judge_goal") as judge:
        decision = manager.evaluate_after_turn("late response")

    assert decision["stale_owner"] is True
    judge.assert_not_called()
    assert goals.GoalManager("cas-expired-wait").state.status == "cleared"


def test_gate_budget_second_save_remains_conditional(hermes_home):
    manager = goals.GoalManager("cas-gate-budget", default_max_turns=1)
    manager.set("old goal")
    manager.add_gate("false")
    db = goals._get_session_db()
    original = db.set_meta_if_equals
    calls = 0

    def replace_after_first_cas(key, expected, replacement):
        nonlocal calls
        calls += 1
        saved = original(key, expected, replacement)
        if calls == 1 and saved:
            goals.GoalManager("cas-gate-budget").set("replacement goal")
        return saved

    with patch.object(db, "set_meta_if_equals", side_effect=replace_after_first_cas), \
         patch.object(goals, "workspace_fingerprint", return_value=""):
        decision = manager.evaluate_after_turn("late response")

    assert decision["stale_owner"] is True
    assert goals.GoalManager("cas-gate-budget").state.goal == "replacement goal"
    assert calls == 2
