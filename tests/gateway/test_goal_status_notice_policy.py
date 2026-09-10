from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import GoalStatusNotice, GoalStatusNoticeKind
from gateway.session import SessionSource


class _NoticeAdapter:
    def __init__(self, decision):
        self.decision = decision
        self.notices = []
        self.sends = []
        self._pending_messages = {}

    async def prepare_goal_status_notice(self, source, notice):
        self.notices.append((source, notice))
        if isinstance(self.decision, Exception):
            raise self.decision
        return self.decision(notice) if callable(self.decision) else self.decision

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sends.append(content)
        return SimpleNamespace(success=True)


def _runner(adapter):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner.config = SimpleNamespace(
        group_sessions_per_user=True,
        thread_sessions_per_user=False,
    )
    runner._enqueue_fifo = lambda *_args: None
    return runner


def _source():
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="chat-1",
        user_id="user-1",
        chat_type="dm",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("decision", "expected_status"),
    [
        ({
            "status": "active", "verdict": "continue", "should_continue": True,
            "continuation_prompt": "continue", "message": "native continue",
        }, "continue"),
        ({
            "status": "done", "verdict": "done", "should_continue": False,
            "continuation_prompt": None, "message": "native done",
        }, "done"),
        ({
            "status": "active", "verdict": "waiting", "should_continue": False,
            "continuation_prompt": None, "message": "native wait",
        }, "wait"),
    ],
)
async def test_goal_decision_notices_are_typed_before_send(decision, expected_status):
    adapter = _NoticeAdapter(lambda notice: GoalStatusNotice(
        kind=notice.kind,
        status=notice.status,
        text=f"replacement:{notice.text}",
    ))
    runner = _runner(adapter)
    manager = SimpleNamespace(is_active=lambda: True, evaluate_after_turn=lambda *_a, **_k: decision)
    runner._post_turn_manager = AsyncMock(return_value=manager)
    runner._run_in_executor_with_context = AsyncMock(return_value=decision)

    await runner._post_turn_goal_continuation(
        session_entry=SimpleNamespace(session_id="session-1"),
        source=_source(),
        final_response="turn response",
    )

    assert len(adapter.notices) == 1
    notice = adapter.notices[0][1]
    assert notice == GoalStatusNotice(
        kind=GoalStatusNoticeKind.GOAL,
        status=expected_status,
        text=decision["message"],
    )
    assert adapter.sends == [f"replacement:{decision['message']}"]


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", [None, RuntimeError("policy unavailable")])
async def test_goal_notice_policy_suppression_and_failure_are_fail_closed(policy):
    adapter = _NoticeAdapter(policy)
    runner = _runner(adapter)

    await runner._send_goal_status_notice(
        _source(), "must not escape", status="done",
    )

    assert len(adapter.notices) == 1
    assert adapter.sends == []


@pytest.mark.asyncio
async def test_adapter_without_goal_notice_policy_preserves_legacy_send():
    adapter = SimpleNamespace(sends=[])

    async def send(_chat_id, content, reply_to=None, metadata=None):
        adapter.sends.append(content)
        return SimpleNamespace(success=True)

    adapter.send = send
    runner = _runner(adapter)

    await runner._send_goal_status_notice(_source(), "legacy notice", status="active")

    assert adapter.sends == ["legacy notice"]
