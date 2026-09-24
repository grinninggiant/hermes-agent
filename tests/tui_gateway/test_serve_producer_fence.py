"""Real ASGI routing with fixture transports; no lifespan/provider activation."""
import asyncio
import threading

import pytest


@pytest.fixture
def serve(monkeypatch):
    from hermes_cli import web_server
    from tui_gateway import process_admission, server
    monkeypatch.setattr(server, '_sessions', {})
    monkeypatch.setattr(process_admission, '_inventory', lambda: {})
    yield web_server
    process_admission.cancel('fixture')


def test_http_dispatch_preserves_inflight_and_reopens(serve, monkeypatch):
    import httpx
    from tui_gateway import process_admission
    entered, finish = threading.Event(), threading.Event()
    # Replace only a route's external work, retaining the actual ASGI middleware stack.
    route = next(r for r in serve.app.routes if getattr(r, 'path', '') == '/api/health')
    async def work(scope, receive, send):
        entered.set()
        await asyncio.to_thread(finish.wait, 5)
        from starlette.responses import JSONResponse
        await JSONResponse({'fixture': True})(scope, receive, send)
    monkeypatch.setattr(route, 'app', work)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=serve.app), base_url='http://localhost') as client:
            pending = asyncio.create_task(client.get('/api/health'))
            assert await asyncio.to_thread(entered.wait, 5)
            try:
                receipt = process_admission.quiesce('fixture')
                assert receipt['inventory']['rpc'] >= 1
                assert (await client.get('/api/health')).status_code == 503
            finally:
                finish.set()
            assert (await pending).json() == {'fixture': True}
            assert process_admission._active == 0
            # Unsupported independent producers must still prevent an idle proof.
            assert process_admission.quiesce('fixture')['status'] == 'unknown'
            process_admission.cancel('fixture')
            assert (await client.get('/api/health')).status_code == 200
    asyncio.run(exercise())


def test_real_pty_route_blocks_new_connection_not_existing_work(serve, monkeypatch):
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect
    from hermes_cli import web_server_chat
    from hermes_cli.web_routers import chat_ws
    from tui_gateway import process_admission
    calls = []
    async def argv(**kwargs):
        return ['fixture'], '.', {}
    class Bridge:
        @staticmethod
        def spawn(*args, **kwargs):
            calls.append('spawn')
            return object()
    async def pump(ws, bridge):
        await ws.send_text('active')
        assert await ws.receive_text() == 'finish'
        await ws.send_text('finished')
    monkeypatch.setattr(web_server_chat, '_resolve_chat_argv_async', argv)
    monkeypatch.setattr(web_server_chat, 'PtyBridge', Bridge)
    monkeypatch.setattr(web_server_chat, '_PTY_BRIDGE_AVAILABLE', True)
    monkeypatch.setattr(chat_ws, '_legacy_pump', pump)
    # Existing auth remains in the real route; provide its actual session token.
    client = TestClient(serve.app, base_url='http://localhost', client=('127.0.0.1', 12345))
    url = '/api/pty?token=' + serve._SESSION_TOKEN
    with client.websocket_connect(url) as active:
        assert active.receive_text() == 'active'
        receipt = process_admission.quiesce('fixture')
        assert receipt['inventory']['rpc'] >= 1
        with pytest.raises(WebSocketDisconnect) as rejected:
            with client.websocket_connect(url):
                pass
        assert rejected.value.code == 1013
        assert calls == ['spawn']
        active.send_text('finish')
        assert active.receive_text() == 'finished'
    assert process_admission._active == 0
    process_admission.cancel('fixture')
    with client.websocket_connect(url) as reopened:
        assert reopened.receive_text() == 'active'
        reopened.send_text('finish')
        assert reopened.receive_text() == 'finished'
    assert calls == ['spawn', 'spawn']
