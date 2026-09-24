"""Old callback/finalizer cleanup must not cancel an overlapping successor."""
import asyncio
import threading
from types import SimpleNamespace

import pytest

from gateway.platforms.base import SendResult
from gateway.run_turn_runner import TurnRunner
from gateway.turn_context import TurnContext
from tools import clarify_gateway as cm


def _runner(loop, adapter, key):
    runner = TurnRunner(None, TurnContext(
        session_key=key, session_id="session", message="question",
        _status_adapter=adapter, _status_chat_id="chat", _loop_for_step=loop,
    ))
    runner._native_image_run_message = lambda: "question"
    return runner


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [None, ("session", "old")])
@pytest.mark.parametrize("declined", [False, True])
async def test_failed_old_callback_leaves_successor_waiter_alive(monkeypatch, owner, declined):
    key = "overlapping-send"
    registered = asyncio.Event()
    entries = {}
    monkeypatch.setattr(cm, "get_clarify_timeout", lambda: 5)

    class Adapter:
        async def send_clarify(self, **kwargs):
            with cm._lock:
                entries[kwargs["question"]] = cm._entries[kwargs["clarify_id"]]
            if kwargs["question"] == "new":
                registered.set()
                return SendResult(success=True)
            return SendResult(success=False, raw_response={"code": "egress_declined"} if declined else None)

    loop = asyncio.get_running_loop()
    old, new = (_runner(loop, Adapter(), key) for _ in range(2))
    captured_old = old._clarify_callback_sync
    new_task = asyncio.create_task(asyncio.to_thread(
        new._clarify_callback_sync, "new", None, turn_owner=("session", "new"),
    ))
    try:
        await asyncio.wait_for(registered.wait(), 5)
        result = await asyncio.wait_for(asyncio.to_thread(captured_old, "old", None, turn_owner=owner), 5)
        assert "could not be delivered" in result
        assert entries["old"].event.is_set()
        assert entries["old"].response == ""
        assert not cm.resolve_gateway_clarify(entries["old"].clarify_id, "late")
        assert not entries["new"].event.is_set(), "old send cancelled successor"
        assert cm.resolve_text_response_for_session(key, "new answer")
        assert await asyncio.wait_for(new_task, 5) == "new answer"
    finally:
        cm.clear_session(key)
        await asyncio.wait_for(new_task, 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [None, ("session", "old")])
@pytest.mark.parametrize("answered", [False, True])
async def test_old_run_finalizer_cleans_only_its_registrations(monkeypatch, owner, answered):
    key = "overlapping-finalizers"
    entries = {}
    old_ready, new_ready = asyncio.Event(), asyncio.Event()
    release_old = threading.Event()
    monkeypatch.setattr(cm, "get_clarify_timeout", lambda: 5)
    loop = asyncio.get_running_loop()

    class Adapter:
        async def send_clarify(self, **kwargs):
            with cm._lock:
                entries[kwargs["question"]] = cm._entries[kwargs["clarify_id"]]
            new_ready.set()
            return SendResult(success=True)

    old, new = (_runner(loop, Adapter(), key) for _ in range(2))

    def old_schedule(coro, _message):
        coro.close()
        entry = cm.get_pending_for_session(key)
        entries["old"] = entry
        if answered:
            assert cm.resolve_gateway_clarify(entry.clarify_id, "old answer")
        raise RuntimeError("send scheduling failed after registration")

    old._schedule = old_schedule

    def old_conversation(*args, **kwargs):
        with pytest.raises(RuntimeError, match="send scheduling failed"):
            old._clarify_callback_sync("old", None, turn_owner=owner)
        loop.call_soon_threadsafe(old_ready.set)
        assert release_old.wait(5)
        return "old done"

    def new_conversation(*args, **kwargs):
        return new._clarify_callback_sync("new", None, turn_owner=("session", "new"))

    async def run(runner, conversation):
        return await asyncio.to_thread(
            runner._run_conversation_with_approval,
            SimpleNamespace(run_conversation=conversation), [], None, None, None,
        )

    old_task = asyncio.create_task(run(old, old_conversation))
    new_task = None
    try:
        await asyncio.wait_for(old_ready.wait(), 5)
        new_task = asyncio.create_task(run(new, new_conversation))
        await asyncio.wait_for(new_ready.wait(), 5)
        release_old.set()
        assert await asyncio.wait_for(old_task, 5) == "old done"
        assert entries["old"].event.is_set()
        assert entries["old"].response == ("old answer" if answered else "")
        assert not cm.resolve_gateway_clarify(entries["old"].clarify_id, "late")
        assert not entries["new"].event.is_set(), "old finalizer cancelled successor"
        # Legacy owner=None is not permission to claim every ownerless registration.
        late = await asyncio.wait_for(asyncio.to_thread(old._clarify_callback_sync, "late old", None), 5)
        assert late.startswith("[")
        assert cm.get_pending_for_session(key) is entries["new"]
        # Explicit session cancellation must STILL cancel the successor and reject late replies.
        assert cm.clear_session(key) == 1
        assert not cm.resolve_gateway_clarify(entries["new"].clarify_id, "after stop")
        assert (await asyncio.wait_for(new_task, 5)).startswith("[")
        assert not cm.has_pending(key)
    finally:
        release_old.set()
        cm.clear_session(key)
        await asyncio.wait_for(old_task, 5)
        if new_task is not None:
            await asyncio.wait_for(new_task, 5)
