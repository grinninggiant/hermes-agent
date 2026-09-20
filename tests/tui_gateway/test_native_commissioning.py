"""Desktop slash RPC -> native commissioner -> real child with offline vendor I/O."""
import json
import threading
from types import SimpleNamespace

import pytest

from tests.tools.test_delegate_acceptance_lifecycle import native_parent, wait_for  # noqa: F401


@pytest.mark.parametrize("ending", ["release", "stop", "session_stop"])
def test_desktop_commissions_owned_native_children(native_parent, monkeypatch, ending):
    from tui_gateway import server
    parent, requests = native_parent
    frames = []
    transport = SimpleNamespace(write=lambda frame: frames.append(frame))
    owner = {"session_key": parent.session_id, "agent": parent, "history": [],
             "history_lock": threading.RLock(), "transport": transport, "running": False}
    monkeypatch.setattr(server, "_sessions", {"ui-owner": owner})
    def forbidden_worker(*args, **kwargs):
        raise AssertionError("commissioning must not spawn a slash worker")
    monkeypatch.setattr(server, "_SlashWorker", forbidden_worker)
    before = (parent._cached_system_prompt, list(parent._session_messages))

    def call(command, via=transport):
        rid = command
        server.dispatch({"id": rid, "method": "slash.exec", "params": {
            "session_id": "ui-owner", "command": command}}, transport=via)
        return wait_for(lambda: next((f for f in frames if f.get("id") == rid), None))

    # Existing RPC must accept the host command, not spawn a slash subprocess.
    assert "commission" in call("/commission")['result']['output'].lower()
    server.dispatch({"id": "run", "method": "slash.exec", "params": {
        "session_id": "ui-owner", "command": "/commission run"}}, transport=transport)
    try:
        def idle():
            response = call("/commission status")
            frames[:] = [f for f in frames if f.get("id") != "/commission status"]
            rows = json.loads(response["result"]["output"])["children"]
            return rows if len(rows) == 2 and all(r["acceptance"]["state"] == "idle" for r in rows) else None
        rows = wait_for(idle)
        assert requests and owner["running"]
        assert {r["package_id"] for r in rows} == {"ops239-ec8e0050-guidance", "ops239-b7013cf0-guidance"}
        foreign = SimpleNamespace(write=lambda frame: frames.append(frame))
        assert "not owned" in call("/commission status", via=foreign)["result"]["output"]
        # A replacement generation sharing the agent cannot control these children.
        server._sessions["ui-owner"] = {**owner}
        denied = call(f"/commission stop {rows[0]['subagent_id']}")
        assert "owned" in denied["result"]["output"]
        server._sessions["ui-owner"] = owner
        frames.clear()
        if ending == "session_stop":
            response = server.dispatch({"id": "stop", "method": "session.interrupt",
                "params": {"session_id": "ui-owner"}}, transport=transport)
            assert response["result"]["status"] == "interrupted"
        else:
            for row in rows:
                result = json.loads(call(f"/commission {ending} {row['subagent_id']}")["result"]["output"])
                assert result["status"] in {"queued", "interrupt_requested"}
        receipt = wait_for(lambda: next((f for f in frames if f.get("id") == "run"), None))
        payload = json.loads(receipt["result"]["output"])
        assert payload["scope"] == "component_ready_not_live_acceptance"
        assert payload["approval_trial"] == "pending_native_human_control"
        results = payload["results"]
        from pathlib import Path
        for entry in results:
            evidence = entry["evidence"]
            assert Path(evidence["final_result_path"]).is_file()
            assert evidence["transcript_status"] == "retained"
            assert evidence["transcripts"]
            assert "summary" not in entry and "tool_trace" not in entry
        assert all(r["status"] == ("completed" if ending == "release" else "interrupted") for r in results)
        assert before == (parent._cached_system_prompt, parent._session_messages)
        assert not owner["running"] and not parent._active_children
        assert json.loads(call("/commission status")["result"]["output"])["receipt"]["results"] == results
    finally:
        parent.hard_interrupt("test cleanup")
        wait_for(lambda: not owner["running"])


def test_active_parent_rejects_run_without_waiting_or_clearing_owner(native_parent, monkeypatch):
    from tui_gateway import server
    parent, requests = native_parent
    frames = []
    transport = SimpleNamespace(write=frames.append)
    run_thread = threading.current_thread()
    owner = {"session_key": parent.session_id, "agent": parent, "history": [],
             "history_lock": threading.RLock(), "transport": transport,
             "running": True, "_run_thread": run_thread}
    monkeypatch.setattr(server, "_sessions", {"ui-owner": owner})
    server.dispatch({"id": "busy", "method": "slash.exec", "params": {
        "session_id": "ui-owner", "command": "commission run"}}, transport=transport)
    response = wait_for(lambda: next((f for f in frames if f.get("id") == "busy"), None))
    assert "session busy" in response["result"]["output"]
    assert owner["running"] and owner["_run_thread"] is run_thread
    assert not requests and not parent._active_children
