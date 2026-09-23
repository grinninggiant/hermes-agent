"""Background-review dispatch restriction survives both tool worker submission paths."""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import threading

import pytest

from agent import tool_executor as executor
from hermes_cli import plugins


def _blocked():
    # Real dispatch-side gate, not a mock of its result.
    return plugins._dispatch_pre_tool_call_hooks("terminal", {"command": "true"})[0]


def test_sequential_worker_inherits_review_whitelist_and_cleans_up(monkeypatch):
    agent = SimpleNamespace(_tool_worker_threads=set(), _tool_worker_threads_lock=threading.Lock(),
                            _interrupt_requested=False, _touch_activity=lambda _: None)
    seen = []

    def dispatch(_agent, **kw):
        seen.append(_blocked())
        return executor._ManagedToolResult(result=seen[-1], args={}, middleware_trace=[],
                                           blocked=bool(seen[-1]), dispatched=not bool(seen[-1]))

    monkeypatch.setattr(executor, "_run_agent_tool_execution_middleware", dispatch)
    plugins.set_thread_tool_whitelist({"read_file"}, deny_msg_fmt="review denied {tool_name}")
    try:
        result = executor._run_sequential_tool_execution_middleware(
            agent, function_name="terminal", function_args={"command": "true"},
            effective_task_id="review", tool_call_id="seq", execute=lambda _: pytest.fail("executed"))
        assert result.result == "review denied terminal"
        assert seen == ["review denied terminal"]
    finally:
        plugins.clear_thread_tool_whitelist()


def test_concurrent_workers_inherit_review_whitelist_and_clear_on_reuse():
    results = []
    both_workers = threading.Barrier(2)

    def dispatch(index, start_order):
        both_workers.wait(timeout=5)
        results.append(_blocked())

    batch = SimpleNamespace(run_worker=dispatch)
    plugins.set_thread_tool_whitelist({"read_file"}, deny_msg_fmt="review denied {tool_name}")
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures, _ = executor._ConcurrentBatch.submit_all(batch, pool, [0, 1])
            for future in futures:
                future.result(timeout=5)
            assert results == ["review denied terminal"] * 2
            # Both pooled threads must drop the review gate before reuse.
            def after():
                both_workers.wait(timeout=5)
                return _blocked()
            followups = [pool.submit(after) for _ in range(2)]
            assert [future.result(timeout=5) for future in followups] == [None, None]
            def crash():
                assert _blocked() == "review denied terminal"
                raise ValueError("worker failed")
            with pytest.raises(ValueError, match="worker failed"):
                pool.submit(executor.propagate_context_to_thread(crash)).result(timeout=5)
            assert pool.submit(_blocked).result(timeout=5) is None
    finally:
        plugins.clear_thread_tool_whitelist()
