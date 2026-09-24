"""Regression coverage for the gateway turn-result normalization boundary."""

from unittest.mock import MagicMock

import pytest

from gateway.config import Platform
from gateway.run_turn import GatewayTurnMixin
from gateway.run_turn_runner import TurnRunner
from gateway.session import SessionSource
from gateway.turn_context import TurnContext


class _Agent:
    model = "test-model"
    session_prompt_tokens = 11
    session_completion_tokens = 7
    context_compressor = None
    tools = []


def _turn_runner(monkeypatch, *, final_response, turn_exit_reason):
    runner = MagicMock()
    runner._provider_routing = {}
    runner._service_tier = None
    runner._get_system_prompt_for_channel.return_value = ""
    runner._resolve_session_agent_runtime.return_value = ("test-model", {"provider": "test"})
    runner._resolve_session_reasoning_config.return_value = {}
    runner._resolve_session_service_tier.return_value = None
    runner._resolve_turn_agent_config.return_value = {"model": "test-model", "runtime": {}}

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="chat-1",
        chat_type="dm",
        user_id="user-1",
    )
    ctx = TurnContext(
        source=source,
        message="finish the task",
        history=[],
        session_id="session-1",
        session_key=None,
        user_config={},
        _run_still_current=lambda: True,
    )
    turn_runner = TurnRunner(runner, ctx)
    agent = _Agent()
    result = {
        "final_response": final_response,
        "messages": [],
        "api_calls": 1,
        "completed": False,
        "failed": False,
        "interrupted": False,
        "turn_exit_reason": turn_exit_reason,
    }
    monkeypatch.setattr(turn_runner, "_setup_stream_consumer", lambda _platform: (None, None, None, False))
    monkeypatch.setattr(turn_runner, "_resolve_turn_agent", lambda *_args: (agent, False))
    monkeypatch.setattr(turn_runner, "_publish_agent_for_interrupt", lambda _agent: True)
    monkeypatch.setattr(turn_runner, "_wire_turn_agent_callbacks", lambda *_args: None)
    monkeypatch.setattr(turn_runner, "_load_turn_history", lambda *_args: ([], [], []))
    monkeypatch.setattr(turn_runner, "_prepare_turn_message", lambda _history: (None, None))
    monkeypatch.setattr(turn_runner, "_run_conversation_with_approval", lambda *_args: result)
    monkeypatch.setattr(turn_runner, "_finish_stream_consumer", lambda *_args: None)
    monkeypatch.setattr(turn_runner, "_sync_session_after_run", lambda _history: (False, "session-1", 0))
    return turn_runner


@pytest.mark.parametrize("final_response", ["bounded response", ""])
def test_run_sync_preserves_exit_reason_for_populated_and_empty_responses(monkeypatch, final_response):
    turn_runner = _turn_runner(
        monkeypatch,
        final_response=final_response,
        turn_exit_reason="max_iterations_reached(90/90)",
    )

    normalized = turn_runner.run_sync()
    immutable = GatewayTurnMixin._immutable_gateway_turn_result(normalized, "session-1")

    assert immutable["turn_exit_reason"] == "max_iterations_reached(90/90)"


def test_run_sync_keeps_missing_exit_reason_fail_closed(monkeypatch):
    turn_runner = _turn_runner(monkeypatch, final_response="response", turn_exit_reason=None)

    normalized = turn_runner.run_sync()
    immutable = GatewayTurnMixin._immutable_gateway_turn_result(normalized, "session-1")

    assert immutable["turn_exit_reason"] is None
