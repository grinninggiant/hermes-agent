"""Restart draining must include the real adapter completion callback."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.event import ProcessingOutcome
from tests.gateway.test_42039_duplicate_user_message import _bootstrap
from tests.gateway.test_platform_continuation_seam import _ContinuationAdapter, _event


@pytest.mark.asyncio
async def test_restart_does_not_drain_while_adapter_completion_is_unsettled(monkeypatch, tmp_path):
    entered = asyncio.Event()
    release = asyncio.Event()

    class DeliveryAdapter(_ContinuationAdapter):
        async def on_processing_complete(self, event, outcome):
            assert outcome == ProcessingOutcome.SUCCESS
            entered.set()
            await asyncio.wait_for(release.wait(), timeout=5)
            await super().on_processing_complete(event, outcome)

    runner = _bootstrap(monkeypatch, tmp_path)
    adapter = DeliveryAdapter()
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner.session_store.lookup_by_session_key.return_value = (
        runner.session_store.get_or_create_session.return_value
    )
    runner._post_turn_goal_continuation = AsyncMock()
    runner._post_turn_loop_completion = AsyncMock()
    runner._run_agent = AsyncMock(return_value={
        "final_response": "Unfinished bounded work",
        "messages": [{"role": "user", "content": "finish the task"}],
        "tools": [], "history_offset": 0, "api_calls": 1,
        "completed": False, "failed": False, "interrupted": False,
        "turn_exit_reason": "max_iterations_reached(1/1)",
        "session_id": "sess-dedup", "input_tokens": 11,
        "output_tokens": 7, "last_prompt_tokens": 11,
    })
    adapter.set_message_handler(runner._handle_message)
    await adapter.handle_message(_event())
    tasks = list(adapter._session_tasks.values())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert tasks and any(not task.done() for task in tasks)
        assert runner._active_work_count() == 1, (
            "Restart reports drained after core hooks, while adapter completion still owns delivery"
        )
        # A multiplex registry alias must not count the same live task twice.
        runner._profile_adapters = {"secondary": {Platform.TELEGRAM: adapter}}
        assert runner._active_work_count() == 1
        _, timed_out = await runner._drain_active_agents(timeout=0.01)
        assert timed_out is True
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
    assert runner._active_work_count() == 0
