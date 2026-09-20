"""Offline component coverage: real factory, registry, turns and native compaction."""
import json
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest

from hermes_constants import get_hermes_home
from hermes_state import SessionDB
from run_agent import AIAgent
from tools.delegate_tool import _build_child_agent, _run_single_child, delegate_task


def wait_for(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("lifecycle did not reach expected state")


@pytest.fixture
def native_parent(tmp_path, monkeypatch):
    # Block ALL external I/O; only the explicit HTTP transport below can reply.
    def no_network(*args, **kwargs):
        raise AssertionError("external network forbidden in acceptance component test")
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        "model:\n  default: test/main\n  context_length: 100000\n"
        "compression:\n  enabled: true\n  context_timeout_seconds: 10\n"
        "delegation:\n  orchestrator_enabled: false\n"
    )
    class Requests(list):
        def __init__(self):
            super().__init__()
            self.read_paths = []
            self.on_send = None
    requests = Requests()

    def send(client, request, **kwargs):
        assert request.url.host == "acceptance.invalid", str(request.url)
        body = json.loads(request.content)
        requests.append(body)
        if requests.on_send is not None:
            requests.on_send()
        text = "Offline transport reply. " * 100
        delta = {"role": "assistant", "content": text}
        finish_reason = "stop"
        if len(requests) <= len(requests.read_paths):
            delta = {"role": "assistant", "tool_calls": [{
                "index": 0, "id": f"read-{len(requests)}", "type": "function",
                "function": {"name": "read_file", "arguments": json.dumps({
                    "path": str(requests.read_paths[len(requests) - 1])
                })},
            }]}
            finish_reason = "tool_calls"
        base = {"id": "fixture-response", "created": 1, "model": "test/main"}
        if body.get("stream"):
            chunks = [dict(base, object="chat.completion.chunk", choices=[
                {"index": 0, "delta": delta, "finish_reason": None}
            ]), dict(base, object="chat.completion.chunk", choices=[
                {"index": 0, "delta": {}, "finish_reason": finish_reason}
            ])]
            data = "".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n"
            return httpx.Response(200, content=data, headers={"content-type": "text/event-stream"}, request=request)
        return httpx.Response(200, json=dict(base, object="chat.completion", choices=[
            {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
        ], usage={"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}), request=request)

    monkeypatch.setattr(httpx.Client, "send", send)
    db = SessionDB(db_path=home / "state.db")
    parent = AIAgent(model="test/main", provider="custom", api_mode="chat_completions",
                     api_key="offline-fixture", base_url="https://acceptance.invalid/v1",
                     enabled_toolsets=["file"], session_db=db,
                     skip_context_files=True, skip_memory=True, quiet_mode=True)
    db.create_session(parent.session_id, source="test")
    try:
        yield parent, requests
    finally:
        parent.hard_interrupt("test cleanup")
        parent.close()
        db.close()


def build(parent, **kwargs):
    return _build_child_agent(0, "acceptance task", None, ["file"], None, 12, 1, parent, **kwargs)


def control(parent, child, action, message=None):
    return json.loads(delegate_task(action=action, subagent_id=child._subagent_id,
                                    message=message, parent_agent=parent))


def snapshot(parent, child):
    rows = json.loads(delegate_task(action="list", parent_agent=parent))["subagents"]
    return next((r for r in rows if r["subagent_id"] == child._subagent_id), None)


def start(parent, child):
    result = {}
    def run():
        result.update(_run_single_child(0, "acceptance task", child, parent))
    thread = threading.Thread(target=run)
    thread.start()
    return thread, result


def test_opt_in_retains_native_child_until_owner_releases(native_parent):
    parent, requests = native_parent
    child = build(parent, acceptance_idle_seconds=5)
    thread, result = start(parent, child)
    try:
        row = wait_for(lambda: (r := snapshot(parent, child)) and r.get("acceptance", {}).get("state") == "idle" and r)
        assert child in parent._active_children
        assert child._session_messages
        assert row["acceptance"]["compaction"] is None
        foreign = AIAgent.__new__(AIAgent)
        foreign.session_id = "other-conversation"
        assert "error" in control(foreign, child, "release")
        sibling = build(parent)
        try:
            for action in ("compact", "continue", "release", "stop"):
                assert "error" in control(sibling, child, action, "unauthorized")
            assert snapshot(sibling, child) is None
        finally:
            sibling.close()
        assert control(parent, child, "release")["status"] == "queued"
        thread.join(10)
        assert not thread.is_alive()
        assert result["status"] == "completed"
        assert snapshot(parent, child) is None
        assert child not in parent._active_children
        assert child._session_messages == []
        assert requests
    finally:
        child.hard_interrupt("test cleanup")
        thread.join(10)


@pytest.mark.parametrize("stop_phase", [None, "lineage", "continuation", "compaction", "queued", "compaction_expiry"])
def test_native_compaction_then_exactly_one_continuation(native_parent, monkeypatch, tmp_path, stop_phase):
    from types import SimpleNamespace
    import agent.context_compressor as compressor_module
    import gateway.slash_commands_session  # mandatory native integration import

    parent, requests = native_parent
    for i in range(8):
        path = tmp_path / f"evidence-{i}.txt"
        path.write_text(f"evidence {i}\n" + "prior evidence " * 200)
        requests.read_paths.append(path)
    summary_calls = []
    def summary_transport(*args, **kwargs):
        summary_calls.append(kwargs)
        if stop_phase == "compaction":
            assert control(parent, child, "stop")["status"] == "interrupt_requested"
        if stop_phase == "compaction_expiry":
            # Let the real native fence deadline elapse inside the transport.
            threading.Event().wait(2.1)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="Retained task evidence."))])
    monkeypatch.setattr(compressor_module, "call_llm", summary_transport)
    child = build(parent, acceptance_idle_seconds=2 if stop_phase == "compaction_expiry" else 20)
    child.context_compressor.tail_token_budget = 100
    old_session_id = child.session_id
    if stop_phase == "lineage":
        child.compression_in_place = False
    first_late = "first turn late steer"
    registry_late = "registry boundary late steer"
    continuation_late = "continuation late steer"
    if stop_phase is None:
        original_clear = child.clear_interrupt
        clear_count = []
        def late_steer_clear(**kwargs):
            cleared = original_clear(**kwargs)
            clear_count.append(True)
            if len(clear_count) == 1:
                assert control(parent, child, "steer", registry_late)["status"] == "queued"
            return cleared
        monkeypatch.setattr(child, "clear_interrupt", late_steer_clear)
        # Queue after the final response, before native finalizer drains steering.
        requests.on_send = lambda: child.steer(first_late) if len(requests) == 9 else None
    thread, result = start(parent, child)
    try:
        wait_for(lambda: (r := snapshot(parent, child)) and r.get("acceptance", {}).get("state") == "idle")
        assert "error" in control(parent, child, "continue", "too soon")
        assert control(parent, child, "compact")["status"] == "queued"
        if stop_phase in {"compaction", "compaction_expiry"}:
            thread.join(10)
            assert not thread.is_alive()
            assert result["status"] == ("completed" if stop_phase == "compaction_expiry" else "interrupted")
            assert result["acceptance"]["reason"] == ("expired" if stop_phase == "compaction_expiry" else "interrupted")
            assert result["acceptance"]["compaction"]["status"] == "not_committed"
            assert len(summary_calls) == 1
            assert "error" in control(parent, child, "continue", "must not restart")
            assert snapshot(parent, child) is None
            assert child._session_messages == []
            return
        row = wait_for(lambda: (r := snapshot(parent, child)) and r.get("acceptance", {}).get("compaction") and r)
        outcome = row["acceptance"]["compaction"]
        if stop_phase == "lineage":
            assert outcome["status"] == "adopted", outcome
            assert outcome["evidence"] == "native_compression_lineage"
            assert parent._session_db.get_session(old_session_id)["end_reason"] == "compression"
            assert parent._session_db.get_session(child.session_id)["parent_session_id"] == old_session_id
            assert child.session_id != old_session_id
        else:
            assert outcome["status"] == "committed", outcome
            assert outcome["evidence"] == "native_in_place_commit"
            assert child._last_compression_attempt_in_place is True
        assert len(summary_calls) == 1
        assert "error" in control(parent, child, "compact")
        message = "Continue once using the retained task evidence."
        before = len(requests)
        if stop_phase is None:
            requests.on_send = lambda: child.steer(continuation_late)
        if stop_phase == "continuation":
            requests.on_send = lambda: child.hard_interrupt("Stop during continuation")
        if stop_phase == "queued":
            with getattr(child, "_delegate_acceptance").condition:
                assert control(parent, child, "continue", message)["status"] == "queued"
                assert control(parent, child, "stop")["status"] == "interrupt_requested"
            thread.join(10)
            assert not thread.is_alive()
            assert result["status"] == "interrupted"
            assert len(requests) == before
            assert snapshot(parent, child) is None
            return
        assert control(parent, child, "continue", message)["status"] == "queued"
        assert "error" in control(parent, child, "continue", message)
        thread.join(10)
        assert not thread.is_alive()
        assert result["status"] == ("interrupted" if stop_phase == "continuation" else "completed"), result
        assert result["acceptance"]["reason"] == ("interrupted" if stop_phase == "continuation" else "continued")
        if stop_phase is None:
            assert result["missed_steer"] == "\n".join([first_late, registry_late, continuation_late])
        assert len(requests) == before + 1
        assert any(m.get("content") == message for m in requests[-1]["messages"])
        assert any("Retained task evidence." in str(m.get("content")) for m in requests[-1]["messages"])
        assert snapshot(parent, child) is None
        assert child not in parent._active_children
    finally:
        child.hard_interrupt("test cleanup")
        thread.join(10)


@pytest.mark.parametrize("ending", ["stop", "parent_stop", "expiry", "timeout"])
def test_idle_termination_uses_native_teardown(native_parent, monkeypatch, ending):
    import tools.delegate_tool as delegate_module

    parent, requests = native_parent
    if ending == "timeout":
        monkeypatch.setattr(delegate_module, "_get_child_timeout", lambda: 2.0)
    child = build(parent, acceptance_idle_seconds=2 if ending == "expiry" else 20)
    thread, result = start(parent, child)
    try:
        wait_for(lambda: (r := snapshot(parent, child)) and r.get("acceptance", {}).get("state") == "idle")
        before = len(requests)
        if ending == "stop":
            assert control(parent, child, "stop")["status"] == "interrupt_requested"
        elif ending == "parent_stop":
            parent.hard_interrupt("Stop parent and retained children")
        thread.join(10)
        assert not thread.is_alive()
        expected = {"stop": "interrupted", "parent_stop": "interrupted", "expiry": "completed", "timeout": "timeout"}
        assert result["status"] == expected[ending], result
        if ending != "timeout":
            assert result["acceptance"]["reason"] == ("expired" if ending == "expiry" else "interrupted")
        assert snapshot(parent, child) is None
        assert child not in parent._active_children
        wait_for(lambda: child._session_messages == [])
        assert len(requests) == before
        assert "error" in control(parent, child, "continue", "cannot restart")
    finally:
        child.hard_interrupt("test cleanup")
        thread.join(10)


@pytest.mark.parametrize("parent_stop", [False, True])
def test_stop_at_first_turn_finalizer_clear_is_sticky(native_parent, monkeypatch, parent_stop):
    parent, requests = native_parent
    child = build(parent, acceptance_idle_seconds=0.2)
    original_clear = child.clear_interrupt
    stopped = []
    def stop_then_clear(**kwargs):
        if requests and not stopped:
            stopped.append(True)
            (parent if parent_stop else child).hard_interrupt("Stop at finalizer clear")
        return original_clear(**kwargs)
    monkeypatch.setattr(child, "clear_interrupt", stop_then_clear)
    result = _run_single_child(0, "acceptance task", child, parent)
    assert stopped
    assert result["status"] == "interrupted"
    assert result["acceptance"]["reason"] == "interrupted"
    assert len(requests) == 1
    assert snapshot(parent, child) is None
    assert child not in parent._active_children
    assert child._session_messages == []


def test_ordinary_child_is_not_retained(native_parent):
    parent, _ = native_parent
    child = build(parent)
    result = _run_single_child(0, "ordinary task", child, parent)
    assert result["status"] == "completed"
    assert snapshot(parent, child) is None
    assert child not in parent._active_children
    assert child._session_messages == []
