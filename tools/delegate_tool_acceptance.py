"""Opt-in acceptance window on the existing child worker, not a detached agent.

The commissioning host keeps the normal delegation future open while inspecting
an idle child. Native owner control admits one compaction and one continuation.
Expiry and Stop use the ordinary finally-path teardown. No model-facing spawn
option or independent scheduler.
"""
from __future__ import annotations

import threading
import time


class AcceptanceWindow:
    def __init__(self, seconds: float):
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 0 < seconds <= 300:
            raise ValueError("acceptance_idle_seconds must be a number in (0, 300]")
        self.seconds = seconds
        self.condition = threading.Condition()
        self.state = "running"
        self.deadline: float | None = None
        self.command = None
        self.compaction = None
        self.reason = None
        self.cancelled = False  # Delegation lifetime, never reset at a turn boundary.

    def cancel(self):
        with self.condition:
            self.cancelled = True
            self.condition.notify_all()

    def snapshot(self):
        with self.condition:
            return {"state": self.state, "compaction": self.compaction, "reason": self.reason}

    def request(self, action, message, child):
        with self.condition:
            if (self.state != "idle" or self.deadline is None or time.monotonic() >= self.deadline
                    or self.cancelled):
                return {"error": "Acceptance child is not idle or its window has ended."}
            if action == "compact" and self.compaction is not None:
                return {"error": "The acceptance compaction attempt has already been consumed."}
            if action == "continue" and (
                not self.compaction or self.compaction["status"] not in {"committed", "adopted"}
                or not isinstance(message, str) or not message.strip()
            ):
                return {"error": "Continuation needs native compaction evidence and a non-empty message."}
            self.command = (action, message)
            self.state = "queued"
            self.condition.notify_all()
            return {"status": "queued", "action": action, "subagent_id": child._subagent_id}

    def run(self, run, result):
        if not result.get("completed") or result.get("failed") or result.get("interrupted") or result.get("error"):
            return result
        late = run.close_steering()
        if late:
            self._merge_pending_steer(result, late)
        with self.condition:
            self.deadline = time.monotonic() + self.seconds
            self.state = "idle"
        try:
            while True:
                with self.condition:
                    if self.cancelled:
                        self.reason = "interrupted"
                        result["interrupted"] = True
                        break
                    if time.monotonic() >= self.deadline:
                        self.reason = "expired"
                        break
                    if self.command is None:
                        self.condition.wait(timeout=min(0.1, self.deadline - time.monotonic()))
                        continue
                    # Command admission and sticky Stop publication share this lock.
                    # Once admitted, native interrupt handles Stop during the turn;
                    # never hold this lock across model/tool work.
                    action, message = self.command
                    self.command = None
                    self.state = action
                if action == "compact":
                    outcome = self._compact(run, result)
                    with self.condition:
                        self.compaction = outcome
                        self.state = "idle"
                    continue
                if action == "continue":
                    previous_calls = result.get("api_calls", 0)
                    pending_steer = result.get("pending_steer")
                    result = run.child.run_conversation(
                        user_message=message, conversation_history=result["messages"],
                        task_id=run.child_task_id, stream_callback=run.relay_text,
                    )
                    self._merge_pending_steer(result, pending_steer, prepend=True)
                    result["api_calls"] = previous_calls + result.get("api_calls", 0)
                    self.reason = "interrupted" if result.get("interrupted") else "continued"
                    break
                self.reason = "released"
                break
        finally:
            with self.condition:
                if self.cancelled:
                    result["interrupted"] = True
                    self.reason = "interrupted"
                self.state = "closed"
            result["acceptance"] = self.snapshot()
        return result

    @staticmethod
    def _merge_pending_steer(result, text, *, prepend=False):
        if text:
            existing = result.get("pending_steer")
            parts = [text, existing] if prepend else [existing, text]
            result["pending_steer"] = "\n".join(part for part in parts if part)

    def _compact(self, run, result):
        from agent.conversation_compression import CompressionCommitFence

        child = run.child
        old_session_id = child.session_id
        fence = CompressionCommitFence()
        deadline = self.deadline
        if deadline is None:
            raise RuntimeError("Acceptance compaction requires an active window")
        fence.set_total_ceiling_seconds(max(0.001, deadline - time.monotonic()))
        try:
            # The native fence owns cancellation/commit admission. Passing it also
            # avoids the host wrapper's automatic stall-fallback retry: one command,
            # one attempt. Never infer success from smaller message/token counts.
            messages, _ = child._compress_context(
                result["messages"], "", force=True, task_id=run.child_task_id, commit_fence=fence,
            )
            evidence = None
            status = "not_committed"
            if child._last_compression_attempt_in_place is True:
                status, evidence = "committed", "native_in_place_commit"
            elif child.session_id != old_session_id and child._session_db is not None:
                # The generic chain walker excludes subagent-source rows. Verify
                # the durable native rotation edge directly for delegated children.
                parent_row = child._session_db.get_session(old_session_id)
                child_row = child._session_db.get_session(child.session_id)
                if (parent_row and parent_row.get("end_reason") == "compression"
                        and child_row and child_row.get("parent_session_id") == old_session_id):
                    status, evidence = "adopted", "native_compression_lineage"
            if evidence:
                result["messages"] = messages
                child._session_messages = messages
            return {"status": status, "evidence": evidence, "session_id": child.session_id}
        except Exception as exc:
            # No retry, no exception text crossing the owner-visible boundary.
            return {"status": "failed", "evidence": None, "error_type": type(exc).__name__}
