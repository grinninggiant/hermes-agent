"""Generation ownership contracts for inbound post-turn cleanup."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.run import GatewayRunner
from gateway.run_shutdown import GatewayShutdownMixin
from gateway.session import SessionSource
from gateway.turn_lease import SessionTurnLeaseRegistry
from hermes_cli.active_sessions import try_acquire_active_session
from hermes_constants import get_hermes_home


@pytest.fixture(autouse=True)
def _isolated_hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))


def _runner() -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    runner._sessions = {}
    runner._turn_leases = SessionTurnLeaseRegistry()
    runner._post_turn_work_owners = {}
    runner._persist_active_agents = MagicMock()
    return runner


def _source() -> SessionSource:
    return SessionSource(platform=Platform.TELEGRAM, chat_id="chat", chat_type="dm", user_id="user")


def _active_lease(key: str):
    lease, refusal = try_acquire_active_session(
        session_id=key,
        surface="gateway:telegram",
        config={},
        metadata={"live_session_id": key},
    )
    assert refusal is None
    assert lease is not None
    return lease


def _active_entries():
    return json.loads((get_hermes_home() / "runtime" / "active_sessions.json").read_text())["entries"]


@pytest.mark.asyncio
async def test_stale_post_turn_cleanup_preserves_replacement_slot_and_lease():
    """A boundary advances the real generation before N's late cleanup runs."""
    runner = _runner()
    key = "telegram:chat"
    state = runner._session_state(key)

    old_generation = runner._begin_session_run_generation(key)
    old_slot = _active_lease(key)
    old_token = await runner._turn_leases.acquire(
        "transcript", owner_key=key, generation=old_generation
    )
    state.turn.agent = object()
    state.turn.lease = old_slot
    state.turn.lease_token = old_token
    state.turn.lease_generation = old_generation

    # This is the real boundary handoff: release N's resources, then admit N+1.
    old_slot.release()
    assert runner._turn_leases.release(old_token)
    state.turn.lease = None
    state.turn.lease_token = None
    state.turn.lease_generation = None
    new_generation = runner._begin_session_run_generation(key)
    assert new_generation == old_generation + 1
    new_slot = _active_lease(key)
    new_token = await runner._turn_leases.acquire(
        "transcript", owner_key=key, generation=new_generation
    )
    state.turn.agent = object()
    state.turn.lease = new_slot
    state.turn.lease_token = new_token
    state.turn.lease_generation = new_generation

    assert runner._release_running_agent_state(key, run_generation=old_generation) is False
    assert runner._release_turn_lease(key, old_generation) is False
    assert state.turn.agent is not None
    assert state.turn.lease is new_slot
    assert state.turn.lease_token is new_token
    assert not new_token.released
    assert len(_active_entries()) == 1
    assert _active_entries()[0]["lease_id"] == new_slot.lease_id

    assert runner._release_running_agent_state(key, run_generation=new_generation)
    assert runner._release_turn_lease(key, new_generation)
    assert _active_entries() == []


async def _run_turn_that_owns_a_lease(runner, event, started=None, wait=False):
    key = runner._session_key_for_source(event.source)
    generation = runner._session_state(key).persistent.run_generation
    token = await runner._turn_leases.acquire(
        "transcript", owner_key=key, generation=generation
    )
    state = runner._session_state(key)
    state.turn.lease_token = token
    state.turn.lease_generation = generation
    if started is not None:
        started.set()
    if wait:
        await asyncio.sleep(10)
    raise RuntimeError("turn failed")


def _wire_inbound_runner(runner, *, wait=False):
    source = _source()
    event = SimpleNamespace(source=source, text="turn", internal=False)
    key = runner._session_key_for_source(source)
    started = asyncio.Event()
    runner._hm_admit_event = AsyncMock(return_value=(event, source, False))
    runner._hm_estop_gate = MagicMock(return_value=None)
    runner._hm_pending_reply_intercepts = AsyncMock(return_value=None)
    runner._hm_evict_idle_stale_agent = MagicMock()
    runner._hm_evict_reaped_agent = MagicMock()
    runner._is_session_running = MagicMock(return_value=False)
    runner._hm_dispatch_idle_commands = AsyncMock(return_value=(False, None))
    runner._claim_active_session_slot = MagicMock(return_value=(_active_lease(key), None))
    runner._clear_durable_active_turn = AsyncMock()
    runner._restore_moa_one_shot = MagicMock()
    runner._restore_pending_one_turn_model_override = MagicMock()
    runner._run_post_turn_hooks = AsyncMock()
    runner._handle_message_with_agent = lambda *_args: _run_turn_that_owns_a_lease(
        runner, event, started, wait
    )
    return event, key, started


@pytest.mark.asyncio
async def test_post_turn_owner_releases_real_slot_and_turn_lease_on_exception():
    runner = _runner()
    event, key, _started = _wire_inbound_runner(runner)

    with pytest.raises(RuntimeError, match="turn failed"):
        await runner._handle_message(event)

    state = runner._session_state(key)
    assert state.turn.agent is None
    assert state.turn.lease_token is None
    assert runner._post_turn_work_owners == {}
    assert _active_entries() == []
    assert runner._turn_leases._leases["transcript"].holder is None


@pytest.mark.asyncio
async def test_post_turn_owner_releases_real_slot_and_turn_lease_on_cancellation():
    runner = _runner()
    event, key, started = _wire_inbound_runner(runner, wait=True)
    task = asyncio.create_task(runner._handle_message(event))
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    state = runner._session_state(key)
    assert state.turn.agent is None
    assert state.turn.lease_token is None
    assert runner._post_turn_work_owners == {}
    assert _active_entries() == []
    assert runner._turn_leases._leases["transcript"].holder is None


def test_post_turn_owner_count_has_one_entry_per_live_owner():
    runner = _runner()
    runner._running_agents = {"telegram:chat": object()}
    runner._session_state("telegram:chat").persistent.run_generation = 2
    owner = object()
    runner._post_turn_work_owners[id(owner)] = (owner, "telegram:chat", 2)
    assert GatewayShutdownMixin._active_post_turn_work_count(runner) == 0

    old_owner = object()
    runner._post_turn_work_owners[id(old_owner)] = (old_owner, "telegram:chat", 1)
    assert GatewayShutdownMixin._active_post_turn_work_count(runner) == 1
