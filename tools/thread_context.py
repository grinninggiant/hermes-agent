"""Propagate agent-turn context into worker threads that dispatch Hermes tools.

A bare ``threading.Thread`` / ``ThreadPoolExecutor`` worker starts with an empty
``contextvars.Context`` and no thread-local approval/sudo callbacks or review whitelist.
Tool dispatch then loses approval ContextVars (gateway sessions can auto-approve dangerous
commands), CLI callbacks (``prompt_dangerous_approval`` cannot reach the user,
GHSA-qg5c-hvr5-hjgr), and background-review tool restrictions.
Call :func:`propagate_context_to_thread` **on the parent thread** (it snapshots at call time) and
use the result as the worker target; callbacks are installed for the worker's lifetime and
always cleared on exit.
"""

from __future__ import annotations

import contextvars
import logging
from typing import Callable

logger = logging.getLogger(__name__)


def _callback_api():
    """Resolve the terminal_tool callback getters/setters (lazy: terminal_tool imports
    tools.approval at load, so a top-level import risks a cycle for tools.approval callers)."""
    from tools import terminal_tool as tt

    return (tt._get_approval_callback, tt._get_sudo_password_callback,
            tt.set_approval_callback, tt.set_sudo_password_callback)


def propagate_context_to_thread(target: Callable) -> Callable:
    """Wrap *target* to run with the *current* thread's ContextVars and approval/sudo callbacks.

    Fail-closed: if callback installation raises they stay ``None`` — dangerous commands are then
    denied by ``prompt_dangerous_approval`` and the gateway approval queue blocks.
    """
    ctx = contextvars.copy_context()
    from hermes_cli.plugins import _thread_tool_whitelist, set_thread_tool_whitelist, clear_thread_tool_whitelist
    allowed = getattr(_thread_tool_whitelist, "allowed", None)
    whitelist = (
        (set(allowed), getattr(_thread_tool_whitelist, "fmt", "Tool '{tool_name}' denied"))
        if allowed is not None else None
    )
    # (setter, parent callback) pairs; None when the callback API could not be captured.
    installs = None
    try:
        get_approval, get_sudo, set_approval, set_sudo = _callback_api()
        installs = ((set_approval, get_approval()), (set_sudo, get_sudo()))
    except Exception:
        logger.debug("Could not capture parent approval/sudo callbacks", exc_info=True)

    def _runner(*args, **kwargs):
        def _inner():
            try:
                if whitelist is not None:
                    set_thread_tool_whitelist(*whitelist)
                if installs is not None:
                    try:
                        for setter, cb in installs:
                            if cb is not None:
                                setter(cb)
                    except Exception:
                        logger.debug("Failed to install propagated approval/sudo callbacks; "
                                     "dangerous-command approval will fail closed", exc_info=True)
                return target(*args, **kwargs)
            finally:
                try:
                    if whitelist is not None:
                        clear_thread_tool_whitelist()
                finally:
                    if installs is not None:
                        try:
                            for setter, _cb in installs:
                                setter(None)
                        except Exception:
                            logger.debug("Failed to clear propagated approval/sudo callbacks",
                                         exc_info=True)

        return ctx.run(_inner)

    return _runner
