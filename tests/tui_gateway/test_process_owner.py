"""Private inherited channel authority, independent of HTTP/session credentials."""
import hashlib
import hmac
import json
import secrets
import socket
import threading
import subprocess
import sys

import pytest


def test_private_channel_generation_replay_and_fail_closed():
    from tui_gateway.process_owner import serve_owner_channel

    def connect():
        parent, child = socket.socketpair()
        parent.settimeout(3)
        thread = threading.Thread(target=serve_owner_channel, args=(child,), daemon=True)
        thread.start()
        stream = parent.makefile('rwb', buffering=0)
        key = secrets.token_bytes(32)
        stream.write(json.dumps({'capability': key.hex()}).encode() + b'\n')
        hello = json.loads(stream.readline())
        return parent, stream, thread, key, hello['generation']

    def request(stream, key, generation, seq, operation):
        tag = hmac.new(key, f'{generation}:{seq}:{operation}'.encode(), hashlib.sha256).hexdigest()
        frame = dict(generation=generation, seq=seq, operation=operation, tag=tag)
        stream.write(json.dumps(frame).encode() + b'\n')
        result = json.loads(stream.readline())
        assert key.hex() not in json.dumps(result)
        assert tag not in json.dumps(result)
        return result

    p, s, t, key, gen = connect()
    p2, s2, t2, key2, gen2 = connect()
    try:
        assert gen != gen2
        assert request(s, key2, gen, 1, 'status') == {'error': 'unauthorized'}
        assert request(s, key, gen, 1, 'status')['authority'] == 'launch-channel'
        assert request(s, key, gen, 1, 'status') == {'error': 'unauthorized'}
        assert request(s2, key, gen, 2, 'status') == {'error': 'unauthorized'}
        receipt = request(s, key, gen, 2, 'quiesce')
        assert receipt['status'] == 'unknown'  # runtime has not bound yet
        assert receipt['admission'] == 'closed'
        assert receipt['generation'] == gen
        assert receipt['reason'] == 'inventory-unavailable'
        assert request(s, key, gen, 3, 'cancel') == {'admission': 'open'}
        assert request(s, key, gen, 4, 'shutdown') == {'error': 'unsupported'}
    finally:
        for parent, stream in [(p, s), (p2, s2)]:
            stream.close()
            parent.close()
        t.join(3)
        t2.join(3)
    assert not t.is_alive() and not t2.is_alive()


@pytest.mark.platforms("macos")
def test_owner_endpoint_fork_and_exec_are_revoked_without_affecting_parent():
    # Isolate fork from pytest/plugin threads. No model, live server or signals.
    code = r"""
import errno, json, os, socket, subprocess, sys, threading
from tui_gateway.process_owner import serve_owner_channel
from tui_gateway import process_admission
parent, channel = socket.socketpair()
parent.settimeout(3)
thread = threading.Thread(target=serve_owner_channel, args=(channel,), daemon=True)
thread.start()
parent.sendall(json.dumps({'capability': '01' * 32}).encode() + b'\n')
assert json.loads(parent.recv(4096))['generation']
fd = channel.fileno()
assert not os.get_inheritable(fd)
process_admission._lease = {'generation': 'fixture', 'nonce': 'fixture'}
pid = os.fork()
if pid == 0:
    try:
        assert channel.fileno() == -1
        assert not process_admission.is_closed()
        try:
            os.fstat(fd)
        except OSError as exc:
            assert exc.errno == errno.EBADF
        else:
            raise AssertionError('owner fd survived fork')
        os._exit(0)
    except BaseException:
        os._exit(7)
assert os.waitpid(pid, 0)[1] == 0
assert channel.fileno() == fd
assert process_admission.is_closed()
parent.sendall(b'{}\n')
assert json.loads(parent.recv(4096)) == {'error': 'unauthorized'}
# close_fds=False exercises CLOEXEC, rather than subprocess's fd cleanup.
probe = 'import os,sys; fd=int(sys.argv[1]); ' + "\ntry: os.fstat(fd)\nexcept OSError: sys.exit(0)\nelse: sys.exit(8)"
assert subprocess.run([sys.executable, '-c', probe, str(fd)], close_fds=False, timeout=5).returncode == 0
process_admission._lease = None
parent.close()
thread.join(3)
assert not thread.is_alive()
# A later socket reusing the old descriptor is NOT owned by the old hook.
fresh, peer = socket.socketpair()
assert fd in (fresh.fileno(), peer.fileno())
pid = os.fork()
if pid == 0:
    try:
        os.fstat(fresh.fileno())
        os.fstat(peer.fileno())
        fresh.sendall(b'ok')
        os._exit(0)
    except BaseException:
        os._exit(9)
assert os.waitpid(pid, 0)[1] == 0
assert peer.recv(2) == b'ok'
fresh.close(); peer.close()
"""
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, encoding='utf-8', timeout=15)
    assert result.returncode == 0, result.stderr


def test_public_transport_cannot_reach_owner_dispatch(monkeypatch):
    from types import SimpleNamespace
    from tui_gateway import server
    monkeypatch.setenv('HERMES_DESKTOP', '1')
    transport = SimpleNamespace(write=lambda frame: True)
    for operation in ('status', 'quiesce', 'cancel'):
        result = server.dispatch({'id': 1, 'method': f'process.owner.{operation}',
                                  'params': {'owner': 'electron', 'profile': 'general'}}, transport)
        assert result['error']['code'] == -32601
