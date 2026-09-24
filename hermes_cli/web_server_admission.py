"""Serve ingress participation in the inherited process-owner lease.

This is admission, not cancellation: accepted ASGI calls retain their reservation
until all response/background work completes. Existing PTY streams are not
interrupted or parsed as commands. Detached PTYs and independent cron/room workers
still require their own fences; process_admission must continue to report unknown.
"""
from tui_gateway import process_admission


class ProcessAdmissionMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        kind = scope['type']
        # Native RPC has pre-executor per-message admission, including drain
        # operations. Counting its persistent socket would make idle impossible.
        if kind not in {'http', 'websocket'} or (
            kind == 'websocket' and scope.get('path') == '/api/ws'
        ):
            await self.app(scope, receive, send)
            return
        if not process_admission.admit():
            if kind == 'websocket':
                await send({'type': 'websocket.close', 'code': 1013,
                            'reason': 'Process admission is closed'})
            else:
                from starlette.responses import JSONResponse
                await JSONResponse({'detail': 'Process admission is closed'},
                                   status_code=503)(scope, receive, send)
            return
        try:
            await self.app(scope, receive, send)
        finally:
            process_admission.release()
