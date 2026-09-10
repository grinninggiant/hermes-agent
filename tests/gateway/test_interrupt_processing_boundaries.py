"""Contracts for the public interrupt and recursive internal-turn boundaries."""

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key


def _source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="chat-1",
        chat_type="dm",
        user_id="user-1",
    )


@pytest.mark.asyncio
async def test_public_interrupt_session_processing_uses_native_session_funnel():
    source = _source()
    session_key = build_session_key(source)
    runner = object.__new__(GatewayRunner)
    runner._session_key_for_source = lambda value: session_key
    runner.session_store = object()
    runner._async_session_store = AsyncMock()
    runner._async_session_store._store = runner.session_store
    runner._async_session_store.lookup_by_session_key.return_value = SimpleNamespace(
        session_key=session_key,
        session_id="session-1",
    )
    runner._interrupt_and_clear_session = AsyncMock()

    assert await runner.interrupt_session_processing(
        source, reason="platform_stop", expected_session_id="session-1",
    ) is True
    runner._interrupt_and_clear_session.assert_awaited_once_with(
        session_key,
        source,
        interrupt_reason="platform_stop",
        invalidation_reason="platform_stop",
    )


@pytest.mark.asyncio
async def test_public_interrupt_session_processing_rejects_stale_expected_session():
    source = _source()
    session_key = build_session_key(source)
    runner = object.__new__(GatewayRunner)
    runner._session_key_for_source = lambda _source: session_key
    runner.session_store = object()
    runner._async_session_store = AsyncMock()
    runner._async_session_store._store = runner.session_store
    runner._async_session_store.lookup_by_session_key.return_value = SimpleNamespace(
        session_id="new-session",
    )
    runner._interrupt_and_clear_session = AsyncMock()

    assert await runner.interrupt_session_processing(
        source, expected_session_id="old-session",
    ) is False
    runner._interrupt_and_clear_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_recursive_internal_turn_is_rejected_before_worker_when_veto_flips():
    source = _source()
    event = MessageEvent(text="wake", source=source, internal=True)
    runner = object.__new__(GatewayRunner)
    runner._profile_scope_for_source = lambda _source: nullcontext()
    runner._adapter_for_source = lambda _source: type(
        "Adapter", (), {"_internal_execution_allowed": AsyncMock(return_value=False)}
    )()
    runner._run_agent_inner = AsyncMock(side_effect=AssertionError("worker launched"))

    result = await runner._run_agent(
        message="wake", context_prompt="", history=[], source=source,
        session_id="session-1", session_key=build_session_key(source),
        gateway_event=event,
    )

    assert result["interrupted"] is True
    runner._run_agent_inner.assert_not_awaited()


@pytest.mark.asyncio
async def test_recursive_internal_turn_is_rejected_for_mismatched_pinned_session():
    source = _source()
    session_key = build_session_key(source)
    event = MessageEvent(text="wake", source=source, internal=True)
    event.metadata.update({
        "gateway_session_strict": True,
        "gateway_session_key": session_key,
        "gateway_session_id": "old-session",
    })
    runner = object.__new__(GatewayRunner)
    runner._profile_scope_for_source = lambda _source: nullcontext()
    runner._adapter_for_source = lambda _source: type(
        "Adapter", (), {"_internal_execution_allowed": AsyncMock(return_value=True)}
    )()
    runner._session_key_for_source = lambda _source: session_key
    runner.session_store = object()
    runner._async_session_store = AsyncMock()
    runner._async_session_store._store = runner.session_store
    runner._async_session_store.lookup_by_session_key.return_value = SimpleNamespace(
        session_id="new-session",
    )
    runner._run_agent_inner = AsyncMock(side_effect=AssertionError("worker launched"))

    result = await runner._run_agent(
        message="wake", context_prompt="", history=[], source=source,
        session_id="old-session", session_key=session_key, gateway_event=event,
    )

    assert result["interrupted"] is True
    assert event.metadata["gateway_session_rejected"] == "strict_session_identity_mismatch"
    runner._run_agent_inner.assert_not_awaited()
