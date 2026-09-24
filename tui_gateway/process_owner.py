"""Launch-only owner transport. Not registered in public JSON-RPC/HTTP.

FD 3 is a dedicated Electron-created duplex socket, never a listening address.
The initial capability arrives only there; no ambient token/env grants authority.
The channel owns a process admission lease, never termination authority.
"""
import hashlib
import hmac
import json
import os
import secrets
import socket
import threading
from contextlib import contextmanager

from tui_gateway import process_admission

_channels_lock = threading.RLock()
_channels = set()


def protect_owner_channel(channel):
    """Register before starting the owner thread, closing the launch/fork gap."""
    with _channels_lock:
        channel.set_inheritable(False)
        _channels.add(channel)
    return channel


def _after_fork_child():
    # detach invalidates the socket held by makefile too. Do NOT shutdown: that
    # would affect the parent's shared socket endpoint. Never retain bare fd IDs.
    try:
        for channel in _channels:
            fd = channel.detach()
            if fd >= 0:
                os.close(fd)
        if _channels:
            process_admission.revoke_after_fork()
        _channels.clear()
    finally:
        _channels_lock.release()


if hasattr(os, 'register_at_fork'):
    os.register_at_fork(before=_channels_lock.acquire,
                        after_in_parent=_channels_lock.release,
                        after_in_child=_after_fork_child)


@contextmanager
def _owned_stream(channel):
    protect_owner_channel(channel)
    try:
        with channel.makefile('rwb', buffering=0) as stream:
            yield stream
    finally:
        with _channels_lock:
            _channels.discard(channel)
            channel.close()

_MAX_FRAME = 4096


def serve_owner_channel(channel: socket.socket) -> None:
    """One transport = one untransferable generation; EOF revokes it forever."""
    from tui_gateway import process_admission
    generation = None
    with _owned_stream(channel) as stream:
        def read():
            raw = stream.readline(_MAX_FRAME + 1)
            if not raw or len(raw) > _MAX_FRAME or not raw.endswith(b'\n'):
                raise ValueError('invalid frame')
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError('invalid frame')
            return value

        def write(value):
            stream.write(json.dumps(value, separators=(',', ':')).encode() + b'\n')

        try:
            bootstrap = read()
            if set(bootstrap) != {'capability'} or not isinstance(bootstrap['capability'], str):
                return
            key = bytes.fromhex(bootstrap['capability'])
            if len(key) != 32:
                return
            del bootstrap
            generation = secrets.token_hex(32)
            sequence = 0
            write({'generation': generation})
            while True:
                frame = read()
                seq, operation = frame.get('seq'), frame.get('operation')
                tag = frame.get('tag')
                if (set(frame) != {'generation', 'seq', 'operation', 'tag'}
                        or type(seq) is not int or seq != sequence + 1
                        or frame.get('generation') != generation
                        or not isinstance(operation, str) or not isinstance(tag, str)):
                    write({'error': 'unauthorized'})
                    continue
                expected = hmac.new(key, f'{generation}:{seq}:{operation}'.encode(), hashlib.sha256).hexdigest()
                if not hmac.compare_digest(expected.encode(), tag.encode()):
                    write({'error': 'unauthorized'})
                    continue
                sequence = seq
                handlers = {
                    'status': lambda: {'authority': 'launch-channel', 'process_fence': True},
                    'quiesce': lambda: process_admission.quiesce(generation),
                    'cancel': lambda: process_admission.cancel(generation),
                }
                handler = handlers.get(operation)
                write(handler() if handler else {'error': 'unsupported'})
        except (ValueError, OSError, UnicodeError):
            # No frame, capability, or signature is ever logged on malformed input.
            return
        finally:
            if generation is not None:
                process_admission.cancel(generation)
