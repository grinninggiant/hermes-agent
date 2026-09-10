"""Generic platform boundaries for durable continuation fencing."""

import asyncio
import logging
from types import SimpleNamespace
from types import MappingProxyType
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, ProcessingOutcome
from gateway.session import SessionSource, build_session_key


class _ContinuationAdapter(BasePlatformAdapter):
    supports_response_streaming = False

    def __init__(self):
        super().__init__(
            PlatformConfig(enabled=True, token="fake", typing_indicator=False),
            Platform.TELEGRAM,
        )
        self.sent = []
        self.turn_results = []
        self.outcomes = []
        self.admit = True
        self.execution_allowed = True

    async def connect(self, *, is_reconnect=False):
        return True

    async def disconnect(self):
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append(content)
        return SendResult(success=True, message_id="sent-1")

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}

    async def on_processing_start(self, event):
        return self.admit

    async def allow_internal_execution(self, event):
        return self.execution_allowed

    async def on_processing_complete(self, event, outcome):
        self.outcomes.append(outcome)

    async def prepare_turn_delivery(self, event, response, result):
        assert result is event._gateway_turn_result
        self.turn_results.append(result)
        if str(result.get("turn_exit_reason") or "").startswith("max_iterations_reached("):
            return None
        return response


def _event(*, internal=False):
    return MessageEvent(
        text="continue the unfinished task",
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="chat-1",
            chat_type="dm",
            user_id="user-1",
        ),
        message_id="message-1",
        internal=internal,
    )


@pytest.mark.asyncio
async def test_iteration_limit_is_classified_before_platform_delivery(monkeypatch, tmp_path):
    """An unfinished bounded turn must not escape as a terminal platform response."""
    from gateway.run import GatewayRunner
    from tests.gateway.test_42039_duplicate_user_message import _bootstrap

    adapter = _ContinuationAdapter()
    runner: GatewayRunner = _bootstrap(monkeypatch, tmp_path)
    runner.session_store.lookup_by_session_key.return_value = (
        runner.session_store.get_or_create_session.return_value
    )
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._post_turn_goal_continuation = AsyncMock()
    runner._post_turn_loop_completion = AsyncMock()
    runner._run_agent = AsyncMock(return_value={
        "final_response": "I reached this turn's limit; prompt me to continue.",
        "messages": [{"role": "user", "content": "finish the task"}],
        "tools": [],
        "history_offset": 0,
        "api_calls": 90,
        "completed": False,
        "failed": False,
        "interrupted": False,
        "turn_exit_reason": "max_iterations_reached(90/90)",
        "session_id": "sess-dedup",
        "input_tokens": 11,
        "output_tokens": 7,
        "last_prompt_tokens": 11,
    })
    adapter.set_message_handler(runner._handle_message)
    event = _event()

    from gateway.run_turn_runner import TurnRunner
    from gateway.turn_context import TurnContext

    stream_consumer = MagicMock()
    monkeypatch.setattr("gateway.stream_consumer.GatewayStreamConsumer", stream_consumer)
    stream_ctx = TurnContext(
        source=event.source,
        user_config={},
        resolve_display_setting=lambda *_args: True,
    )
    TurnRunner(runner, stream_ctx)._setup_stream_consumer("telegram")
    stream_consumer.assert_not_called()

    interim_consumer = MagicMock()
    TurnRunner(runner, stream_ctx)._finish_stream_consumer(
        {"final_response": "must wait for the delivery policy", "completed": True},
        [],
        interim_consumer,
    )
    interim_consumer.finish.assert_called_once_with()

    await adapter._process_message_background(event, build_session_key(event.source))

    assert not any("reached this turn's limit" in content for content in adapter.sent)
    assert len(adapter.turn_results) == 1
    turn_result = adapter.turn_results[0]
    assert isinstance(turn_result, MappingProxyType)
    assert turn_result == {
        "completed": False,
        "failed": False,
        "interrupted": False,
        "turn_exit_reason": "max_iterations_reached(90/90)",
        "session_id": "sess-dedup",
        "input_tokens": 11,
        "output_tokens": 7,
        "already_sent": False,
    }
    runner._post_turn_goal_continuation.assert_awaited_once()


@pytest.mark.asyncio
async def test_internal_continuation_can_be_fenced_immediately_before_execution():
    """A stale admitted wake must not execute after its durable owner vetoes it."""
    adapter = _ContinuationAdapter()
    adapter.execution_allowed = False
    handler = AsyncMock(return_value="must not run")
    adapter.set_message_handler(handler)
    event = _event(internal=True)

    await adapter._process_message_background(event, build_session_key(event.source))

    handler.assert_not_awaited()
    assert adapter.sent == []
    assert adapter.outcomes == [ProcessingOutcome.CANCELLED]


@pytest.mark.asyncio
async def test_native_goal_continuation_is_fenced_before_execution(monkeypatch, tmp_path):
    """A normal native goal continuation must honor the adapter execution fence."""
    from gateway.run import GatewayRunner
    from tests.gateway.test_42039_duplicate_user_message import _bootstrap

    adapter = _ContinuationAdapter()
    adapter.execution_allowed = False
    runner: GatewayRunner = _bootstrap(monkeypatch, tmp_path)
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._post_turn_manager = AsyncMock(return_value=SimpleNamespace(is_active=lambda: True))
    runner._run_in_executor_with_context = AsyncMock(return_value={
        "should_continue": True,
        "continuation_prompt": "continue the unfinished task",
        "message": "",
    })
    handler = AsyncMock(return_value="must not run")
    adapter.set_message_handler(handler)
    source = _event().source
    session_key = build_session_key(source)

    await runner._post_turn_goal_continuation(
        session_entry=SimpleNamespace(session_id="session-1"),
        source=source,
        final_response="partial progress",
    )

    continuation = adapter._pending_messages[session_key]
    adapter._pending_messages.pop(session_key)
    await adapter._process_message_background(continuation, session_key)

    handler.assert_not_awaited()
    assert adapter.outcomes == [ProcessingOutcome.CANCELLED]


@pytest.mark.asyncio
async def test_processing_start_exception_remains_best_effort_for_internal_event():
    """A cosmetic lifecycle failure must not become an internal execution veto."""
    adapter = _ContinuationAdapter()
    adapter.on_processing_start = AsyncMock(side_effect=RuntimeError("reaction unavailable"))
    handler = AsyncMock(return_value=None)
    adapter.set_message_handler(handler)
    event = _event(internal=True)

    await adapter._process_message_background(event, build_session_key(event.source))

    handler.assert_awaited_once_with(event)
    assert adapter.outcomes == [ProcessingOutcome.SUCCESS]


@pytest.mark.asyncio
async def test_internal_route_rejected_when_session_mapping_changes_after_adapter_preflight(
    monkeypatch, tmp_path,
):
    """A runner-side key change is rejection, not successful intentional silence."""
    from gateway.run import GatewayRunner
    from tests.gateway.test_42039_duplicate_user_message import _bootstrap

    adapter = _ContinuationAdapter()
    runner: GatewayRunner = _bootstrap(monkeypatch, tmp_path)
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._run_agent = AsyncMock(return_value={"final_response": "must not run"})
    adapter.set_message_handler(runner._handle_message)
    event = _event(internal=True)
    expected_key = build_session_key(event.source)
    event.metadata.update({
        "gateway_session_key": expected_key,
        "gateway_session_id": "sess-dedup",
        "gateway_session_strict": True,
    })

    # The adapter accepted the route under the original mapping. Before the runner
    # resolves it again, the mapping has moved to a different conversation key.
    assert adapter._event_session_key(event) == expected_key
    monkeypatch.setattr(
        runner, "_session_key_for_source",
        lambda _source: "agent:main:telegram:dm:moved",
    )

    await adapter._process_message_background(event, expected_key)

    runner._run_agent.assert_not_awaited()
    assert event._gateway_rejection_reason == "expected_session_key_mismatch"
    assert event.metadata["gateway_session_rejected"] == "expected_session_key_mismatch"
    assert adapter.outcomes == [ProcessingOutcome.FAILURE]
    assert adapter.sent == []


@pytest.mark.asyncio
async def test_internal_route_rejected_when_strict_session_identity_changes_after_preflight(
    monkeypatch, tmp_path,
):
    """A strict key whose current session changed must expose rejection to completion."""
    from gateway.run import GatewayRunner
    from tests.gateway.test_42039_duplicate_user_message import _bootstrap

    adapter = _ContinuationAdapter()
    runner: GatewayRunner = _bootstrap(monkeypatch, tmp_path)
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._run_agent = AsyncMock(return_value={"final_response": "must not run"})
    current_entry = runner.session_store.get_or_create_session.return_value
    current_entry.session_id = "replacement-session"
    runner._async_session_store.lookup_by_session_key = AsyncMock(return_value=current_entry)
    adapter.set_message_handler(runner._handle_message)
    event = _event(internal=True)
    expected_key = build_session_key(event.source)
    event.metadata.update({
        "gateway_session_key": expected_key,
        "gateway_session_id": "original-session",
        "gateway_session_strict": True,
    })

    assert adapter._event_session_key(event) == expected_key
    await adapter._process_message_background(event, expected_key)

    runner._run_agent.assert_not_awaited()
    assert event._gateway_rejection_reason == "strict_session_identity_mismatch"
    assert event.metadata["gateway_session_rejected"] == "strict_session_identity_mismatch"
    assert adapter.outcomes == [ProcessingOutcome.FAILURE]
    assert adapter.sent == []


@pytest.mark.asyncio
async def test_strict_session_identity_is_revalidated_after_awaited_preparation(
    monkeypatch, tmp_path,
):
    """A reset while the last preparation await is blocked must prevent model execution."""
    from gateway.run import GatewayRunner
    from tests.gateway.test_42039_duplicate_user_message import _bootstrap

    runner: GatewayRunner = _bootstrap(monkeypatch, tmp_path)
    event = _event(internal=True)
    expected_key = build_session_key(event.source)
    original = runner.session_store.get_or_create_session.return_value
    original.session_id = "original-session"
    original.session_key = expected_key
    replacement = SimpleNamespace(session_id="replacement-session", session_key=expected_key)
    mapping_changed = False

    async def current_mapping(_key):
        return replacement if mapping_changed else original

    lookup_mock = AsyncMock(side_effect=current_mapping)
    runner.async_session_store.lookup_by_session_key = lookup_mock
    event.metadata.update({
        "gateway_session_key": expected_key,
        "gateway_session_id": "original-session",
        "gateway_session_strict": True,
    })
    preparation_blocked = asyncio.Event()
    release_preparation = asyncio.Event()

    async def block_at_last_pre_run_hook(*_args, **_kwargs):
        preparation_blocked.set()
        await release_preparation.wait()

    runner.hooks.emit = AsyncMock(side_effect=block_at_last_pre_run_hook)
    runner._hmwa_resolve_session = AsyncMock(
        return_value=(event.source, original, expected_key)
    )
    prepared = runner._PreparedTurn(
        history=[], context_prompt="context", message_text=event.text,
        persist_user_message=None, persist_user_timestamp=None,
        persist_user_display_kind="internal_notification",
        persistence_session_id=original.session_id, persistence_owner="owner",
    )
    runner._hmwa_prepare_turn = AsyncMock(return_value=(prepared, None))
    monkeypatch.setattr(
        "gateway.run_heartbeat_acceptance.heartbeat_owner_is_current",
        lambda *_args: True,
    )
    runner._run_agent = AsyncMock(return_value={"final_response": "must not run"})

    task = asyncio.create_task(
        runner._handle_message_with_agent(event, event.source, expected_key, 1)
    )
    await asyncio.wait_for(preparation_blocked.wait(), timeout=2)
    mapping_changed = True
    release_preparation.set()
    await asyncio.wait_for(task, timeout=2)

    assert lookup_mock.await_count == 1
    runner._run_agent.assert_not_awaited()
    assert event._gateway_rejection_reason == "strict_session_identity_mismatch"


@pytest.mark.asyncio
async def test_stale_generation_does_not_publish_result_or_invoke_delivery_policy(
    monkeypatch, tmp_path,
):
    """A completed obsolete run is not a turn eligible for platform delivery policy."""
    from gateway.run import GatewayRunner
    from tests.gateway.test_42039_duplicate_user_message import _bootstrap

    adapter = _ContinuationAdapter()
    adapter.prepare_turn_delivery = AsyncMock(return_value="must not send")
    runner: GatewayRunner = _bootstrap(monkeypatch, tmp_path)
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._run_agent = AsyncMock(return_value={
        "final_response": "obsolete", "completed": True, "messages": [],
    })
    runner._is_session_run_current = MagicMock(return_value=False)
    adapter.set_message_handler(runner._handle_message)
    event = _event()

    await adapter._process_message_background(event, build_session_key(event.source))

    adapter.prepare_turn_delivery.assert_not_awaited()
    assert not hasattr(event, "_gateway_turn_result")
    assert all("obsolete" not in content for content in adapter.sent)


@pytest.mark.asyncio
async def test_queued_first_response_is_prepared_before_terminal_delivery():
    """Queued-chain text and media share the same pre-egress decision."""
    from gateway.run import GatewayRunner

    adapter = _ContinuationAdapter()
    adapter.prepare_turn_delivery = AsyncMock(return_value=None)
    runner = object.__new__(GatewayRunner)
    runner._deliver_queued_first_response = AsyncMock()
    event = _event()
    ctx = SimpleNamespace(
        session_key=build_session_key(event.source), source=event.source,
        stream_consumer_holder=[None], event_message_id=event.message_id,
        inbound_message_id=event.message_id, _status_thread_metadata=None,
        gateway_event=event, _run_still_current=lambda: True, run_generation=1,
    )
    result = {
        "final_response": "blocked\nMEDIA:https://example.invalid/private.png",
        "completed": False, "failed": False, "interrupted": False,
        "turn_exit_reason": "max_iterations_reached(90/90)", "messages": [],
    }

    await GatewayRunner._run_agent_deliver_first_response(
        runner, ctx, adapter, result, result, None,
    )

    adapter.prepare_turn_delivery.assert_awaited_once()
    runner._deliver_queued_first_response.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalidation_point", ["before_policy", "during_policy"])
async def test_stale_queued_turn_has_no_terminal_egress(invalidation_point):
    """A queued turn invalidated on either side of policy cannot send text or media."""
    from gateway.run import GatewayRunner

    adapter = _ContinuationAdapter()
    runner = object.__new__(GatewayRunner)
    runner._deliver_queued_first_response = AsyncMock()
    runner._run_agent_stream_confirmed_final_delivery = MagicMock(return_value=False)
    runner._is_intentional_silence = MagicMock(return_value=False)
    runner._pop_post_delivery_callback = MagicMock(return_value=None)
    current = True
    boundary_reached = asyncio.Event()
    release_boundary = asyncio.Event()

    async def await_stream(_task):
        if invalidation_point == "before_policy":
            boundary_reached.set()
            await release_boundary.wait()

    async def prepare(_event, response, _result):
        if invalidation_point == "during_policy":
            boundary_reached.set()
            await release_boundary.wait()
        return response

    runner._await_stream_task = await_stream
    adapter.prepare_turn_delivery = AsyncMock(side_effect=prepare)
    event = _event()
    ctx = SimpleNamespace(
        session_key=build_session_key(event.source), source=event.source,
        stream_consumer_holder=[MagicMock()], event_message_id=event.message_id,
        inbound_message_id=event.message_id, _status_thread_metadata=None,
        gateway_event=event, _run_still_current=lambda: current, run_generation=1,
        session_id="session-1",
    )
    result = {
        "final_response": "private text\nMEDIA:https://example.invalid/private.png",
        "completed": True, "failed": False, "interrupted": False,
        "turn_exit_reason": "completed", "messages": [],
    }

    task = asyncio.create_task(GatewayRunner._run_agent_deliver_first_response(
        runner, ctx, adapter, result, result, object(),
    ))
    await asyncio.wait_for(boundary_reached.wait(), timeout=2)
    current = False
    release_boundary.set()
    await asyncio.wait_for(task, timeout=2)

    if invalidation_point == "before_policy":
        adapter.prepare_turn_delivery.assert_not_awaited()
    else:
        adapter.prepare_turn_delivery.assert_awaited_once()
    runner._deliver_queued_first_response.assert_not_awaited()
    assert adapter.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("invalidation_point", ["before_policy", "during_policy"])
async def test_stale_normal_turn_has_no_text_media_or_tts_egress(
    monkeypatch, tmp_path, invalidation_point,
):
    """A normal turn invalidated on either side of policy cannot reach any terminal rail."""
    from gateway.run import GatewayRunner
    from tests.gateway.test_42039_duplicate_user_message import _bootstrap

    adapter = _ContinuationAdapter()
    runner: GatewayRunner = _bootstrap(monkeypatch, tmp_path)
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._run_agent = AsyncMock(return_value={
        "final_response": "private text\nMEDIA:https://example.invalid/private.png",
        "completed": True, "failed": False, "interrupted": False,
        "turn_exit_reason": "completed", "messages": [], "session_id": "sess-dedup",
    })
    runner._should_send_voice_reply = MagicMock(return_value=True)
    runner._send_voice_reply = AsyncMock()
    runner._deliver_media_from_response = AsyncMock()
    current = True
    runner._is_session_run_current = MagicMock(side_effect=lambda *_args: current)
    boundary_reached = asyncio.Event()
    release_boundary = asyncio.Event()

    async def persist(*_args, **_kwargs):
        if invalidation_point == "before_policy":
            boundary_reached.set()
            await release_boundary.wait()

    async def prepare(_event, response, _result):
        if invalidation_point == "during_policy":
            boundary_reached.set()
            await release_boundary.wait()
        return response

    runner._hmwa_persist_turn_transcript = AsyncMock(side_effect=persist)
    adapter.prepare_turn_delivery = AsyncMock(side_effect=prepare)
    adapter.set_message_handler(runner._handle_message)
    event = _event()

    task = asyncio.create_task(
        adapter._process_message_background(event, build_session_key(event.source))
    )
    await asyncio.wait_for(boundary_reached.wait(), timeout=2)
    adapter.sent.clear()  # Ignore unrelated pre-turn onboarding notices.
    current = False
    release_boundary.set()
    await asyncio.wait_for(task, timeout=2)

    if invalidation_point == "before_policy":
        adapter.prepare_turn_delivery.assert_not_awaited()
    else:
        adapter.prepare_turn_delivery.assert_awaited_once()
    runner._send_voice_reply.assert_not_awaited()
    runner._deliver_media_from_response.assert_not_awaited()
    assert adapter.sent == []


@pytest.mark.asyncio
async def test_policy_runs_before_auto_tts_terminal_egress(monkeypatch, tmp_path):
    """Whole-file TTS cannot speak a response rejected by the delivery policy."""
    from gateway.run import GatewayRunner
    from tests.gateway.test_42039_duplicate_user_message import _bootstrap

    adapter = _ContinuationAdapter()
    runner: GatewayRunner = _bootstrap(monkeypatch, tmp_path)
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._run_agent = AsyncMock(return_value={
        "final_response": "unfinished speech", "completed": False, "failed": False,
        "interrupted": False, "turn_exit_reason": "max_iterations_reached(90/90)",
        "messages": [], "session_id": "sess-dedup",
    })
    runner._should_send_voice_reply = MagicMock(
        side_effect=lambda _event, response, *_args, **_kwargs: bool(response)
    )
    runner._send_voice_reply = AsyncMock()
    adapter.prepare_turn_delivery = AsyncMock(return_value=None)
    adapter.set_message_handler(runner._handle_message)
    event = _event()

    await adapter._process_message_background(event, build_session_key(event.source))

    adapter.prepare_turn_delivery.assert_awaited_once()
    runner._send_voice_reply.assert_not_awaited()
    assert all("unfinished speech" not in content for content in adapter.sent)


def test_response_streaming_capability_gates_streaming_tts(monkeypatch):
    """An interim-capable consumer must not gain a terminal audio side channel."""
    from gateway.run import GatewayRunner

    adapter = _ContinuationAdapter()
    adapter._should_auto_tts_for_chat = MagicMock(return_value=True)
    runner = object.__new__(GatewayRunner)
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._gateway_loop = None
    consumer = MagicMock()
    monkeypatch.setattr("gateway.streaming_tts_consumer.StreamingTTSConsumer", consumer)
    holder = [None]

    GatewayRunner._run_agent_start_streaming_tts(
        runner, _event().source, "voice", None, holder,
    )

    consumer.assert_not_called()
    assert holder == [None]


def test_response_streaming_capability_keeps_interim_commentary_available(monkeypatch):
    """Disabling terminal response streaming does not disable non-final status output."""
    from gateway.run_turn_runner import TurnRunner
    from gateway.turn_context import TurnContext

    adapter = _ContinuationAdapter()
    runner = MagicMock()
    runner._adapter_for_source.return_value = adapter
    runner._build_stream_consumer_config.return_value = (MagicMock(), None)
    consumer = MagicMock()
    monkeypatch.setattr(
        "gateway.stream_consumer.GatewayStreamConsumer", MagicMock(return_value=consumer),
    )
    ctx = TurnContext(
        source=_event().source, user_config={}, interim_assistant_messages_enabled=True,
        resolve_display_setting=lambda *_args: True, _run_still_current=lambda: True,
    )

    created, stream_delta_cb, interim_cb, want_interim = TurnRunner(
        runner, ctx,
    )._setup_stream_consumer("telegram")
    interim_cb("still working")

    assert created is consumer
    assert stream_delta_cb is None
    assert want_interim is True
    consumer.on_commentary.assert_called_once_with("still working")


@pytest.mark.asyncio
async def test_prepare_turn_delivery_exception_fails_closed(caplog):
    """Policy errors are internal failures and never become outbound error text."""
    adapter = _ContinuationAdapter()
    adapter.prepare_turn_delivery = AsyncMock(
        side_effect=RuntimeError("secret policy detail")
    )
    adapter._notify_turn_error = AsyncMock()
    handler = AsyncMock(return_value="sensitive terminal response")
    adapter.set_message_handler(handler)
    event = _event()
    event._gateway_turn_result = MappingProxyType({
        "completed": True, "failed": False, "interrupted": False,
        "turn_exit_reason": "completed", "session_id": "session-1",
        "input_tokens": 1, "output_tokens": 1, "already_sent": False,
    })

    with caplog.at_level(logging.ERROR):
        await adapter._process_message_background(event, build_session_key(event.source))

    assert adapter.sent == []
    adapter._notify_turn_error.assert_not_awaited()
    assert adapter.outcomes == [ProcessingOutcome.FAILURE]
    assert any("prepare_turn_delivery" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_normal_intentional_empty_response_remains_success():
    adapter = _ContinuationAdapter()
    adapter.prepare_turn_delivery = AsyncMock(return_value=None)
    handler = AsyncMock(return_value=None)
    adapter.set_message_handler(handler)
    event = _event()

    await adapter._process_message_background(event, build_session_key(event.source))

    handler.assert_awaited_once_with(event)
    adapter.prepare_turn_delivery.assert_not_awaited()
    assert getattr(event, "_gateway_rejection_reason", None) is None
    assert adapter.outcomes == [ProcessingOutcome.SUCCESS]


@pytest.mark.asyncio
async def test_default_platform_keeps_classified_response_unchanged():
    """Platforms that do not implement the policy hook retain legacy delivery."""
    adapter = _ContinuationAdapter()
    adapter.prepare_turn_delivery = BasePlatformAdapter.prepare_turn_delivery.__get__(adapter)
    handler = AsyncMock(return_value="ordinary response")
    adapter.set_message_handler(handler)
    event = _event()
    event._gateway_turn_result = MappingProxyType({
        "completed": True,
        "failed": False,
        "interrupted": False,
        "turn_exit_reason": "completed",
        "session_id": "session-1",
        "input_tokens": 1,
        "output_tokens": 1,
        "already_sent": False,
    })

    await adapter._process_message_background(event, build_session_key(event.source))

    assert adapter.sent == ["ordinary response"]
    assert adapter.outcomes == [ProcessingOutcome.SUCCESS]
