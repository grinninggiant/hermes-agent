"""Exact live-session admission lease; never interrupts or terminates work.

runtime.quiesce({session_id}) closes admission and samples native work. Result:
{status: idle|busy|unknown, admission: closed, nonce, generation, scope: session}.
Repeat on the SAME attached transport to poll (same proof). Only idle is a
session drain receipt, NOT permission to terminate a shared serve process.
runtime.quiesce.cancel({session_id, nonce, generation}) reopens admission.
4001: no current session authority; 4094: stale proof/transport; 4093: gated RPC.
Disconnect does not reopen admission: reconnect must explicitly acquire a new
proof via quiesce and cancel it. No owner/profile/path selectors are accepted.
"""
from .method_ctx import HandlerRegistry, bind_module

_registry = HandlerRegistry()
method = _registry.method

_RUNTIME_DRAIN_METHODS = frozenset({
    "runtime.quiesce", "runtime.quiesce.cancel", "session.interrupt",
    "approval.respond", "clarify.respond", "sudo.respond", "secret.respond",
    "clarify.lock", "request.answer",
    "subagent.list", "subagent.tail", "subagent.interrupt", "session.history",
    "session.usage",
})


def _process_admission_closed():
    from .process_admission import is_closed
    return is_closed()


def _runtime_admit_rpc(rid, name, params):
    if name in _RUNTIME_DRAIN_METHODS:
        return None, None
    with _sessions_lock:
        session = _sessions.get(params.get("session_id"))
        if session is None or "history_lock" not in session:
            return None, None
        with session["history_lock"]:
            if session.get("_runtime_quiescence"):
                return None, _err(rid, 4093, "session admission closed for runtime transition")
            session["_runtime_rpc_active"] = session.get("_runtime_rpc_active", 0) + 1
            return session, None


def _runtime_release_rpc(session):
    if session is not None:
        with session["history_lock"]:
            session["_runtime_rpc_active"] -= 1


def _runtime_work_status(sid, session):
    """Fail closed if any native ownership inventory cannot be inspected."""
    from tools.async_delegation import has_live_for_session
    from tools.delegate_tool_registry import _active_subagents, _active_subagents_lock
    try:
        if any(session.get(k) for k in (
                "running", "_runtime_rpc_active", "queued_prompt", "queued_prompts",
                "_auto_continue_scheduled", "_closing", "_finalized")):
            return "busy"
        for key in ("_run_thread", "_agent_build_thread"):
            thread = session.get(key)
            if thread is not None and thread.is_alive():
                return "busy"
        with _active_subagents_lock:
            if any(r.get("owner_session_record") is session for r in _active_subagents.values()):
                return "busy"
        if has_live_for_session(session_key=str(session.get("session_key") or ""), origin_ui_session_id=sid):
            return "busy"
        processes = _session_processes(session)
        if any(p.get("status") not in {"completed", "exited", "killed", "failed"} for p in processes):
            return "busy"
        return "idle"
    except Exception:
        logger.warning("runtime quiescence inventory unavailable", exc_info=True)
        return "unknown"


@method("runtime.quiesce")
def _runtime_quiesce(rid, params):
    from uuid import uuid4
    sid = params.get("session_id")
    with _sessions_lock:
        transport, session = _current_session_steer_authority(sid)
        if transport is None or session is None or "history_lock" not in session:
            return _err(rid, 4001, "session not found or not owned by this transport")
        with session["history_lock"]:
            generation = session.setdefault("_runtime_generation", uuid4().hex)
            proof = session.get("_runtime_quiescence")
            if proof is None or proof["transport"] is not transport:
                proof = {"nonce": uuid4().hex, "transport": transport, "record": session}
                session["_runtime_quiescence"] = proof
            return _ok(rid, {"scope": "session", "status": _runtime_work_status(sid, session),
                             "admission": "closed", "nonce": proof["nonce"], "generation": generation})


@method("runtime.quiesce.cancel")
def _runtime_quiesce_cancel(rid, params):
    with _sessions_lock:
        transport, session = _current_session_steer_authority(params.get("session_id"))
        if transport is None or session is None or "history_lock" not in session:
            return _err(rid, 4001, "session not found or not owned by this transport")
        with session["history_lock"]:
            proof = session.get("_runtime_quiescence")
            if (not proof or proof["record"] is not session or proof["transport"] is not transport
                    or params.get("nonce") != proof["nonce"]
                    or params.get("generation") != session.get("_runtime_generation")):
                return _err(rid, 4094, "stale runtime quiescence proof")
            session.pop("_runtime_quiescence")
            return _ok(rid, {"admission": "open", "generation": session["_runtime_generation"]})


def register(server):
    from .process_admission import bind_runtime
    bind_runtime(server)
    bind_module(globals(), server)
