"""Request body limits cover declared and streamed input."""
from __future__ import annotations

import asyncio
import unittest

from fastapi.testclient import TestClient

from backend.app import app
from backend.request_limits import MAX_REQUEST_BYTES, RequestSizeMiddleware


class RequestLimitTest(unittest.TestCase):
    def test_api_rejects_large_body_before_json_parsing(self):
        response = TestClient(app).post("/api/run", content=b"x" * (MAX_REQUEST_BYTES + 1),
                                        headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")

    def test_streamed_body_limit_without_content_length(self):
        async def run(chunks: list[bytes]):
            received: list[dict] = []
            sent: list[dict] = []

            async def endpoint(scope, receive, send):
                message = await receive()
                received.append(message)
                await send({"type": "http.response.start", "status": 200, "headers": []})
                await send({"type": "http.response.body", "body": b"ok"})

            iterator = iter(enumerate(chunks))

            async def receive():
                index, chunk = next(iterator)
                return {"type": "http.request", "body": chunk,
                        "more_body": index < len(chunks) - 1}

            async def send(message):
                sent.append(message)

            middleware = RequestSizeMiddleware(endpoint)
            await middleware({"type": "http", "method": "POST", "path": "/api/run", "headers": []},
                             receive, send)
            return received, sent

        received, sent = asyncio.run(run([b"a" * MAX_REQUEST_BYTES]))
        self.assertEqual(len(received[0]["body"]), MAX_REQUEST_BYTES)
        self.assertEqual(sent[0]["status"], 200)
        received, sent = asyncio.run(run([b"a" * MAX_REQUEST_BYTES, b"b"]))
        self.assertEqual(received, [])
        self.assertEqual(sent[0]["status"], 413)
