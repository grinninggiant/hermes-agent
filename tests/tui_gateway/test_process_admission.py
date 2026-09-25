"""Exercise inherited owner sockets and the native dispatch queue, without models."""
import hashlib
import hmac
import json
import secrets
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def no_passive_network(_isolate_hermes_home):
    # Control an unrelated real producer rather than hiding its child processes
    # from the inventory. No user config is touched (canonical runner temp home).
    from hermes_constants import get_hermes_home
    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / 'config.yaml').write_text('updates:\n  check: false\n', encoding='utf-8')


class Owner:
    def __init__(self):
        from tui_gateway.process_owner import serve_owner_channel
        self.socket, child = socket.socketpair()
        self.socket.settimeout(5)
        self.thread = threading.Thread(target=serve_owner_channel, args=(child,), daemon=True)
        self.thread.start()
        self.stream = self.socket.makefile('rwb', buffering=0)
        self.key = secrets.token_bytes(32)
        self.stream.write(json.dumps({'capability': self.key.hex()}).encode() + b'\n')
        self.generation = json.loads(self.stream.readline())['generation']
        self.seq = 0

    def request(self, operation, generation=None):
        self.seq += 1
        generation = generation or self.generation
        tag = hmac.new(self.key, f'{generation}:{self.seq}:{operation}'.encode(), hashlib.sha256).hexdigest()
        self.stream.write(json.dumps(dict(generation=generation, seq=self.seq, operation=operation, tag=tag)).encode() + b'\n')
        return json.loads(self.stream.readline())

    def close(self):
        self.stream.close()
        self.socket.close()
        self.thread.join(5)
        assert not self.thread.is_alive()


def test_owner_fences_preexecutor_and_all_sessions(monkeypatch):
    from tui_gateway import server
    replies = []
    transport = SimpleNamespace(write=replies.append)
    sessions = {sid: dict(session_key=sid, history_lock=threading.RLock(), transport=transport)
                for sid in ('one', 'two')}
    monkeypatch.setattr(server, '_sessions', sessions)
    entered, release = threading.Event(), threading.Event()
    pool = ThreadPoolExecutor(max_workers=1)
    def occupy():
        entered.set()
        assert release.wait(5)
    blocker = pool.submit(occupy)
    assert entered.wait(5)
    monkeypatch.setattr(server, '_pool', pool)
    monkeypatch.setattr(server, '_LONG_HANDLERS', frozenset({'test.owner.work'}))
    monkeypatch.setitem(server._methods, 'test.owner.work', lambda rid, p: server._ok(rid, {'ran': p['session_id']}))
    owner = Owner()
    other = Owner()
    try:
        assert server.dispatch(dict(id=1, method='test.owner.work', params={'session_id': 'one'}), transport) is None
        proof = owner.request('quiesce')
        assert proof['admission'] == 'closed'
        assert proof['status'] == 'busy'
        assert proof['inventory']['rpc'] == 1
        for sid in ('one', 'two', 'missing'):
            response = server.dispatch(dict(id=2, method='test.owner.work', params={'session_id': sid}), transport)
            assert response['error']['code'] == 4093
        assert other.request('cancel')['error'] == 'foreign-lease'
        release.set()
        blocker.result(5)
        pool.shutdown(wait=True)
        assert replies == [server._ok(1, {'ran': 'one'})]
        idle = owner.request('quiesce')
        assert idle['status'] == 'idle', json.dumps(idle['inventory'])
        assert idle['generation'] == owner.generation
        assert idle['nonce'] == proof['nonce']
        assert idle['scope'] == 'process'
        owner.close()
        assert other.request('quiesce')['nonce'] != proof['nonce']
        assert other.request('cancel')['admission'] == 'open'
        assert server.handle_request(dict(id=3, method='test.owner.work', params={'session_id': 'two'}))['result']['ran'] == 'two'
    finally:
        release.set()
        pool.shutdown(wait=True)
        if owner.thread.is_alive():
            owner.close()
        other.close()


def test_real_children_delegations_and_processes(monkeypatch, tmp_path):
    import subprocess
    import sys
    from tui_gateway import server
    from tools import async_delegation, delegate_tool_registry
    from tools.process_registry import process_registry
    monkeypatch.setattr(server, '_sessions', {})
    entered, release = threading.Event(), threading.Event()
    def runner():
        delegate_tool_registry._register_subagent({'subagent_id': 'owner-fixture-child'})
        entered.set()
        try:
            assert release.wait(10)
            return {'status': 'completed', 'result': 'fixture finished without a model'}
        finally:
            delegate_tool_registry._unregister_subagent('owner-fixture-child')
    handle = async_delegation.dispatch_async_delegation(
        goal='local test', context=None, toolsets=None, role='leaf', model=None,
        session_key='retired-fixture-session', runner=runner)
    assert handle['status'] == 'dispatched'
    assert entered.wait(5)
    # A real registered local process, released through stdin; never killed by quiesce.
    proc = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.readline()'],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    tracked = process_registry.adopt_local(proc, command='local fence fixture', cwd=str(tmp_path),
                                          session_key='other-session', notify_on_complete=False)
    owner = Owner()
    try:
        proof = owner.request('quiesce')
        assert proof['status'] == 'busy'
        assert proof['inventory']['children'] >= 1
        assert proof['inventory']['delegations'] >= 1
        assert proof['inventory']['processes'] >= 1
        assert proof['inventory']['os_children'] >= 1
        assert proc.poll() is None
        assert not release.is_set()
        assert owner.request('shutdown') == {'error': 'unsupported'}
        assert proc.poll() is None
        release.set()
        event = process_registry.completion_queue.get(timeout=5)
        assert event['delegation_id'] == handle['delegation_id']
        async_delegation._executor.shutdown(wait=True)
        monkeypatch.setattr(async_delegation, '_executor', None)
        # Dequeued but not delivered remains durable pending work, not idle.
        proof = owner.request('quiesce')
        assert proof['inventory']['durable_notifications'] >= 1
        assert async_delegation.mark_completion_delivered(handle['delegation_id'])
        proc.stdin.write('\n')
        proc.stdin.flush()
        proc.wait(timeout=5)
        tracked._reader_thread.join(5)
        assert owner.request('quiesce')['status'] == 'idle'
    finally:
        release.set()
        if proc.poll() is None:
            proc.stdin.write('\n')
            proc.stdin.flush()
            proc.wait(timeout=5)
        proc.stdin.close()
        tracked._reader_thread.join(5)
        owner.close()


def test_disconnect_reconnect_and_session_proof_are_not_process_authority(monkeypatch):
    from tui_gateway import server
    transport = SimpleNamespace(write=lambda frame: None)
    session = dict(session_key='one', history_lock=threading.RLock(), transport=transport)
    monkeypatch.setattr(server, '_sessions', {'one': session})
    owner = Owner()
    foreign = Owner()
    try:
        proof = owner.request('quiesce')
        # A foreign channel disconnect must not clear the owner's fence.
        foreign.close()
        assert owner.request('quiesce')['nonce'] == proof['nonce']
        session_proof = server.dispatch(dict(id=1, method='runtime.quiesce', params={'session_id': 'one'}), transport)['result']
        assert server.dispatch(dict(id=2, method='runtime.quiesce.cancel', params={
            'session_id': 'one', 'nonce': session_proof['nonce'],
            'generation': session_proof['generation']}), transport)['result']['admission'] == 'open'
        assert owner.request('quiesce')['nonce'] == proof['nonce']
        owner.close()
        replacement = Owner()
        try:
            assert replacement.generation != proof['generation']
            assert replacement.request('quiesce', generation=proof['generation']) == {'error': 'unauthorized'}
            replacement.seq -= 1  # rejection did not consume the wire sequence
            fresh = replacement.request('quiesce')
            assert fresh['nonce'] != proof['nonce']
            assert fresh['generation'] == replacement.generation
            assert replacement.request('cancel')['admission'] == 'open'
        finally:
            replacement.close()
    finally:
        if owner.thread.is_alive():
            owner.close()
        if foreign.thread.is_alive():
            foreign.close()


def test_preexecutor_cancel_and_submission_failure_release_accounting(monkeypatch):
    from tui_gateway import server, process_admission
    from concurrent.futures import Future
    replies = []
    transport = SimpleNamespace(write=replies.append)
    monkeypatch.setattr(server, '_sessions', {})
    monkeypatch.setattr(server, '_LONG_HANDLERS', frozenset({'test.queued'}))
    future = Future()
    monkeypatch.setattr(server, '_pool', SimpleNamespace(submit=lambda work: future))
    owner = Owner()
    try:
        assert server.dispatch(dict(id=1, method='test.queued'), transport) is None
        assert owner.request('quiesce')['inventory']['rpc'] == 1
        assert future.cancel()
        assert owner.request('quiesce')['status'] == 'idle'
        assert not replies
        owner.request('cancel')
        def reject(work):
            raise RuntimeError('executor stopped')
        monkeypatch.setattr(server, '_pool', SimpleNamespace(submit=reject))
        with pytest.raises(RuntimeError, match='executor stopped'):
            server.dispatch(dict(id=2, method='test.queued'), transport)
        assert process_admission._active == 0
        assert owner.request('quiesce')['status'] == 'idle'
    finally:
        owner.close()


def test_inventory_race_cannot_issue_stale_or_serve_idle_receipt(monkeypatch):
    from tui_gateway import server, process_admission
    import sys
    monkeypatch.setattr(server, '_sessions', {})
    entered, release = threading.Event(), threading.Event()
    inventory = process_admission._inventory
    def blocked_inventory():
        entered.set()
        assert release.wait(5)
        return inventory()
    monkeypatch.setattr(process_admission, '_inventory', blocked_inventory)
    result = []
    thread = threading.Thread(target=lambda: result.append(process_admission.quiesce('old')))
    thread.start()
    try:
        assert entered.wait(5)
        assert not process_admission.admit('prompt.submit')
        assert process_admission.cancel('old') == {'admission': 'open'}
        monkeypatch.setattr(process_admission, '_inventory', inventory)
        fresh = process_admission.quiesce('new')
        assert fresh['status'] == 'idle'
        release.set()
        thread.join(5)
        assert result == [{'error': 'stale-lease'}]
        monkeypatch.setitem(sys.modules, 'hermes_cli.web_server', SimpleNamespace())
        receipt = process_admission.quiesce('new')
        assert receipt['status'] == 'unknown'
        assert receipt['reason'] == 'unfenced-serve-producers'
        assert not receipt['inventory_complete']
        assert receipt['admission'] == 'closed'
    finally:
        release.set()
        thread.join(5)
        process_admission.cancel('old')
        process_admission.cancel('new')


def test_real_pending_notifications_and_unknown_inventory(monkeypatch):
    from tui_gateway import server
    from tools.process_registry import process_registry
    monkeypatch.setattr(server, '_sessions', {})
    owner = Owner()
    try:
        event = {'type': 'completion', 'session_id': 'process-admission-fixture'}
        process_registry.completion_queue.put(event)
        try:
            receipt = owner.request('quiesce')
            assert receipt['status'] == 'busy'
            assert receipt['inventory']['notifications'] >= 1
        finally:
            assert process_registry.completion_queue.get_nowait() is event
        assert owner.request('quiesce')['status'] == 'idle'
        def unavailable(*a, **kw):
            raise RuntimeError('inventory unavailable')
        monkeypatch.setattr(process_registry, 'list_sessions', unavailable)
        receipt = owner.request('quiesce')
        assert receipt['status'] == 'unknown'
        assert receipt['admission'] == 'closed'
        assert owner.request('shutdown') == {'error': 'unsupported'}
    finally:
        owner.close()


def test_server_requests_remain_busy_through_response_callback(monkeypatch):
    from tui_gateway import process_admission, server, server_requests
    monkeypatch.setattr(server, '_sessions', {})
    monkeypatch.setattr(server_requests, '_write', lambda frame: None)
    entered, finish = threading.Event(), threading.Event()
    def completed(result):
        assert result == {'choice': 'deny'}
        entered.set()
        assert finish.wait(5)
    request = server_requests.ServerRequest('fixture', 'approval',
                                           {'request_id': 'fixture'}, on_result=completed)
    server_requests._register(request)
    owner = Owner()
    response = threading.Thread(target=lambda: server.dispatch(
        {'jsonrpc': '2.0', 'id': request.id, 'result': {'choice': 'deny'}},
        SimpleNamespace(write=lambda frame: None)))
    try:
        pending = owner.request('quiesce')
        assert pending['status'] == 'busy'
        assert pending['inventory']['notifications'] == 1
        response.start()
        assert entered.wait(5)
        draining = owner.request('quiesce')
        assert draining['status'] == 'busy'
        assert draining['inventory']['notifications'] == 0
        assert draining['inventory']['rpc'] == 1
        finish.set()
        response.join(5)
        assert not response.is_alive()
        assert owner.request('quiesce')['status'] == 'idle'
    finally:
        finish.set()
        if response.ident is not None:
            response.join(5)
        owner.close()
    assert process_admission._active == 0


def test_shared_turn_gate_and_poller_preserve_work_behind_owner_fence(monkeypatch):
    from tui_gateway import server
    from tools.process_registry import process_registry
    from hermes_cli import backend_retirement
    retirement = backend_retirement.RetirementFence()
    monkeypatch.setattr(backend_retirement, 'retirement', retirement)
    session = dict(history_lock=threading.RLock(), queued_prompt={'text': 'pending'})
    monkeypatch.setattr(server, '_sessions', {'fixture': session})
    stop = threading.Event()
    stop.set()
    event = {'type': 'completion', 'session_id': 'fenced-poller'}
    process_registry.completion_queue.put(event)
    owner = Owner()
    try:
        assert owner.request('quiesce')['status'] == 'busy'
        with server._session_turn_admission(session) as admitted:
            assert not admitted
        assert not server._notif_claim_turn(session)
        assert not server._drain_queued_prompt(1, 'fixture', session)
        assert session['queued_prompt'] == {'text': 'pending'}
        server._notification_poller_scoped_loop(stop, 'fixture', session)
        assert process_registry.completion_queue.get_nowait() is event
        assert owner.request('cancel') == {'admission': 'open'}
        with server._session_turn_admission(session) as admitted:
            assert admitted
        session['_runtime_quiescence'] = {'nonce': 'fixture'}
        with server._session_turn_admission(session) as admitted:
            assert not admitted
        session.pop('_runtime_quiescence')
        retirement._preparing = True
        with server._session_turn_admission(session) as admitted:
            assert not admitted  # The upstream fence remains independently authoritative.
        assert retirement.active_count() == 0
    finally:
        owner.close()


@pytest.mark.parametrize('method', ['clarify.lock', 'request.answer'])
def test_current_request_settlement_drains_both_fences(monkeypatch, method):
    from tui_gateway import server, server_requests
    transport = SimpleNamespace(write=lambda frame: None)
    session = dict(session_key='fixture', transport=transport, history_lock=threading.RLock())
    monkeypatch.setattr(server, '_sessions', {'fixture': session})
    monkeypatch.setattr(server_requests, '_write', lambda frame: None)
    request = server_requests.ServerRequest('fixture', 'clarify', {'question': 'fixture'}, qids=['q0'])
    server_requests._register(request)
    def call(name, params):
        return server.dispatch({'id': 1, 'method': name, 'params': params}, transport)
    assert call('runtime.quiesce', {'session_id': 'fixture'})['result']['admission'] == 'closed'
    owner = Owner()
    try:
        assert owner.request('quiesce')['status'] == 'busy'
        params = ({'request_id': request.id, 'question_id': 'q0', 'answer': 'fixture'}
                  if method == 'clarify.lock' else {'id': request.id, 'result': {'answers': {'q0': 'fixture'}}})
        assert call(method, params)['result']['status'] == 'ok'
        assert request.answered and request.result == {'answers': {'q0': 'fixture'}}
        assert owner.request('quiesce')['status'] == 'idle'
        assert call('prompt.submit', {'session_id': 'fixture', 'text': 'blocked'})['error']['code'] == 4093
        assert call(method, params)['result']['status'] == 'expired'
    finally:
        server_requests.cancel('fixture')
        owner.close()
