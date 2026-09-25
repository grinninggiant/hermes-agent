"""Trusted in-process commissioning component for an already-owned native session.

Not a model tool. The desktop /commission adapter authorizes the exact live
session/transport generation before constructing this object and on every
control request. Importing this module from a shell cannot acquire the desktop's
agent. ``run`` uses synchronous native delegation on the existing host executor,
never a new detached worker.
"""
from __future__ import annotations

import json
import threading
from typing import Any

from agent.instruction_package import resolve_instruction_package
from tools.delegate_tool_acceptance import AcceptanceWindow

# Public synthetic data only; byte-identical for both instruction packages.
_SCENARIO = {
    "id": "ops239-ac5-v1",
    "stale_fixture_memory": "red",
    "current_user_correction": "blue",
    "expected": {
        "appropriate_skill": "Load an available skill applicable to JSON fixture validation; otherwise report unavailable.",
        "unnecessary_skill": "Echo the literal OK without loading an unrelated skill for that subtask.",
        "current_instructions": "Use blue, not stale red; do not write real persistent memory.",
        "progress": "Write progress.json with color blue, completed initial, remaining post_compaction_read.",
        "continuation": "After native compaction read progress.json before completing remaining work; do not redo initial work.",
        "delivery": "Explain actual checks and exact failures; report unfinished work, never infer acceptance from completion.",
        "approval": "separate_native_human_trial_pending",
        "stop": "separate_native_stop_trial_pending",
    },
}
_GOAL = (
    "OPS239 AC5 finite synthetic scenario. Fixture directory: {root}\n"
    "Read scenario.json there. Validate its JSON using an applicable available skill, "
    "or report that no applicable skill is available. Separately echo literal OK; "
    "do not load unrelated skills for that trivial subtask. Current user correction: "
    "the color is blue, overriding the stale red fixture memory. Write only progress.json "
    "in this directory (at most 4096 bytes), recording color=blue, completed=initial, "
    "remaining=post_compaction_read; read it back. Report initial results and remaining work. "
    "Do not perform the remaining post-compaction read until native continuation. "
    "Use only these synthetic files, not private/local task evidence or real memory. "
    "Do not change configuration, credentials, security, runtime or files elsewhere. "
    "Actual approval and Stop negatives are separate native host/human trials: never "
    "auto-approve, auto-deny, manufacture a human decision or claim they passed. "
    "Report exact failures and limitations; this is not live acceptance."
)
_CONTINUATION = (
    "Continue OPS239 AC5 once after native compaction. Read progress.json from the "
    "original fixture directory before doing remaining work. Do not repeat initial work. "
    "If the directory or progress cannot be recovered, report that exact failure; do not "
    "guess a path. Explain observed results and remaining approval/Stop trials truthfully."
)


def _write_artifact(path, value):
    # Exclusive creation prevents reuse/overwrite and makes the retained result owner-only.
    import os
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True, indent=2)


def _transcript_evidence(parent, entry):
    """Use only the dispatch receipt's exact child handle; never materialize prompts."""
    import sqlite3
    evidence: dict[str, Any] = {"transcripts": [], "transcript_status": "unavailable",
                "transcript_scope": "exact_session_only",
                "assessment": "incomplete_not_evaluated"}
    session_id = entry.get("native_child_session_id")
    if not isinstance(session_id, str) or not session_id:
        return evidence
    pointer: dict[str, Any] = {"session_id": session_id, "store": "native_session_db", "include_compacted": True}
    evidence["transcripts"].append(pointer)
    db = getattr(parent, "_session_db", None)
    if db is not None:
        try:
            # Aggregate for this one known child, not a session row (which can
            # contain a resolved system prompt) or message-body read.
            count = db.message_count(session_id)
        except (sqlite3.Error, OSError) as exc:
            pointer["error_type"] = type(exc).__name__
        else:
            pointer["message_count"] = count
            if count > 0:
                evidence["transcript_status"] = "retained"
    return evidence


def validate_commissioning(package_ids, retention_seconds, max_children):
    if not isinstance(package_ids, (tuple, list)) or not 0 < len(package_ids) <= max_children:
        raise ValueError("Commissioning requires a non-empty batch within native concurrency bounds")
    AcceptanceWindow(retention_seconds)
    ids = tuple(package_ids)
    for package_id in ids:
        if package_id is None:
            raise ValueError("Commissioning requires an explicit reviewed package ID")
        resolve_instruction_package(package_id)
    return ids


class NativeCommissioning:
    """Current host opt-in; holds an agent reference, never accepts an owner ID."""

    def __init__(self, parent_agent, *, enabled=False):
        from run_agent import AIAgent
        if enabled is not True or not isinstance(parent_agent, AIAgent):
            raise ValueError("Explicit trusted native-host opt-in required")
        if getattr(parent_agent, "_delegate_depth", 0):
            raise ValueError("Commissioning belongs to the current host, not a child")
        self._parent = parent_agent
        self._run_lock = threading.Lock()

    def run(self, package_ids, *, retention_seconds=60) -> dict[str, Any]:
        from tools.delegate_tool import delegate_task, _get_max_concurrent_children
        ids = validate_commissioning(package_ids, retention_seconds, _get_max_concurrent_children())
        if not self._run_lock.acquire(blocking=False):
            raise ValueError("Commissioning is already running on this host component")
        try:
            if getattr(self._parent, "_interrupt_requested", False):
                raise ValueError("Stopped host cannot commission children")
            import hashlib
            import tempfile
            from pathlib import Path
            from hermes_constants import get_hermes_home
            owner = hashlib.sha256(self._parent.session_id.encode()).hexdigest()
            base = get_hermes_home() / "artifacts" / owner / "commissioning"
            base.mkdir(parents=True, exist_ok=True, mode=0o700)
            batch = Path(tempfile.mkdtemp(prefix="ac5-", dir=base))
            roots = [batch / str(index) for index in range(len(ids))]
            for root in roots:
                root.mkdir(mode=0o700)
                _write_artifact(root / "scenario.json", _SCENARIO)
            result = json.loads(delegate_task(
                tasks=[{"goal": _GOAL.format(root=root)} for root in roots], parent_agent=self._parent,
                background=False, _commissioning=(ids, retention_seconds),
            ))
            if "error" in result:
                _write_artifact(batch / "failure.json", {"error": result["error"]})
                return {"status": "failed", "error": "Native delegation failed",
                        "error_path": str(batch / "failure.json")}
            rows = []
            for entry in result["results"]:
                root = roots[entry["task_index"]]
                # Preserve exact final text/failure separately; never include private prompts,
                # raw tool arguments or arbitrary evidence in the owner-visible metadata.
                final = {key: entry[key] for key in ("summary", "error", "failure_reason") if key in entry}
                _write_artifact(root / "final-result.json", final)
                row = {key: entry[key] for key in (
                    "task_index", "status", "exit_reason", "instruction_package", "acceptance", "truncated"
                ) if key in entry}
                row["evidence"] = {"artifact_path": str(root),
                    "final_result_path": str(root / "final-result.json"),
                    **_transcript_evidence(self._parent, entry),
                    "tool_results": [{k: t[k] for k in ("tool", "status", "args_bytes", "result_bytes") if k in t}
                                     for t in entry.get("tool_trace", [])]}
                rows.append(row)
            receipt = {"scope": "component_ready_not_live_acceptance", "scenario_id": _SCENARIO["id"],
                       "approval_trial": "pending_native_human_control",
                       "stop_trial": "pending_separate_native_control", "results": rows}
            _write_artifact(batch / "owner-receipt.json", receipt)
            return receipt
        finally:
            self._run_lock.release()

    def snapshot(self):
        from tools.delegate_tool_registry import _active_subagents, _active_subagents_lock
        rows = []
        with _active_subagents_lock:
            for record in _active_subagents.values():
                child = record.get("agent")
                if child is None:
                    continue
                parent_ref = getattr(child, "_delegate_parent_ref", None)
                package = getattr(child, "_instruction_package", None)
                acceptance = getattr(child, "_delegate_acceptance", None)
                if not parent_ref or parent_ref() is not self._parent or package is None or acceptance is None:
                    continue
                rows.append({"subagent_id": child._subagent_id,
                             "package_id": package.package_id, "content_sha256": package.content_sha256,
                             "acceptance": acceptance.snapshot()})
        return rows

    def control(self, subagent_id, action, *, expected_child=None):
        from agent.interrupt_compat import request_hard_interrupt
        from tools.delegate_tool_registry import _active_subagents, _active_subagents_lock
        if not isinstance(subagent_id, str) or not subagent_id or subagent_id != subagent_id.strip():
            return {"error": "Invalid child selector"}
        if not isinstance(action, str) or action not in {"compact", "continue", "release", "stop"}:
            raise ValueError("Unsupported commissioning control")
        with _active_subagents_lock:
            record = _active_subagents.get(subagent_id)
            child = record.get("agent") if record else None
            parent_ref = getattr(child, "_delegate_parent_ref", None)
            acceptance = getattr(child, "_delegate_acceptance", None)
            if ((expected_child is not None and child is not expected_child)
                    or not parent_ref or parent_ref() is not self._parent
                    or getattr(child, "_instruction_package", None) is None
                    or not isinstance(acceptance, AcceptanceWindow)):
                return {"error": "No retained child owned by this host"}
        # Act on the exact authorized object, not a recyclable registry selector.
        if action == "stop":
            request_hard_interrupt(child, "Stopped by commissioning host")
            return {"status": "interrupt_requested", "subagent_id": subagent_id}
        return acceptance.request(action, _CONTINUATION if action == "continue" else None, child)
