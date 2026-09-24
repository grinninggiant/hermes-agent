"""Native RPC admission: no model calls, isolated live records and registries."""
import threading
from types import SimpleNamespace

import pytest


@pytest.fixture
def runtime(monkeypatch):
    from tui_gateway import server
    from tools import async_delegation, delegate_tool_registry
    from tools.process_registry import process_registry
    transport = SimpleNamespace(write=lambda frame: True)
    owner = dict(session_key="parent", transport=transport, history_lock=threading.RLock(), running=False)
    monkeypatch.setattr(server, "_sessions", {"owned": owner})
    monkeypatch.setattr(delegate_tool_registry, "_active_subagents", {})
    monkeypatch.setattr(async_delegation, "_records", {})
    monkeypatch.setattr(process_registry, "list_sessions", lambda: [])
    def call(method, via=transport, **params):
        return server.dispatch(dict(id=1, method=method, params={"session_id": "owned", **params}), via)
    return server, owner, transport, call


def test_quiesce_cancel_authority_and_generation(runtime):
    server, owner, transport, call = runtime
    assert call("runtime.quiesce", profile="general")["error"]["code"] == 4000
    assert "_runtime_quiescence" not in owner
    proof = call("runtime.quiesce")["result"]
    assert proof["status"] == "idle" and proof["admission"] == "closed"
    assert call("prompt.submit", text="never run")["error"]["code"] == 4093
    assert call("runtime.quiesce", via=SimpleNamespace(write=lambda f: True))["error"]["code"] == 4001
    assert call("runtime.quiesce.cancel", nonce="wrong", generation=proof["generation"])["error"]["code"] == 4094
    assert call("runtime.quiesce.cancel", nonce=proof["nonce"], generation=proof["generation"])["result"]["admission"] == "open"
    newer = call("runtime.quiesce")["result"]
    assert newer["nonce"] != proof["nonce"]
    assert call("runtime.quiesce.cancel", nonce=proof["nonce"], generation=proof["generation"])["error"]["code"] == 4094
    server._sessions["owned"] = dict(session_key="parent", transport=transport, history_lock=threading.RLock(), running=False)
    assert call("runtime.quiesce.cancel", nonce=newer["nonce"], generation=newer["generation"])["error"]["code"] == 4094


def test_busy_and_concurrent_admission_are_never_idle(runtime, monkeypatch):
    server, owner, transport, call = runtime
    entered, release = threading.Event(), threading.Event()
    def work(rid, params):
        entered.set()
        assert release.wait(5)
        return server._ok(rid, {})
    monkeypatch.setitem(server._methods, "test.work", work)
    worker = threading.Thread(target=lambda: call("test.work"))
    worker.start()
    try:
        assert entered.wait(5)
        assert call("runtime.quiesce")["result"]["status"] == "busy"
        assert call("test.work")["error"]["code"] == 4093
        assert not server._notif_claim_turn(owner)
    finally:
        release.set()
        worker.join(5)
    assert call("runtime.quiesce")["result"]["status"] == "idle"
    owner["running"] = True
    assert call("runtime.quiesce")["result"]["status"] == "busy"
    assert owner["running"] is True and "_turn_cancel_requested" not in owner


@pytest.mark.parametrize("kind", ["child", "delegation", "process", "unknown", "queued", "thread"])
def test_owned_work_and_unknown_refuse_transition(runtime, monkeypatch, kind):
    server, owner, transport, call = runtime
    from tools import async_delegation, delegate_tool_registry
    if kind == "child":
        delegate_tool_registry._active_subagents["child"] = dict(owner_session_id="owned", owner_session_record=owner, owner_transport=transport)
    elif kind == "delegation":
        async_delegation._records["d"] = dict(status="running", session_key="parent", origin_ui_session_id="owned")
    elif kind == "process":
        monkeypatch.setattr(server, "_session_processes", lambda s: [{"status": "running"}])
    elif kind == "unknown":
        monkeypatch.setattr(server, "_session_processes", lambda s: (_ for _ in ()).throw(RuntimeError("unavailable")))
    elif kind == "queued":
        owner["queued_prompt"] = {"text": "pending"}
    else:
        owner["_run_thread"] = threading.current_thread()
    assert call("runtime.quiesce")["result"]["status"] == ("unknown" if kind == "unknown" else "busy")



def test_real_prompt_races_quiesce_and_other_sessions_stay_open(runtime, monkeypatch):
    server, owner, transport, call = runtime
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(server, "_typed_stop_phrase_response", lambda *a: None)
    def fence(*args):
        entered.set()
        assert release.wait(5)
        return server._err(1, 4122, "fixture refuses model execution")
    monkeypatch.setattr(server, "_legacy_group_fence_error", fence)
    replies = []
    worker = threading.Thread(target=lambda: replies.append(call("prompt.submit", text="race")))
    worker.start()
    try:
        assert entered.wait(5)
        assert call("runtime.quiesce")["result"]["status"] == "busy"
        assert call("prompt.submit", text="late")["error"]["code"] == 4093
    finally:
        release.set()
        worker.join(5)
    assert replies[0]["error"]["code"] == 4122
    assert call("runtime.quiesce")["result"]["status"] == "idle"
    other = dict(session_key="other", transport=transport, history_lock=threading.RLock(), running=False)
    server._sessions["other"] = other
    assert call("prompt.submit", session_id="other", text="unfenced")["error"]["code"] == 4122
    assert "_runtime_quiescence" not in other
    assert call("runtime.quiesce", session_id="missing")["error"]["code"] == 4001


def test_detached_transport_cannot_reuse_proof(runtime):
    server, owner, transport, call = runtime
    proof = call("runtime.quiesce")["result"]
    new_transport = SimpleNamespace(write=lambda frame: True)
    owner["transport"] = new_transport
    assert call("runtime.quiesce.cancel", nonce=proof["nonce"], generation=proof["generation"])["error"]["code"] == 4001
    assert call("runtime.quiesce.cancel", via=new_transport, nonce=proof["nonce"], generation=proof["generation"])["error"]["code"] == 4094
    new_proof = call("runtime.quiesce", via=new_transport)["result"]
    assert new_proof["nonce"] != proof["nonce"]
    assert call("runtime.quiesce.cancel", via=new_transport, nonce=new_proof["nonce"], generation=new_proof["generation"])["result"]["admission"] == "open"
