"""Offline host-component entry, native dispatch/turn/compaction, no live acceptance."""
import json
import threading
from types import SimpleNamespace

import pytest

from tests.tools.test_delegate_acceptance_lifecycle import native_parent, wait_for  # noqa: F401

BASE = "ops239-ec8e0050-guidance"
CAND = "ops239-b7013cf0-guidance"


def test_fixed_scenario_receipt_retains_results_without_prompt(native_parent, monkeypatch):
    from pathlib import Path
    from tools.delegate_tool_commissioning import NativeCommissioning
    parent, requests = native_parent
    def forbidden(*args, **kwargs):
        raise AssertionError("commissioning must not scan sessions or extract prompt bodies")
    monkeypatch.setattr(parent._session_db, "list_sessions_rich", forbidden)
    monkeypatch.setattr(parent._session_db, "get_messages", forbidden)
    owned_ids = {}
    requests.on_send = lambda: owned_ids.update(
        (c._instruction_package.package_id, c.session_id) for c in parent._active_children)
    host = NativeCommissioning(parent, enabled=True)
    result = host.run([BASE, CAND], retention_seconds=0.1)
    assert result["scope"] == "component_ready_not_live_acceptance"
    roots = []
    for entry in result["results"]:
        root = Path(entry["evidence"]["artifact_path"])
        roots.append(root)
        fixture = json.loads((root / "scenario.json").read_text())
        assert fixture["current_user_correction"] == "blue"
        assert fixture["stale_fixture_memory"] == "red"
        assert fixture["expected"]["approval"] == "separate_native_human_trial_pending"
        final = json.loads((root / "final-result.json").read_text())
        assert final["summary"].startswith("Offline transport reply.")
        assert entry["evidence"]["transcripts"]
        assert entry["evidence"]["transcripts"][0]["session_id"] == owned_ids[entry["instruction_package"]["package_id"]]
        assert "summary" not in entry and "tool_trace" not in entry
    pointers = [p for entry in result["results"] for p in entry["evidence"]["transcripts"]]
    assert {p["session_id"] for p in pointers} == set(owned_ids.values())
    assert all(p["message_count"] == parent._session_db.message_count(p["session_id"]) > 0 for p in pointers)
    assert all(entry["evidence"]["assessment"] == "incomplete_not_evaluated" for entry in result["results"])
    import os
    if os.name == "posix":
        for root in roots:
            assert root.stat().st_mode & 0o777 == 0o700
            assert (root / "final-result.json").stat().st_mode & 0o777 == 0o600
        assert (roots[0].parent / "owner-receipt.json").stat().st_mode & 0o777 == 0o600
    assert roots[0] != roots[1]
    assert (roots[0] / "scenario.json").read_bytes() == (roots[1] / "scenario.json").read_bytes()
    assert "Offline transport reply" not in json.dumps(result)


@pytest.mark.parametrize("persistence", ["missing_handle", "missing_db", "empty", "read_error"])
def test_missing_transcript_evidence_is_explicit(native_parent, persistence, monkeypatch):
    from tools.delegate_tool_commissioning import _transcript_evidence
    parent, _ = native_parent
    entry = {"native_child_session_id": "owned-unpersisted-child"}
    if persistence == "missing_handle":
        entry.clear()
    elif persistence == "missing_db":
        parent = SimpleNamespace(_session_db=None)
    elif persistence == "read_error":
        import sqlite3
        def unavailable(session_id):
            assert session_id == entry["native_child_session_id"]
            raise sqlite3.OperationalError("private database failure detail")
        monkeypatch.setattr(parent._session_db, "message_count", unavailable)
    evidence = _transcript_evidence(parent, entry)
    assert evidence["transcript_status"] == "unavailable"
    assert evidence["assessment"] == "incomplete_not_evaluated"
    assert evidence["transcript_scope"] == "exact_session_only"
    if persistence == "missing_handle":
        assert evidence["transcripts"] == []
    else:
        pointer, = evidence["transcripts"]
        assert pointer["session_id"] == entry["native_child_session_id"]
        if persistence == "empty":
            assert pointer["message_count"] == 0
        elif persistence == "read_error":
            assert pointer["error_type"] == "OperationalError"
            assert "private database failure detail" not in json.dumps(evidence)


@pytest.mark.parametrize("missing_progress", [False, True])
@pytest.mark.parametrize("in_place", [False, True])
@pytest.mark.parametrize("package_id", [BASE, CAND])
def test_owned_progress_survives_native_compaction(native_parent, monkeypatch, missing_progress, in_place, package_id):
    """Scripted HTTP fixture drives real factory/tools, not model-behavior evidence."""
    from pathlib import Path
    import httpx
    import agent.context_compressor as compressor_module
    from tools.delegate_tool_commissioning import NativeCommissioning
    parent, _ = native_parent
    calls = []
    root = None
    def transport(client, request, **kwargs):
        nonlocal root
        body = json.loads(request.content)
        calls.append(body)
        if root is None:
            goal = next(m["content"] for m in body["messages"] if m["role"] == "user")
            root = Path(goal.split("Fixture directory: ", 1)[1].split("\n", 1)[0])
        n = len(calls)
        progress = {"color": "blue", "completed": "initial", "remaining": "post_compaction_read"}
        if n <= 8 or n == 10:
            name = "write_file" if n == 2 else "read_file"
            args = {"path": str(root / ("progress.json" if n in (2, 3, 10) else "scenario.json"))}
            if n == 2:
                args["content"] = json.dumps(progress)
            elif 4 <= n <= 8:
                # Distinct bounded regions exercise real reads, not the duplicate-read guard.
                args.update(offset=n, limit=1)
            delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": f"fixture-{n}",
                "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}
            finish = "tool_calls"
        else:
            text = "Initial verified; post_compaction_read remains." if n == 9 else "Continuation read returned. Approval/Stop pending."
            if missing_progress and n == 11:
                # Preserve the exact native tool failure, not an invented success.
                text = next(m["content"] for m in reversed(body["messages"]) if m["role"] == "tool")
            delta, finish = {"role": "assistant", "content": text}, "stop"
        chunks = [{"id": "offline-fixture", "object": "chat.completion.chunk", "created": 1,
                   "model": "test/main", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                  {"id": "offline-fixture", "object": "chat.completion.chunk", "created": 1,
                   "model": "test/main", "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}]
        return httpx.Response(200, request=request, headers={"content-type": "text/event-stream"},
            content="".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n")
    monkeypatch.setattr(httpx.Client, "send", transport)
    monkeypatch.setattr(compressor_module, "call_llm", lambda *a, **k: SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="Initial complete. Read owned progress on continuation."))]))
    host = NativeCommissioning(parent, enabled=True)
    result = {}
    thread = threading.Thread(target=lambda: result.update(host.run([package_id], retention_seconds=20)))
    thread.start()
    try:
        rows = wait_for(lambda: (r := host.snapshot()) and r[0]["acceptance"]["state"] == "idle" and r)
        assert root is not None
        if not (root / "progress.json").exists():
            raise RuntimeError(calls[2]["messages"][-1]["content"])
        assert json.loads((root / "progress.json").read_text())["color"] == "blue"
        child = parent._active_children[0]
        original_session_id = child.session_id
        child.compression_in_place = in_place
        child.context_compressor.tail_token_budget = 100
        sid = rows[0]["subagent_id"]
        assert host.control(sid, "compact")["status"] == "queued"
        wait_for(lambda: host.snapshot()[0]["acceptance"]["compaction"])
        final_session_id = child.session_id
        assert (final_session_id == original_session_id) is in_place
        if missing_progress:
            (root / "progress.json").unlink()
        assert host.control(sid, "continue")["status"] == "queued"
        thread.join(10)
        assert not thread.is_alive()
        assert len(calls) == 11
        tool_result = next(m["content"] for m in reversed(calls[-1]["messages"]) if m["role"] == "tool")
        retained = json.loads((root / "final-result.json").read_text())
        if missing_progress:
            assert retained["summary"] == tool_result
            assert "not found" in tool_result.lower() or "does not exist" in tool_result.lower()
        else:
            assert "blue" in tool_result and "post_compaction_read" in tool_result
        entry = result["results"][0]
        assert entry["evidence"]["transcripts"]
        assert entry["evidence"]["transcripts"][0]["session_id"] == final_session_id
        assert entry["evidence"]["transcript_status"] == "retained"
        assert any(t["tool"] == "read_file" for t in entry["evidence"]["tool_results"])
    finally:
        parent.hard_interrupt("test cleanup")
        thread.join(10)


def test_host_preflight_rejects_whole_batch_before_construction(native_parent):
    from tools.delegate_tool_commissioning import NativeCommissioning
    parent, requests = native_parent
    with pytest.raises(ValueError):
        NativeCommissioning(parent, enabled=False)
    host = NativeCommissioning(parent, enabled=True)
    for ids, seconds in [([BASE, "invalid"], 20), ([None], 20), ([BASE], True),
                         ([BASE], float("nan")), ([BASE], 301), ([], 20),
                         ([BASE] * 100, 20), (BASE, 20)]:
        with pytest.raises(ValueError):
            host.run(ids, retention_seconds=seconds)
        assert not parent._active_children
        assert not requests


@pytest.mark.parametrize("package_id", [BASE, CAND])
@pytest.mark.parametrize("ending", ["continue", "release", "stop"])
def test_host_entry_native_lifecycle(native_parent, monkeypatch, tmp_path, package_id, ending):
    from agent.instruction_package import resolve_instruction_package
    import agent.context_compressor as compressor_module
    from tools.delegate_tool_commissioning import NativeCommissioning

    parent, requests = native_parent
    parent.enabled_toolsets = ["file", "skills"]
    from hermes_constants import get_hermes_home
    skill = get_hermes_home() / "skills" / "commissioning" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: commissioning\ndescription: Use when checking commissioning evidence.\n---\nRead local evidence.\n")
    for i in range(8):
        path = tmp_path / f"evidence-{i}.txt"
        path.write_text("native evidence " * 200)
        requests.read_paths.append(path)
    summaries = []
    def summary_transport(*args, **kwargs):
        summaries.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="Retained evidence."))])
    monkeypatch.setattr(compressor_module, "call_llm", summary_transport)
    host = NativeCommissioning(parent, enabled=True)
    result = {}
    thread = threading.Thread(target=lambda: result.update(host.run([package_id], retention_seconds=20)))
    thread.start()
    try:
        rows = wait_for(lambda: (rows := host.snapshot()) and rows[0]["acceptance"]["state"] == "idle" and rows)
        row = rows[0]
        child = parent._active_children[0]
        package = resolve_instruction_package(package_id)
        assert package is not None
        assert any(package.skills_tail in str(m.get("content")) for m in requests[0]["messages"])
        assert child._instruction_package is package
        assert row["package_id"] == package_id
        assert row["content_sha256"] == package.content_sha256
        assert parent._instruction_package is None
        child.context_compressor.tail_token_budget = 100
        sid = row["subagent_id"]
        from run_agent import AIAgent
        foreign_parent = AIAgent.__new__(AIAgent)
        foreign_host = NativeCommissioning(foreign_parent, enabled=True)
        assert foreign_host.snapshot() == []
        for action in ("compact", "continue", "release", "stop"):
            assert "error" in foreign_host.control(sid, action)
        with pytest.raises(TypeError):
            host.control(sid, "continue", message="arbitrary prompt")
        assert "error" in host.control(sid + " ", "stop")
        assert "error" in host.control(sid, "continue")
        assert host.control(sid, "compact")["status"] == "queued"
        wait_for(lambda: (rows := host.snapshot()) and rows[0]["acceptance"]["compaction"])
        assert host.snapshot()[0]["acceptance"]["compaction"]["status"] == "committed"
        before = len(requests)
        assert host.control(sid, ending)["status"] in {"queued", "interrupt_requested"}
        thread.join(10)
        assert not thread.is_alive()
        assert result["results"][0]["status"] == ("interrupted" if ending == "stop" else "completed")
        assert result["results"][0]["instruction_package"]["package_id"] == package_id
        assert len(summaries) == 1
        assert len(requests) == before + (ending == "continue")
        if ending == "continue":
            assert any(package.memory_guidance in str(m.get("content")) for m in requests[-1]["messages"])
        assert host.snapshot() == []
        assert not parent._active_children
        assert child._session_messages == []
    finally:
        parent.hard_interrupt("test cleanup")
        thread.join(10)


def test_both_packages_share_native_batch_and_parent_stop(native_parent):
    from tools.delegate_tool_commissioning import NativeCommissioning
    parent, _ = native_parent
    host = NativeCommissioning(parent, enabled=True)
    result = {}
    thread = threading.Thread(target=lambda: result.update(host.run([BASE, CAND], retention_seconds=20)))
    thread.start()
    try:
        rows = wait_for(lambda: (rows := host.snapshot()) and len(rows) == 2
                        and all(r["acceptance"]["state"] == "idle" for r in rows) and rows)
        assert {r["package_id"] for r in rows} == {BASE, CAND}
        parent.hard_interrupt("Stop the current host")
        thread.join(10)
        assert not thread.is_alive()
        assert [r["instruction_package"]["package_id"] for r in result["results"]] == [BASE, CAND]
        assert all(r["status"] == "interrupted" for r in result["results"])
        assert all("summary" not in r and "tool_trace" not in r for r in result["results"])
        assert host.snapshot() == []
        assert not parent._active_children
        with pytest.raises(ValueError, match="Stopped host"):
            host.run([BASE])
    finally:
        parent.hard_interrupt("test cleanup")
        thread.join(10)


@pytest.mark.parametrize("route", ["agent", "registry"])
def test_model_cannot_select_package_or_retention(native_parent, route):
    from tools.registry import registry
    parent, requests = native_parent
    observed = []
    def observe():
        child = parent._active_children[0]
        observed.append((child._instruction_package, getattr(child, "_delegate_acceptance", None)))
    requests.on_send = observe
    args = {"tasks": [{"goal": "ordinary delegation", "instruction_package_id": CAND,
                       "acceptance_idle_seconds": 300}],
            "instruction_package_id": CAND, "acceptance_idle_seconds": 300,
            "_commissioning": ([CAND], 300)}
    import contextvars
    from gateway.session_context import declare_stateless_channel
    def dispatch():
        declare_stateless_channel()
        return (parent._dispatch_delegate_task(args) if route == "agent"
                else registry.dispatch("delegate_task", args, parent_agent=parent))
    raw = contextvars.copy_context().run(dispatch)
    result = json.loads(raw)
    assert result["results"][0]["status"] == "completed"
    assert "native_child_session_id" not in result["results"][0]
    assert observed and all(pair == (None, None) for pair in observed)
    assert not parent._active_children
    schema = registry.get_schema("delegate_task")
    assert schema is not None
    assert "instruction_package_id" not in json.dumps(schema)
    assert "acceptance_idle_seconds" not in json.dumps(schema)
    assert "_commissioning" not in json.dumps(schema)
