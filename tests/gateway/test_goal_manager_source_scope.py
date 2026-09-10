import inspect
from pathlib import Path
import sqlite3

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.session import AsyncSessionStore, SessionSource, SessionStore
from hermes_constants import get_hermes_home
from hermes_cli.goals import GoalContract


@pytest.fixture
def scoped_runner(tmp_path, monkeypatch):
    import hermes_state
    from gateway.run import GatewayRunner
    from hermes_cli import goals

    root = tmp_path / "default"
    alpha = root / "profiles" / "alpha"
    beta = root / "profiles" / "beta"
    for home in (root, alpha, beta):
        home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", hermes_state._IMPORT_DEFAULT_DB_PATH)
    goals._DB_CACHE.clear()

    config = GatewayConfig(multiplex_profiles=True)
    config.goals = {"max_turns": 7}
    store = SessionStore(sessions_dir=root / "sessions", config=config)
    runner = object.__new__(GatewayRunner)
    runner.config = config
    runner.session_store = store
    runner._async_session_store = AsyncSessionStore(store)
    homes = {"alpha": alpha, "beta": beta}
    runner._resolve_profile_home_for_source = lambda source: homes[source.profile]

    yield runner, root, alpha, beta

    goals._DB_CACHE.clear()
    store.close_all_db_handles()


def _source(profile, chat_id):
    return SessionSource(
        platform=Platform.TELEGRAM,
        profile=profile,
        chat_id=chat_id,
        user_id="user-1",
        chat_type="dm",
    )


def _goal_values(db_path):
    with sqlite3.connect(db_path) as conn:
        return {
            value for key, value in conn.execute(
                "SELECT key, value FROM state_meta WHERE key LIKE 'goal:%'"
            )
        }


@pytest.mark.asyncio
async def test_source_goal_operations_isolate_profiles_and_ensure_once(scoped_runner):
    runner, root, alpha, beta = scoped_runner
    alpha_source = _source("alpha", "chat-alpha")
    beta_source = _source("beta", "chat-beta")
    alpha_contract = GoalContract(outcome="alpha shipped", verification="alpha tests pass")
    recovery_session_id = "same-session-id-in-each-profile"

    alpha_state = await runner.ensure_goal_for_source(
        alpha_source, "alpha goal", contract=alpha_contract,
        session_id=recovery_session_id,
    )
    beta_state = await runner.ensure_goal_for_source(
        beta_source, "beta goal", contract=GoalContract(outcome="beta shipped"),
        session_id=recovery_session_id,
    )
    unchanged = await runner.ensure_goal_for_source(
        alpha_source, "replacement", contract=GoalContract(outcome="wrong"),
        session_id=recovery_session_id,
    )

    assert alpha_state.goal == unchanged.goal == "alpha goal"
    assert alpha_state.contract == unchanged.contract == alpha_contract
    assert alpha_state.max_turns == beta_state.max_turns == 7
    assert (await runner.goal_state_for_source(
        alpha_source, session_id=recovery_session_id,
    )).goal == "alpha goal"
    assert (await runner.goal_state_for_source(
        beta_source, session_id=recovery_session_id,
    )).goal == "beta goal"

    # Returned states are detached snapshots, not live manager-owned state.
    alpha_state.goal = "mutated outside scope"
    alpha_state.contract.outcome = "mutated contract"
    reread = await runner.goal_state_for_source(
        alpha_source, session_id=recovery_session_id,
    )
    assert reread.goal == "alpha goal"
    assert reread.contract.outcome == "alpha shipped"

    assert Path(get_hermes_home()) == root
    assert any("alpha goal" in value for value in _goal_values(alpha / "state.db"))
    assert any("beta goal" in value for value in _goal_values(beta / "state.db"))
    assert not _goal_values(root / "state.db")


@pytest.mark.asyncio
async def test_explicit_session_recovery_reads_resumes_and_never_creates_session(
    scoped_runner, monkeypatch,
):
    runner, _root, _alpha, _beta = scoped_runner
    source = _source("alpha", "chat-alpha")
    created = await runner.ensure_goal_for_source(
        source, "recover goal", contract=GoalContract(verification="tests pass"),
    )
    session_id = (await runner.async_session_store.lookup_by_session_key(
        runner._session_key_for_source(source)
    )).session_id

    from hermes_cli.goals import GoalManager

    with runner._profile_scope_for_source(source):
        manager = GoalManager(session_id)
        manager.state.turns_used = 4
        manager._save()
        manager.pause("restart")

    async def fail_if_created(*_args, **_kwargs):
        raise AssertionError("explicit recovery must not create or rebind a session")

    monkeypatch.setattr(runner.async_session_store, "get_or_create_session", fail_if_created)
    monkeypatch.setattr(
        GoalManager, "evaluate_after_turn",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("judge must not run")),
    )
    runner._queue_goal_continuation = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("normal continuation must not be enqueued")
    )

    ensured = await runner.ensure_goal_for_source(
        source,
        "must not replace",
        contract=GoalContract(outcome="must not replace"),
        session_id=session_id,
    )
    paused = await runner.goal_state_for_source(source, session_id=session_id)
    prompt = await runner.next_goal_continuation_prompt_for_source(source, session_id=session_id)
    kept = await runner.resume_goal_for_source(
        source, session_id=session_id, reset_budget=False,
    )
    reset = await runner.resume_goal_for_source(
        source, session_id=session_id, reset_budget=True,
    )

    assert created.max_turns == ensured.max_turns == paused.max_turns == 7
    assert ensured.goal == "recover goal"
    assert paused.status == "paused" and paused.turns_used == 4
    assert prompt is None
    assert kept.state.status == "active" and kept.state.turns_used == 4
    assert "recover goal" in kept.continuation_prompt
    assert "tests pass" in kept.continuation_prompt
    assert reset.state.turns_used == 0
    assert reset.continuation_prompt == kept.continuation_prompt

    with pytest.raises(ValueError, match="non-empty"):
        await runner.goal_state_for_source(source, session_id="  ")


def test_source_goal_api_has_no_callback_or_manager_escape_hatch():
    from gateway.run import GatewayRunner

    assert not hasattr(GatewayRunner, "with_goal_manager_for_source")
    public_operations = {
        "ensure_goal_for_source",
        "goal_state_for_source",
        "resume_goal_for_source",
        "next_goal_continuation_prompt_for_source",
    }
    for name in public_operations:
        signature = inspect.signature(getattr(GatewayRunner, name))
        assert "callback" not in signature.parameters
        assert "operation" not in signature.parameters
        assert "GoalManager" not in str(signature)
