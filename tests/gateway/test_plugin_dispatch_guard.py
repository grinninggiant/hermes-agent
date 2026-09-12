"""Guarded internal work must be revalidated when it actually reaches ingress."""
import pytest
from gateway.run import GatewayRunner
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource
from gateway.config import Platform


def event():
    return MessageEvent(
        text="authorized continuation", message_type=MessageType.TEXT,
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="42", user_id="42", chat_type="dm"),
        internal=True, allow_gateway_control=False,
        metadata={"hermes_plugin_guard_required": True},
    )


@pytest.mark.asyncio
async def test_missing_guard_after_event_restore_fails_closed():
    runner = object.__new__(GatewayRunner)
    assert await runner._hm_admit_event(event()) is None


@pytest.mark.asyncio
async def test_guard_failure_drops_work_instead_of_crashing_dispatch():
    runner = object.__new__(GatewayRunner)
    message = event()
    def unavailable():
        raise OSError("intent store unavailable")
    setattr(message, "_hermes_plugin_dispatch_guard", unavailable)
    assert await runner._hm_admit_event(message) is None


@pytest.mark.asyncio
async def test_guard_is_rechecked_when_queued_work_reaches_ingress():
    runner = object.__new__(GatewayRunner)
    message = event()
    state = {"allowed": True}
    setattr(message, "_hermes_plugin_dispatch_guard", lambda: state["allowed"])
    assert await runner._hm_admit_event(message) is not None
    state["allowed"] = False
    assert await runner._hm_admit_event(message) is None
