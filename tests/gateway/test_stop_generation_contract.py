"""Worker admission and Stop cleanup must stay with the originating generation."""

import asyncio
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.run import GatewayRunner
from gateway.session import build_session_key
from tests.gateway.test_platform_continuation_seam import _ContinuationAdapter, _event


@pytest.mark.asyncio
@pytest.mark.parametrize("internal", [False, True])
@pytest.mark.parametrize("boundary", [
    "lookup_veto", "lookup_generation", "gate_generation", "gate_error", "proxy_typing",
])
async def test_worker_admission_rechecks_policy_and_generation_after_awaits(internal, boundary, monkeypatch):
    event = _event(internal=internal)
    key = build_session_key(event.source)
    event.metadata.update(gateway_session_strict=True, gateway_session_key=key,
                          gateway_session_id="session-1")
    adapter = _ContinuationAdapter()
    runner = object.__new__(GatewayRunner)
    runner._profile_scope_for_source = lambda _source: nullcontext()
    runner._delivery_adapter_for = lambda _source: adapter
    runner._session_key_for_source = lambda _source: key
    runner.session_store = object()
    generation = runner._begin_session_run_generation(key)
    entered, release = asyncio.Event(), asyncio.Event()
    gate_open = True
    looked_up = False

    async def lookup(_key):
        nonlocal looked_up
        if boundary.startswith("lookup") or boundary == "gate_error":
            entered.set()
            await asyncio.wait_for(release.wait(), 2)
        looked_up = True
        return SimpleNamespace(session_id="session-1")

    async def allowed(_event):
        if boundary == "gate_error" and looked_up:
            raise RuntimeError("gate unavailable")
        if boundary == "gate_generation" and looked_up:
            entered.set()
            await asyncio.wait_for(release.wait(), 2)
        return gate_open

    adapter.allow_execution = allowed
    runner._async_session_store = SimpleNamespace(
        _store=runner.session_store, lookup_by_session_key=lookup,
    )
    runner._run_agent_inner = AsyncMock(return_value={"completed": True})
    worker = runner._run_agent_inner
    if boundary == "proxy_typing":
        async def send_typing(*_args, **_kwargs):
            entered.set()
            await asyncio.wait_for(release.wait(), 2)

        del runner._run_agent_inner
        runner._get_proxy_url = lambda: "http://127.0.0.1:1"
        runner._proxy_stream_consumer = lambda *_args: None
        adapter.send_typing = send_typing
        worker = Mock(side_effect=AssertionError("proxy dispatched"))
        monkeypatch.setattr("aiohttp.ClientSession", worker)
    task = asyncio.create_task(runner._run_agent(
        event.text, "", [], event.source, "session-1", gateway_event=event,
        session_key=key, run_generation=generation,
    ))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if boundary in {"lookup_veto", "proxy_typing"}:
            gate_open = False
        elif "generation" in boundary:
            runner._begin_session_run_generation(key)
        release.set()
        result = await asyncio.wait_for(task, 2)
        assert result["interrupted"] is True
        if boundary == "gate_error":
            assert result["turn_exit_reason"].endswith("execution_rejected")
        worker.assert_not_called()
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    # An internal-only policy must not silently become a policy for human input.
    del adapter.allow_execution
    runner._run_agent_inner = AsyncMock(return_value={"completed": True})
    adapter.allow_internal_execution = AsyncMock(return_value=False)
    external = _event()
    result = await runner._run_agent(
        external.text, "", [], external.source, "session-1", gateway_event=external,
        session_key=key,
    )
    assert result["completed"] is True
    adapter.allow_internal_execution.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["lookup", "cleanup", "stale_receipt", "current_receipt"])
async def test_targeted_interrupt_cannot_signal_or_clean_successor(boundary):
    adapter = _ContinuationAdapter()
    source = _event().source
    key = build_session_key(source)
    runner = object.__new__(GatewayRunner)
    runner._session_key_for_source = lambda _source: key
    runner._delivery_adapter_for = lambda _source: adapter
    runner._persist_active_agents = lambda: None
    runner.session_store = object()
    runner._agent_cache = {}
    state = runner._session_state(key)
    old = SimpleNamespace(hard_interrupt=Mock())
    successor = SimpleNamespace(hard_interrupt=Mock())
    state.turn.agent = old
    generation = runner._begin_session_run_generation(key)
    entered, release = asyncio.Event(), asyncio.Event()
    adapter._active_sessions[key] = asyncio.Event()

    async def lookup(_key):
        if boundary == "lookup":
            entered.set()
            await asyncio.wait_for(release.wait(), 2)
        return SimpleNamespace(session_id="session-1")

    async def stop_typing(*_args):
        if boundary == "cleanup":
            entered.set()
            await asyncio.wait_for(release.wait(), 2)

    adapter._stop_typing_quietly = stop_typing
    runner._async_session_store = SimpleNamespace(
        _store=runner.session_store, lookup_by_session_key=lookup,
    )

    def replace_turn():
        state.turn.agent = successor
        new_generation = runner._begin_session_run_generation(key)
        state.persistent.pending_command_text = "successor command"
        runner._agent_cache[key] = (successor, "signature")
        adapter._pending_messages[key] = _event()
        adapter._active_sessions[key] = asyncio.Event()
        return new_generation

    if boundary == "stale_receipt":
        successor_generation = replace_turn()
    kwargs = {"expected_run_generation": generation} if boundary.endswith("receipt") else {}
    task = asyncio.create_task(runner.interrupt_session_processing(
        source, expected_session_id="session-1", **kwargs,
    ))
    try:
        if boundary in {"lookup", "cleanup"}:
            await asyncio.wait_for(entered.wait(), 2)
            successor_generation = replace_turn()
            release.set()
        accepted = await asyncio.wait_for(task, 2)
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    if boundary == "current_receipt":
        assert accepted is True
        old.hard_interrupt.assert_called_once()
        assert state.turn.agent is None
        assert key not in runner._agent_cache
        assert adapter._active_sessions[key].is_set()
        return
    assert accepted is (boundary == "cleanup")
    assert old.hard_interrupt.called is (boundary == "cleanup")
    successor.hard_interrupt.assert_not_called()
    assert runner._is_session_run_current(key, successor_generation)
    assert state.turn.agent is successor
    assert state.persistent.pending_command_text == "successor command"
    assert runner._agent_cache[key][0] is successor
    assert key in adapter._pending_messages
    assert not adapter._active_sessions[key].is_set()
