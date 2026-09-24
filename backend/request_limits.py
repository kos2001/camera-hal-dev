"""Bound request bodies before JSON parsing or endpoint execution."""
from __future__ import annotations

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

MAX_REQUEST_BYTES = 32 * 1024


class RequestSizeMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int = MAX_REQUEST_BYTES):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        for name, value in scope.get("headers", []):
            if name.lower() == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    await JSONResponse({"detail": "Content-Length가 잘못되었습니다."}, status_code=400)(
                        scope, receive, send)
                    return
                if declared < 0:
                    await JSONResponse({"detail": "Content-Length가 잘못되었습니다."}, status_code=400)(
                        scope, receive, send)
                    return
                if declared > self.max_bytes:
                    await self._reject(scope, receive, send)
                    return

        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > self.max_bytes:
                await self._reject(scope, receive, send)
                return
            if chunk:
                chunks.append(chunk)
            if not message.get("more_body", False):
                break

        replayed = False

        async def replay_receive():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()

        await self.app(scope, replay_receive, send)

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
        await JSONResponse({"detail": "요청 본문이 너무 큽니다."}, status_code=413)(scope, receive, send)
