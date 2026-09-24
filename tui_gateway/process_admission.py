"""Process admission owned exclusively by the inherited launch channel.

The lock linearizes lease acquisition with pre-executor reservations. Inventory
never holds it while acquiring a session/registry lock (turn claims hold history
locks). A receipt is valid only while its exact channel lease remains installed.
"""
import secrets
import threading
import sys

_lock = threading.RLock()
_lease = None
_active = 0
_runtime = None

# These can settle existing work, not create new sessions/turns. They still count
# as admitted work while executing; a public session lease never owns this gate.
_DRAIN = frozenset({
    'session.interrupt', 'approval.respond', 'clarify.respond', 'sudo.respond',
    'secret.respond', 'subagent.interrupt', 'runtime.quiesce',
    'runtime.quiesce.cancel', 'subagent.list', 'subagent.tail',
    'session.history', 'session.usage',
})


def revoke_after_fork():
    """The child has neither the owner thread nor authority over its parent."""
    global _lock, _lease, _active, _runtime
    _lock = threading.RLock()
    _lease, _active, _runtime = None, 0, None


def bind_runtime(server):
    global _runtime
    _runtime = server


def is_closed():
    # Background turn claims read this under their existing history_lock. The
    # sampler takes those same locks after closing admission before proving idle.
    return _lease is not None


def admit(method=None):
    global _active
    with _lock:
        if _lease is not None and method not in _DRAIN:
            return False
        _active += 1
        return True


def release():
    global _active
    with _lock:
        _active -= 1
        assert _active >= 0


def cancel(generation):
    global _lease
    with _lock:
        if _lease is not None:
            if _lease['generation'] != generation:
                return {'error': 'foreign-lease'}
            _lease = None
        return {'admission': 'open'}


def _inventory():
    server = _runtime
    if server is None:
        raise RuntimeError('native runtime not ready')
    from tools import async_delegation, delegate_tool_registry
    from tools.process_registry import process_registry
    counts = dict(turns=0, children=0, delegations=0, processes=0, notifications=0)
    with server._sessions_lock:
        for session in server._sessions.values():
            with session['history_lock']:
                counts['turns'] += int(any(session.get(k) for k in (
                    'running', '_runtime_rpc_active', 'queued_prompt', 'queued_prompts',
                    '_auto_continue_scheduled', '_closing', '_busy_interrupt_pending'))
                    or any(t is not None and t.is_alive() for t in (
                        session.get('_run_thread'), session.get('_agent_build_thread'))))
                if session.get('_finalized'):
                    raise RuntimeError('retired session still registered')
    with delegate_tool_registry._active_subagents_lock:
        counts['children'] = len(delegate_tool_registry._active_subagents)
    counts['delegations'] = async_delegation.active_count()
    counts['processes'] = sum(p.get('status') not in {'completed', 'exited', 'killed', 'failed'}
                              for p in process_registry.list_sessions())
    import psutil
    counts['os_children'] = len(psutil.Process().children(recursive=True))
    counts['notifications'] = process_registry.completion_queue.qsize()
    # A completion may be durable but not (yet) queued, or claimed by a poller.
    # Read only: sampling must not migrate or create profile state.
    import sqlite3
    from contextlib import closing
    path = async_delegation._db_path()
    counts['durable_notifications'] = 0
    if path.exists():
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=0.1)) as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='async_delegations'").fetchone()
            if exists:
                counts['durable_notifications'] = db.execute(
                    "SELECT COUNT(*) FROM async_delegations WHERE delivery_state='pending'"
                ).fetchone()[0]
    with server._prompt_lock:
        counts['notifications'] += len(server._pending)
    return counts


def quiesce(generation):
    global _lease
    with _lock:
        if _lease is None:
            _lease = dict(generation=generation, nonce=secrets.token_hex(32))
        elif _lease['generation'] != generation:
            return {'error': 'foreign-lease'}
        lease = _lease
        active = _active
    try:
        counts = _inventory()
        status = 'busy' if active or any(counts.values()) else 'idle'
        reason = None
        # ASGI requests/new non-RPC sockets now reserve this gate, but that
        # does not fence input on already-open or detached PTYs, detached HTTP
        # workers, multiplexed cron dispatch, hosted-room recovery/peer work,
        # or lifespan maintenance/startup threads. Do not mistake transport
        # admission for complete producer participation: an empty TUI registry
        # is still not permission to replace a serve host.
        if 'hermes_cli.web_server' in sys.modules:
            status, reason = 'unknown', 'unfenced-serve-producers'
    except Exception:
        counts, status, reason = {}, 'unknown', 'inventory-unavailable'
    with _lock:
        if _lease is not lease:
            return {'error': 'stale-lease'}
        counts['rpc'] = max(active, _active)
        if status == 'idle' and counts['rpc']:
            status = 'busy'
        return dict(scope='process', status=status, admission='closed',
                    generation=lease['generation'], nonce=lease['nonce'],
                    inventory=counts, inventory_complete=status != 'unknown',
                    **({'reason': reason} if reason else {}))
